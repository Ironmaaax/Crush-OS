# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .


from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from loguru import logger

from crush.engine.background.notifications import NotificationQueue, ProactiveQueue
from crush.engine.background.routines import (
    ROUTINES_ENABLED,
    CatchUpPolicy,
    Routine,
    RoutineStore,
    TriggerType,
    apply_catch_up,
    fire_routine,
    next_cron_datetime,
)
from crush.kernel.contracts import (
    AutoDreamer,
    BoiteMemoire,
    CalendarReadTool,
    MemoryBackup,
    NotionReadTool,
)
from crush.kernel.schemas import ResultatBoiteReception
from crush.kernel.settings import Settings


def _next_datetime(hour: int) -> datetime:
    now = datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def _seconds_until(hour: int) -> float:
    return (_next_datetime(hour) - datetime.now()).total_seconds()


class Scheduler:
    """Planifie les boucles asyncio : briefing 9h, rappels calendrier, autoDream, routines.

    Phase C : `settings` injecté au constructeur (auparavant
    `from config.settings import settings` en module-level). Les autres
    deps (proactive, auto_dream, calendar_tool, skill_lab, curator)
    étaient DÉJÀ injectées en pré-C.
    """

    def __init__(
        self,
        proactive: ProactiveQueue,
        auto_dream: AutoDreamer,
        calendar_tool: CalendarReadTool,
        settings: Settings,
        notion_tool: NotionReadTool | None = None,
        skill_lab: object | None = None,
        curator: object | None = None,
        notifications: NotificationQueue | None = None,
        sauvegarde: MemoryBackup | None = None,
        boite: BoiteMemoire | None = None,
    ) -> None:
        self._proactive = proactive
        self._auto_dream = auto_dream
        self._calendar_tool = calendar_tool
        self._settings = settings
        self._notion_tool = notion_tool  # Phase D — injecté plutôt qu'instancié dans la boucle
        self._skill_lab = skill_lab  # PHASE 4 — SkillLab pour polling nocturne
        self._curator = curator  # PHASE 6 — Curator nocturne
        # Archivage de la mémoire. Injecté (RÈGLE 3 : l'engine n'importe pas providers).
        self._sauvegarde = sauvegarde
        # Consignes écrites à la main dans Obsidian. Même règle d'injection.
        self._boite = boite
        # File des notifications, glissées en fin de réponse par le Gateway.
        # `_proactive.broadcast` ne touche que les clients CONNECTÉS à l'instant :
        # sur une machine allumée en permanence et consultée par intermittence,
        # une panne signalée par ce canal n'atteint personne.
        self._notifications = notifications
        # Vrai une fois la panne calendrier signalée : sans mémoire, l'alerte
        # repartirait à chaque tentative.
        self._panne_calendrier_signalee = False
        self._tasks: list[asyncio.Task] = []
        self._routine_tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._briefing_loop(), name="scheduler-briefing"),
            asyncio.create_task(self._calendar_loop(), name="scheduler-calendar"),
            asyncio.create_task(self._autodream_loop(), name="scheduler-autodream"),
        ]
        if self._skill_lab is not None:
            self._tasks.append(
                asyncio.create_task(self._skill_lab_loop(), name="scheduler-skill-lab")
            )
        if self._curator is not None:
            self._tasks.append(asyncio.create_task(self._curator_loop(), name="scheduler-curator"))
        if self._sauvegarde is not None and self._settings.backup_enabled:
            self._tasks.append(
                asyncio.create_task(self._sauvegarde_loop(), name="scheduler-sauvegarde")
            )
        if self._boite is not None and self._settings.obsidian_inbox_enabled:
            self._tasks.append(asyncio.create_task(self._boite_loop(), name="scheduler-boite"))
        logger.info("Scheduler started", tasks=len(self._tasks))

    def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._routine_tasks:
            t.cancel()

    def start_routines(
        self,
        routines: list[Routine],
        store: RoutineStore,
        wake_engine: Callable[[], None] | None = None,
    ) -> None:
        """Démarre une boucle asyncio par routine activée.

        Ne fait rien si ROUTINES_ENABLED=false ou si la liste est vide.
        """
        if not ROUTINES_ENABLED:
            logger.info("Scheduler: routines désactivées (ROUTINES_ENABLED=false)")
            return

        started = 0
        for routine in routines:
            if not routine.enabled:
                continue
            if routine.trigger == TriggerType.INTERVAL and routine.interval_seconds:
                t = asyncio.create_task(
                    self._routine_interval_loop(routine, store, wake_engine),
                    name=f"routine-interval-{routine.name}",
                )
                self._routine_tasks.append(t)
                started += 1
            elif routine.trigger == TriggerType.CRON and routine.cron_expr:
                t = asyncio.create_task(
                    self._routine_cron_loop(routine, store, wake_engine),
                    name=f"routine-cron-{routine.name}",
                )
                self._routine_tasks.append(t)
                started += 1
            else:
                logger.debug(
                    f"Routine '{routine.name}': trigger {routine.trigger} ignoré "
                    "(webhook géré par API)"
                )
        logger.info(f"Scheduler: {started} routine(s) démarrée(s)")

    def status(self) -> list[dict]:
        return [
            {
                "name": "Briefing matinal",
                "description": f"Agenda + tâches Notion à {self._settings.briefing_hour}h00",
                "next_run": _next_datetime(self._settings.briefing_hour).isoformat(),
                "interval": "quotidien",
            },
            {
                "name": "Rappels calendrier",
                "description": (
                    f"Rappel {self._settings.calendar_reminder_minutes} min avant chaque event"
                ),
                "next_run": None,
                "interval": "toutes les 60s",
            },
            {
                "name": "AutoDream deep",
                "description": "Analyse nocturne des sessions",
                "next_run": _next_datetime(3).isoformat(),
                "interval": "quotidien",
            },
            {
                "name": "Sauvegarde de la mémoire",
                "description": (
                    f"Archive memory_data/ à {self._settings.backup_hour}h00, "
                    f"{self._settings.backup_keep} archives conservées"
                ),
                "next_run": (
                    _next_datetime(self._settings.backup_hour).isoformat()
                    if self._settings.backup_enabled
                    else None
                ),
                "interval": "quotidien" if self._settings.backup_enabled else "désactivée",
            },
        ]

    # ── Routines ──────────────────────────────────────────────

    async def _routine_interval_loop(
        self,
        routine: Routine,
        store: RoutineStore,
        wake_engine: Callable[[], None] | None,
    ) -> None:
        """Boucle d'exécution pour une routine à intervalle fixe."""
        interval = routine.interval_seconds or 3600

        # Rattrapage au démarrage si des runs ont été manqués
        if routine.catch_up_policy == CatchUpPolicy.ENQUEUE_MISSED:
            await apply_catch_up(routine, store, self._proactive.broadcast_event, wake_engine)

        while True:
            await asyncio.sleep(interval)
            await fire_routine(routine, store, self._proactive.broadcast_event, wake_engine)

    async def _routine_cron_loop(
        self,
        routine: Routine,
        store: RoutineStore,
        wake_engine: Callable[[], None] | None,
    ) -> None:
        """Boucle d'exécution pour une routine à expression cron."""
        expr = routine.cron_expr or "0 * * * *"
        while True:
            now = datetime.now(UTC)
            try:
                next_run = next_cron_datetime(expr, after=now)
            except ValueError as exc:
                logger.error(f"Routine '{routine.name}': expression cron invalide — {exc}")
                await asyncio.sleep(3600)
                continue

            delay = (next_run - datetime.now(UTC)).total_seconds()
            logger.debug(
                f"Routine '{routine.name}' (cron): prochaine exécution "
                f"dans {delay:.0f}s ({next_run.isoformat()})"
            )
            await asyncio.sleep(max(delay, 0))
            await fire_routine(routine, store, self._proactive.broadcast_event, wake_engine)

    # ── Briefing matinal ─────────────────────────────────────

    async def _briefing_loop(self) -> None:
        while True:
            delay = _seconds_until(self._settings.briefing_hour)
            logger.debug("Briefing planifié", seconds=int(delay))
            await asyncio.sleep(delay)
            await self._send_briefing()

    async def _send_briefing(self) -> None:
        parts: list[str] = []

        try:
            result = await self._calendar_tool.execute(days_ahead=1)
            agenda = result.content if not result.is_error else "Agenda indisponible."
            parts.append(f"Agenda : {agenda}")
        except Exception as e:
            parts.append(f"Agenda indisponible ({e}).")

        if self._notion_tool is not None:
            try:
                tasks_result = await self._notion_tool.execute()
                if not tasks_result.is_error and tasks_result.content:
                    parts.append(f"Tâches du jour :\n{tasks_result.content}")
            except Exception as e:
                logger.debug("Briefing Notion error", error=str(e))

        self._proactive.broadcast("Briefing matinal — " + " | ".join(parts))
        logger.info("Briefing matinal envoyé")

    # ── Rappels calendrier ────────────────────────────────────

    # Cadence nominale des rappels : une minute suffit pour un seuil de rappel
    # exprimé en minutes. En cas d'échec, on s'espace jusqu'à ce plafond.
    _CADENCE_NOMINALE = 60
    _CADENCE_MAX = 900

    def _signaler(self, message: str) -> None:
        """Fait remonter un message dans la prochaine conversation.

        Retombe sur la diffusion si aucune file n'a été injectée : mieux
        vaut un canal imparfait que le silence.
        """
        if self._notifications is not None:
            self._notifications.add(message)
        else:
            self._proactive.broadcast(message)

    async def _calendar_loop(self) -> None:
        """Rappels d'événements, avec espacement progressif en cas d'échec.

        La boucle interrogeait le calendrier toutes les 60 s quoi qu'il arrive.
        Sans autorisation Google — l'état par défaut d'une installation neuve —
        chaque passage journalisait un avertissement : 1440 lignes par jour, qui
        noient les vraies anomalies. Mesuré ici : sur trente minutes, 36
        « anomalies » dont aucune n'en était une.

        L'espacement se réinitialise au premier succès, donc l'autorisation
        accordée est reprise en quinze minutes au pire, sans rien redémarrer.
        """
        seen: set[str] = set()
        await asyncio.sleep(10)  # court délai initial — calendrier pas encore auth au démarrage
        attente = self._CADENCE_NOMINALE
        while True:
            ok = await self._check_reminders(seen)
            if ok:
                if self._panne_calendrier_signalee:
                    self._signaler(
                        "L'accès à ton agenda Google refonctionne."
                    )
                    self._panne_calendrier_signalee = False
                attente = self._CADENCE_NOMINALE
            else:
                attente = min(attente * 2, self._CADENCE_MAX)
                logger.debug("Rappels calendrier espacés", prochaine_tentative_s=attente)
                # Signalé UNE fois, quand l'espacement atteint son plafond : à ce
                # stade la panne dure depuis un quart d'heure, ce n'est plus un
                # incident réseau passager. Sans cela, une autorisation Google
                # révoquée — expiration du jeton, changement de mot de passe —
                # se découvre des semaines plus tard, en se demandant pourquoi
                # les rappels ont cessé.
                if attente >= self._CADENCE_MAX and not self._panne_calendrier_signalee:
                    self._panne_calendrier_signalee = True
                    self._signaler(
                        "Je n'accède plus à ton agenda Google — les rappels "
                        "d'événements sont suspendus. L'autorisation a "
                        "probablement expiré : la refaire depuis "
                        "/api/google/auth/calendar."
                    )
            await asyncio.sleep(attente)

    # Format renvoyé par CalendarListTool : "- 2024-01-15T14:00:00+01:00 : Titre"
    _ISO_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2})")

    async def _check_reminders(self, seen: set[str]) -> bool:
        """True si le calendrier a répondu. False pilote l'espacement.

        Un calendrier non autorisé et un réseau coupé se traitent pareil :
        réessayer plus tard, sans bruit, et reprendre la cadence dès que
        ça répond.
        """
        try:
            result = await self._calendar_tool.execute(days_ahead=2)
            if result.is_error:
                logger.debug("Calendar reminder: tool error", content=result.content[:80])
                return False
            now = datetime.now(UTC)
            cutoff = self._settings.calendar_reminder_minutes
            lines = [ln.strip() for ln in result.content.splitlines() if ln.strip()]
            logger.debug("Calendar reminder check", events=len(lines), cutoff_min=cutoff)
            for line in lines:
                fingerprint = line[:80]
                if fingerprint in seen:
                    continue
                iso_match = self._ISO_RE.search(line)
                if not iso_match:
                    continue
                try:
                    event_time = datetime.fromisoformat(iso_match.group(1))
                except ValueError:
                    continue
                delta_min = (event_time - now).total_seconds() / 60
                logger.debug("Calendar event delta", delta_min=round(delta_min, 1), event=line[:60])
                if 0 < delta_min <= cutoff:
                    seen.add(fingerprint)
                    self._proactive.broadcast(f"Rappel dans {int(delta_min)} min : {line}")
                    logger.info("Rappel calendrier envoyé", event=line[:60])
        except Exception:
            logger.exception("Calendar reminder error")
            return False
        return True

    # ── AutoDream nocturne ────────────────────────────────────

    async def _autodream_loop(self) -> None:
        while True:
            delay = _seconds_until(3)
            logger.debug("AutoDream deep planifié", seconds=int(delay))
            await asyncio.sleep(delay)
            logger.info("AutoDream deep démarré")
            await self._auto_dream.deep_analyze()

    # ── Skill Lab nocturne (PHASE 4) ─────────────────────────
    # Polling Kernel des events skill_candidate_proposal — décision F=b.
    # Tourne 5 min APRÈS AutoDream deep pour que les leçons fraîchement
    # ingérées puissent être prises en compte si elles ont déclenché un
    # signal skill_candidate (rare mais possible si une leçon batch génère
    # un skill_candidate_proposal indirectement).

    async def _skill_lab_loop(self) -> None:
        if self._skill_lab is None:
            return
        while True:
            # 3h05 du matin (5 min après AutoDream deep). Pas critique : si on
            # rate la fenêtre, le prochain run sera dans 24h, c'est cohérent
            # avec la fréquence batch de la mémoire.
            delay = _seconds_until(3) + 300
            logger.debug("Skill Lab scan planifié", seconds=int(delay))
            await asyncio.sleep(delay)
            try:
                logger.info("Skill Lab scan nocturne démarré")
                result = await self._skill_lab.scan_kernel()
                logger.info(
                    "Skill Lab scan terminé",
                    examined=result.events_examined,
                    generated=result.candidates_generated,
                    passed=result.sandbox_passed,
                    failed=result.sandbox_failed,
                    skipped=result.skipped_already_handled,
                )
            except Exception as exc:  # noqa: BLE001 — un scan raté ne tue pas la boucle
                logger.warning("Skill Lab scan échec", error=str(exc))

    # ── Curator nocturne (PHASE 6) ────────────────────────────
    # 3h10 du matin (10 min après AutoDream deep, 5 min après Skill Lab scan).
    # Pas critique : le Curator est RAPPORTEUR uniquement en MVP, son rapport
    # peut être consulté à tout moment via GET /api/curator/latest, et un
    # /api/curator/scan manuel est dispo pour itérer sans attendre la nuit.

    async def _curator_loop(self) -> None:
        if self._curator is None:
            return
        while True:
            delay = _seconds_until(3) + 600
            logger.debug("Curator scan planifié", seconds=int(delay))
            await asyncio.sleep(delay)
            try:
                logger.info("Curator scan nocturne démarré")
                report = await self._curator.scan()
                logger.info(
                    "Curator scan terminé",
                    patches=len(report.patches),
                    refused=len(report.refused_protected_patches),
                    facts_archive=report.facts_archive_proposed,
                    skills_stale=report.skills_stale_proposed,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Curator scan échec", error=str(exc))

    # ── Sauvegarde de la mémoire ──────────────────────────────

    # Programmée APRÈS le Curator (3h + 10 min) : celui-ci archive des faits et
    # propose des patches. Sauvegarder avant lui capturerait un état qu'il va
    # modifier dans les minutes qui suivent.

    async def _sauvegarde_loop(self) -> None:
        """Archive la mémoire une fois par jour, et ne parle qu'en cas de problème.

        Le silence en cas de succès est volontaire. Une notification quotidienne
        « tout va bien » finit par ne plus être lue, et c'est alors l'alerte utile
        qui passe inaperçue. On ne signale donc que ce qui demande une action.
        """
        if self._sauvegarde is None:
            return
        while True:
            delay = _seconds_until(self._settings.backup_hour)
            logger.debug("Sauvegarde mémoire planifiée", seconds=int(delay))
            await asyncio.sleep(delay)
            try:
                resultat = await self._sauvegarde.sauvegarder()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Sauvegarde mémoire échec", error=str(exc))
                self._signaler(
                    f"La sauvegarde de ma mémoire a échoué ({type(exc).__name__}). "
                    "Elle n'existe donc qu'en un seul exemplaire."
                )
                continue

            if not resultat.reussie:
                logger.warning("Sauvegarde mémoire échec", error=resultat.erreur)
                self._signaler(
                    f"La sauvegarde de ma mémoire a échoué : {resultat.erreur}. "
                    "Elle n'existe donc qu'en un seul exemplaire."
                )
                continue

            # Archive écrite mais copie hors machine impossible : l'archive existe,
            # elle est juste sur le support qu'elle est censée protéger. Ça vaut un
            # mot, pas une alarme.
            if resultat.erreur:
                self._signaler(
                    f"Mémoire sauvegardée ({resultat.archive}), mais {resultat.erreur} "
                    "L'archive est donc sur le même support que l'original."
                )

    async def _boite_loop(self) -> None:
        """Relit la boîte de réception Obsidian et applique ce qui y est écrit.

        Cadence courte — dix minutes par défaut. Une correction tapée depuis un
        téléphone doit être prise en compte pendant qu'on y pense encore : la
        rattacher à la passe nocturne, c'est laisser un souvenir faux nourrir les
        réponses de toute la journée, et douter que la correction ait été reçue.

        Le coût d'une passe à vide est la lecture d'un fichier de quelques lignes,
        et elle n'écrit rien du tout : c'est ce qui permet cette fréquence.
        """
        if self._boite is None:
            return
        # Une valeur inférieure à la minute serait une erreur de saisie, pas une
        # intention : on ne relit pas un fichier dix fois par seconde.
        intervalle = max(60, self._settings.obsidian_inbox_interval_minutes * 60)
        while True:
            await asyncio.sleep(intervalle)
            try:
                resultat = await self._boite.traiter()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Boîte de réception échec", error=str(exc))
                continue

            if resultat.erreur:
                self._signaler(
                    f"J'ai lu la boîte de réception mais {resultat.erreur} "
                    "Les consignes ont été appliquées ; c'est la trace écrite qui manque."
                )

            # On confirme ce qui a été fait : sans retour, il faut rouvrir le
            # fichier pour savoir si la consigne a été comprise. Le fichier porte
            # le détail, ce message dit seulement qu'il y a quelque chose à y voir.
            if resultat.appliquees:
                self._signaler(_resume_boite(resultat))
            elif resultat.ignorees or resultat.incomprises:
                self._signaler(
                    f"{resultat.ignorees + resultat.incomprises} consigne(s) de la boîte "
                    "de réception n'ont rien donné. La raison est écrite dans le fichier."
                )


def _resume_boite(resultat: ResultatBoiteReception) -> str:
    """Une phrase qui dit ce qui a changé dans la mémoire, sans jargon."""
    morceaux = [f"{resultat.appliquees} consigne(s) appliquée(s)"]
    if resultat.retenus:
        morceaux.append(f"{resultat.retenus} nouveau(x) fait(s) retenu(s)")
    if resultat.ignorees:
        morceaux.append(f"{resultat.ignorees} sans effet")
    if resultat.incomprises:
        morceaux.append(f"{resultat.incomprises} incomprise(s)")
    return "Boîte de réception : " + ", ".join(morceaux) + "."

