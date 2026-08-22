# Copyright (C) 2026 Max Ea
# This file is part of CRUSH-OS,   .


"""
ProactiveEngine — orchestrateur principal.
Tourne en background toutes les 30 minutes.
Dispatche les initiatives selon leur mode d'exécution.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time

from loguru import logger

from crush.engine.background.notifications import NotificationQueue
from crush.engine.proactive.context_builder import ContextBuilder
from crush.engine.proactive.initiative_generator import InitiativeGenerator
from crush.engine.proactive.schemas import ExecutionMode, Initiative, Priority
from crush.engine.proactive.store import InitiativeStore
from crush.kernel.contracts import PushSortant
from crush.kernel.settings import settings

_AUDIT_MAXLEN = 200


# ── Audit ─────────────────────────────────────────────────────────────────────


@dataclass
class ProactiveAuditEvent:
    """Événement auditable émis pour chaque décision proactive."""

    event_id: str
    initiative_id: str
    initiative_title: str
    decision: str  # "notify" | "validate" | "auto"
    reasoning: str
    sources: list[str]
    decided_at: str  # ISO UTC


_ORDRE_PRIORITE = {Priority.LOW: 0, Priority.MEDIUM: 1, Priority.HIGH: 2}


# Au-dela de ce nombre d'initiatives en attente, on cesse d'en produire. Choisi
# a partir de l'observe : 25 en attente et 44 rejets sur 76 en sept jours, donc
# une file qui a largement depasse ce qu'un humain traite. Douze tient sur un
# ecran de telephone sans faire defiler.
_FILE_PLEINE = 12


def _dans_le_silence(maintenant: time, plage: str) -> bool:
    """Sommes-nous dans la plage de silence ? Une plage illisible ne bâillonne rien.

    Le sens de l'erreur est l'inverse de celui de `_atteint_le_seuil` : là, une
    valeur illisible POUSSE, parce que le bruit se remarque et se corrige tandis
    que le silence ne se remarque pas. Ici, une plage illisible ne doit pas faire
    taire l'assistant pour la même raison — une faute de frappe dans un réglage
    ne doit jamais avoir pour effet de perdre des messages.
    """
    plage = (plage or "").strip()
    if not plage:
        return False
    try:
        debut_txt, fin_txt = plage.split("-", 1)
        h1, m1 = (int(x) for x in debut_txt.strip().split(":", 1))
        h2, m2 = (int(x) for x in fin_txt.strip().split(":", 1))
        debut, fin = time(h1, m1), time(h2, m2)
    except (ValueError, TypeError):
        logger.warning("Plage d'heures de silence illisible — ignoree", plage=plage)
        return False
    if debut == fin:
        return False
    # La plage enjambe minuit dans le cas usuel (23:00-07:00) : deux intervalles.
    if debut < fin:
        return debut <= maintenant < fin
    return maintenant >= debut or maintenant < fin


def _atteint_le_seuil(priorite: Priority, seuil: str) -> bool:
    """La priorite justifie-t-elle d'interrompre l'utilisateur ?"""
    try:
        minimum = _ORDRE_PRIORITE[Priority(seuil.strip().lower())]
    except (ValueError, KeyError):
        # Reglage illisible : on pousse. Pour cette fonction, etre bruyant sur une
        # mauvaise configuration vaut mieux que taire silencieusement une decision
        # attendue -- le bruit se remarque et se corrige, le silence non.
        logger.warning("PUSH_NOTIFY_PRIORITY_MIN illisible, push force", valeur=seuil)
        return True
    return _ORDRE_PRIORITE.get(priorite, 0) >= minimum


def _extract_sources(initiative: Initiative) -> list[str]:
    """Infère les sources d'information utilisées pour cette initiative."""
    text = f"{initiative.context} {initiative.reasoning}".lower()
    keywords: dict[str, list[str]] = {
        "email": ["email", "mail", "inbox"],
        "calendrier": ["calendar", "agenda", "event", "rdv"],
        "notion": ["notion", "tâche", "task"],
        "météo": ["météo", "weather", "pluie", "soleil"],
        "mémoire": ["memory", "mémoire", "session"],
    }
    found = [k for k, words in keywords.items() if any(w in text for w in words)]
    return found or ["proactive_context"]


class ProactiveEngine:
    """Phase C : `builder`, `generator`, `store` injectés (auparavant
    `ContextBuilder()`, `InitiativeGenerator()`, `InitiativeStore()`
    instanciés en interne). Les 3 helpers sont des objets simples
    sans déps, mais l'injection rend le ProactiveEngine testable
    (fakes pour builder/generator) sans patch global.
    """

    def __init__(
        self,
        notification_queue: NotificationQueue,
        broadcast_event: Callable,  # ProactiveQueue.broadcast_event(dict) sync
        builder: ContextBuilder,
        generator: InitiativeGenerator,
        store: InitiativeStore,
        interval_minutes: int = 30,
    ) -> None:
        self._notifications = notification_queue
        self._broadcast_event = broadcast_event
        # Branche apres coup par interfaces/channels/setup.py : les canaux ne sont
        # demarres qu'au lifespan de l'app, bien apres bootstrap.build().
        self._push: PushSortant | None = None
        self._interval = interval_minutes * 60
        self._builder = builder
        self._generator = generator
        self._store = store
        self._running = False
        self._last_run: datetime | None = None
        self._last_user_activity: datetime | None = None
        self._cycle_lock = asyncio.Lock()  # un seul cycle à la fois
        self._audit_log: deque[ProactiveAuditEvent] = deque(maxlen=_AUDIT_MAXLEN)

    def signal_user_activity(self) -> None:
        """Appelé par le WebSocket à chaque message entrant."""
        self._last_user_activity = datetime.now()

    def _user_idle_seconds(self) -> float:
        """Secondes écoulées depuis le dernier message utilisateur."""
        if self._last_user_activity is None:
            return float("inf")
        return (datetime.now() - self._last_user_activity).total_seconds()

    async def start(self) -> None:
        """Lance la boucle de proactivité en background."""
        self._running = True
        logger.info(f"ProactiveEngine started (interval: {self._interval // 60}min)")

        # Restaure les initiatives pending avant d'attendre le premier cycle
        await self._restore_pending()

        # Premier run dans 2 minutes — pas immédiatement au boot
        await asyncio.sleep(120)

        while self._running:
            await self._run_cycle()
            await asyncio.sleep(self._interval)

    def stop(self) -> None:
        self._running = False

    async def _restore_pending(self) -> None:
        """Recharge les initiatives pending et signal le Command Center en un seul event."""
        pending = self._store.load_pending_all(days=7)
        if not pending:
            return
        # Un seul événement groupé — évite N rechargements UI en cascade
        self._broadcast_event({"type": "initiatives_restored", "count": len(pending)})
        logger.info(f"ProactiveEngine: {len(pending)} initiatives pending restaurées")

    async def run_now(self) -> list[Initiative]:
        """Force un cycle immédiatement (debug ou bouton manuel)."""
        return await self._run_cycle()

    def audit_events(self, limit: int = 50) -> list[ProactiveAuditEvent]:
        """Retourne les derniers événements d'audit (plus récent en premier)."""
        return list(reversed(list(self._audit_log)))[:limit]

    async def _run_cycle(self) -> list[Initiative]:
        """Un cycle complet : collecte → build → generate → dispatch."""
        if self._cycle_lock.locked():
            logger.info("ProactiveEngine: cycle already running, skipping")
            return []

        async with self._cycle_lock:
            return await self.__run_cycle_locked()

    async def __run_cycle_locked(self) -> list[Initiative]:
        logger.info("ProactiveEngine: starting cycle")
        self._last_run = datetime.now()

        try:
            state = await self._builder.build()

            # Même pattern que le websocket existant (sleep(2) avant background LLM) :
            # attendre que l'utilisateur soit inactif avant de faire l'appel LLM lourd.
            _COOLDOWN_S = 120  # 2 minutes d'inactivité requises
            idle = self._user_idle_seconds()
            if idle < _COOLDOWN_S:
                wait = _COOLDOWN_S - idle
                logger.info(
                    f"ProactiveEngine: user active {idle:.0f}s ago, "
                    f"waiting {wait:.0f}s before LLM call"
                )
                await asyncio.sleep(wait)

            # Le générateur voit désormais ce qui attend déjà et ce qui a été
            # rejeté. Sans ça il repartait du seul état du monde, toutes les trois
            # heures, et redécouvrait les mêmes choses.
            historique = self._historique()

            # ARRÊTER D'AJOUTER À UNE FILE PLEINE. Huit cycles par jour à cinq
            # initiatives, c'est quarante par jour dans une file que rien ne vide :
            # elle ne peut que croître, et une file qui croît est une file qu'on
            # cesse d'ouvrir. Au-delà du seuil, on se taît jusqu'à ce qu'elle
            # redescende — rien n'est perdu, la question sera reposée.
            en_attente = len(historique.get("en_attente", []))
            if en_attente >= _FILE_PLEINE:
                logger.info(
                    "ProactiveEngine: file deja pleine, aucune generation",
                    en_attente=en_attente,
                    seuil=_FILE_PLEINE,
                )
                return []

            initiatives = await self._generator.generate(state, historique)

            if not initiatives:
                logger.info("ProactiveEngine: no initiatives generated")
                return []

            for initiative in initiatives:
                self._store.save(initiative)

            for initiative in initiatives:
                await self._dispatch(initiative)

            high_count = sum(1 for i in initiatives if i.priority == Priority.HIGH)
            logger.info(
                f"ProactiveEngine: cycle complete — "
                f"{len(initiatives)} initiatives, {high_count} HIGH"
            )

            self._broadcast_event(
                {
                    "type": "proactive_update",
                    "count": len(initiatives),
                    "high_priority": high_count,
                }
            )

            return initiatives

        except Exception as e:
            logger.error(f"ProactiveEngine cycle error: {e}")
            return []

    def brancher_push(self, push: PushSortant) -> None:
        """Donne au moteur un moyen d'atteindre l'utilisateur de lui-meme."""
        self._push = push
        logger.info("Moteur proactif : push sortant branche", disponible=push.disponible())

    def _historique(self) -> dict[str, list]:
        """Ce que le magasin sait du passe recent, ou rien.

        Tolerant a un magasin plus ancien : `resume_pour_generateur` peut ne pas
        exister. Le cycle proactif ne doit pas tomber pour un enrichissement de
        prompt -- il perdrait alors la seule fonction qui marchait deja.
        """
        lecteur = getattr(self._store, "resume_pour_generateur", None)
        if not callable(lecteur):
            return {}
        try:
            return dict(lecteur())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Historique des initiatives illisible", erreur=str(exc))
            return {}

    async def _pousser(self, initiative: Initiative, texte: str) -> None:
        """Pousse vers les canaux quand ca merite d'interrompre, pas a chaque cycle.

        VALIDATE part toujours : elle demande une DECISION, et sans push elle
        attend qu'on ouvre le Command Center. NOTIFY ne part qu'au-dessus du seuil
        de priorite : elle a deja un chemin qui fonctionne -- la file de
        notifications, servie a la prochaine conversation -- et tout pousser
        reviendrait a n'etre plus lu du tout.
        """
        if self._push is None or not settings.push_proactive_enabled:
            return
        if initiative.execution_mode == ExecutionMode.NOTIFY and not _atteint_le_seuil(
            initiative.priority, settings.push_notify_priority_min
        ):
            return

        # Heures de silence. Ce qui n'a rien d'urgent attend le matin : le push
        # marche, et c'est justement le probleme — ce qui sonne la nuit finit par
        # etre coupe, et l'alerte utile part avec le reste. L'initiative n'est PAS
        # perdue, elle reste dans la file servie a la prochaine conversation.
        if _dans_le_silence(datetime.now().time(), settings.push_heures_silence):
            urgent = (
                initiative.execution_mode == ExecutionMode.VALIDATE
                and initiative.priority == Priority.HIGH
                and settings.push_silence_laisse_passer_urgent
            )
            if not urgent:
                logger.info(
                    "Initiative retenue jusqu'au matin",
                    initiative=initiative.title,
                    plage=settings.push_heures_silence,
                )
                return
        # VALIDATE attend une reponse : on pousse la question ET le moyen d'y
        # repondre. NOTIFY n'attend rien, un bouton y serait un faux choix.
        if initiative.execution_mode == ExecutionMode.VALIDATE:
            abouti = await self._push.pousser_decision(texte, initiative.id)
        else:
            abouti = await self._push.pousser(texte)
        if not abouti:
            logger.warning("Initiative non poussee", initiative=initiative.title)

    async def _dispatch(self, initiative: Initiative) -> None:
        """Dispatche une initiative selon son mode d'exécution."""
        audit = ProactiveAuditEvent(
            event_id=f"aud_{uuid.uuid4().hex[:8]}",
            initiative_id=initiative.id,
            initiative_title=initiative.title,
            decision=str(initiative.execution_mode),
            reasoning=(initiative.reasoning or initiative.action)[:200],
            sources=_extract_sources(initiative),
            decided_at=datetime.now(UTC).isoformat(),
        )
        self._audit_log.append(audit)
        self._broadcast_event({"type": "proactive_audit", "event": asdict(audit)})
        logger.info(
            f"ProactiveEngine AUDIT [{audit.decision}] {audit.initiative_title!r} "
            f"— {audit.reasoning[:80]} | sources={audit.sources}"
        )

        if initiative.execution_mode == ExecutionMode.AUTO:
            # Auto-exécution réservée à la Phase 2
            logger.info(f"ProactiveEngine AUTO (logged): {initiative.title}")

        elif initiative.execution_mode == ExecutionMode.NOTIFY:
            # Injecter comme notification texte dans la prochaine conversation
            msg = (
                f"[{settings.display_assistant_name} proactif] "
                f"{initiative.title} — {initiative.action}"
            )
            self._notifications.add(msg)
            await self._pousser(initiative, msg)
            logger.info(f"ProactiveEngine NOTIFY: {initiative.title}")

        elif initiative.execution_mode == ExecutionMode.VALIDATE:
            # Envoyer au Command Center pour validation
            self._broadcast_event(
                {
                    "type": "initiative_pending",
                    "initiative": {
                        "id": initiative.id,
                        "type": initiative.type,
                        "title": initiative.title,
                        "context": initiative.context,
                        "reasoning": initiative.reasoning,
                        "action": initiative.action,
                        "priority": initiative.priority,
                        "draft_content": initiative.draft_content,
                        "created_at": initiative.created_at.isoformat(),
                    },
                }
            )
            # C'est LE cas qui se perdait : diffuse aux seuls clients connectes,
            # donc a personne la plupart du temps.
            await self._pousser(
                initiative,
                f"*{initiative.title}*\n{initiative.action}\n\n"
                f"Décision attendue. {initiative.reasoning or ''}".strip(),
            )
            logger.info(f"ProactiveEngine VALIDATE: {initiative.title}")
