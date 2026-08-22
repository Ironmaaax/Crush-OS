# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .


from __future__ import annotations

import asyncio
import json
from pathlib import Path

from loguru import logger

from crush.providers.llm.base import LLMProvider
from crush.providers.memory.fact_index import FactIndex
from crush.providers.memory.ingest import IngestResult, MemoryIngest
from crush.providers.memory.kernel import MemoryKernel
from crush.providers.memory.mirror import MemoryMirror

# Plafond du nombre de sessions ingérées par run deep (la plus récente d'abord).
_MAX_SESSIONS_PER_DEEP = 5
# Plafond de caractères par session passée à l'extracteur (les sessions très
# longues sont tronquées à leur tail, qui contient en général le contexte le
# plus récent et le plus actionnable).
_MAX_CHARS_PER_SESSION = 8000

def _default_prefs(name: str) -> str:
    return f"# Préférences {name}\n\nAucune préférence enregistrée.\n"


def _micro_system(name: str, assistant_name: str = "Crush") -> str:
    return (
        f"Tu es un agent de mémorisation pour {assistant_name}. "
        f"Analyse l'échange et mets à jour les préférences de {name} uniquement si tu détectes "
        "une nouvelle préférence explicite (note que, retiens que, j'aime, je préfère…) ou "
        "un signal implicite fort. Retourne uniquement le markdown mis à jour, sans explication. "
        "Si rien à changer, retourne le fichier identique."
    )


def _deep_system(name: str, assistant_name: str = "Crush") -> str:
    return (
        f"Tu es un agent de mémorisation pour {assistant_name}. "
        f"Analyse les sessions fournies et synthétise les apprentissages durables sur {name} "
        "(préférences, habitudes, contexte). "
        "Retourne uniquement le markdown mis à jour des préférences."
    )


def _refus_de_remplacement(ancien: str, nouveau: str) -> str | None:
    """Le nouveau contenu peut-il remplacer les préférences ? Rend la raison du refus.

    POURQUOI CE GARDE-FOU

    `_run_micro` remplaçait TOUT le fichier par la sortie brute du modèle, après
    chaque échange, avec pour seule condition qu'elle soit non vide et
    différente. Une réponse comme « Rien à changer. » satisfait ces deux
    conditions : la phrase DEVENAIT la mémoire de l'utilisateur. Sans sauvegarde,
    et huit à cinquante fois par jour.

    Une mémoire ne se réécrit pas intégralement à chaque phrase. Les trois refus
    ci-dessous encadrent ce qu'un remplacement légitime peut être.
    """
    a, n = ancien.strip(), nouveau.strip()

    # 1. L'effondrement. Une mémoire construite ne perd pas la moitié de sa
    #    substance en un échange — c'est la signature d'un refus du modèle ou
    #    d'une réponse tronquée, pas d'une mise à jour.
    if len(a) >= 120 and len(n) < len(a) * 0.5:
        return f"effondrement de taille ({len(a)} -> {len(n)} octets)"

    # 2. La forme. Le fichier est une liste à puces sous un titre. Une réponse
    #    conversationnelle n'en a ni l'un ni l'autre.
    lignes = [x.strip() for x in n.splitlines() if x.strip()]
    puces = sum(1 for x in lignes if x.startswith(("-", "*", "•")))
    if puces < 2 and not any(x.startswith("#") for x in lignes):
        return "ni titre ni liste à puces — ressemble à une réponse, pas à une mémoire"

    # 3. Les acquiescements. Le modèle répond parfois à la consigne au lieu de
    #    l'exécuter. On les reconnaît explicitement plutôt que d'espérer que les
    #    deux règles précédentes les attrapent toujours.
    debut = n[:60].lower()
    for aveu in ("rien à changer", "rien a changer", "aucun changement", "pas de "):
        if debut.startswith(aveu):
            return f"acquiescement du modèle plutôt qu'un contenu ({aveu!r})"

    return None


class AutoDream:
    """Micro-update fire-and-forget après chaque échange + analyse profonde nocturne à 3h."""

    def __init__(
        self,
        llm: LLMProvider,
        prefs_path: Path,
        sessions_dir: Path,
        memory_ingest: MemoryIngest | None = None,
        mirror: MemoryMirror | None = None,
        kernel: MemoryKernel | None = None,
        fact_index: FactIndex | None = None,
        user_firstname: str = "Max",
        assistant_name: str = "Crush",
    ) -> None:
        self._llm = llm
        self._prefs_path = prefs_path
        self._sessions_dir = sessions_dir
        self._name = user_firstname
        self._assistant_name = assistant_name
        self._micro_system = _micro_system(user_firstname, assistant_name)
        self._deep_system = _deep_system(user_firstname, assistant_name)
        self._ensure_prefs()
        self._mirror = mirror
        # MOUVEMENT 2 (option D, Generative Agents) : l'ingestion Kernel est
        # déclenchée UNIQUEMENT par _run_deep (passe nocturne), et JAMAIS par
        # _run_micro (à chaque message). On évite la double extraction côté
        # ConsolidationAgent + AutoDream micro, et on respecte le principe de
        # synthèse périodique sur la conversation complète.
        # Le hook PHASE 3 dans _run_micro reste présent comme mort code inerte
        # car self._ingest est désormais TOUJOURS None côté micro en pratique
        # (main.py passe None à AutoDream micro, le memory_ingest n'est consommé
        # qu'au deep via _ingest_recent_sessions).
        self._ingest = memory_ingest
        # Passé SÉPARÉMENT de `memory_ingest`, et non lu à travers lui : la
        # maintenance de la base (absorption des doublons) doit tourner même
        # quand `ingest_deep_enabled` est faux, cas où bootstrap ne transmet
        # aucun ingest.
        self._kernel = kernel
        self._fact_index = fact_index

    def _ensure_prefs(self) -> None:
        if not self._prefs_path.exists():
            self._prefs_path.parent.mkdir(parents=True, exist_ok=True)
            self._prefs_path.write_text(_default_prefs(self._name), encoding="utf-8")

    def _read_prefs(self) -> str:
        return self._prefs_path.read_text(encoding="utf-8")

    def _write_prefs(self, content: str) -> None:
        # Une génération conservée avant d'écraser. Les garde-fous de
        # `_refus_de_remplacement` arrêtent les cas connus, pas les inconnus — et
        # ce fichier est la seule mémoire en prose de l'assistant. Un `.bak`
        # coûte quelques centaines d'octets et rend l'accident réversible.
        if self._prefs_path.exists():
            try:
                self._prefs_path.with_suffix(".md.bak").write_text(
                    self._prefs_path.read_text(encoding="utf-8"), encoding="utf-8"
                )
            # `Exception` et non `OSError` : `read_text(encoding="utf-8")` lève un
            # `UnicodeDecodeError` -- un `ValueError`, pas un `OSError` -- si le
            # fichier contient un octet invalide. Il remontait alors hors de
            # `_write_prefs` et sautait l'écriture finale, donc les préférences
            # n'auraient plus JAMAIS été mises à jour. Exactement l'inverse de ce
            # que le commentaire ci-dessous annonce.
            except Exception as exc:  # noqa: BLE001 — l'écriture prime sur la sauvegarde
                logger.warning("Préférences : sauvegarde impossible", error=str(exc))
        self._prefs_path.write_text(content, encoding="utf-8")

    # ── Micro (fire-and-forget, après chaque échange) ─────────

    def fire_micro(self, user_message: str, assistant_message: str) -> None:
        asyncio.create_task(
            self._run_micro_safe(user_message, assistant_message),
            name="autodream-micro",
        )

    async def _run_micro_safe(self, user_message: str, assistant_message: str) -> None:
        try:
            await self._run_micro(user_message, assistant_message)
        except Exception as e:
            logger.exception("AutoDream micro error", error=str(e))

    async def _run_micro(self, user_message: str, assistant_message: str) -> None:
        prefs = self._read_prefs()
        prompt = (
            f"Préférences actuelles :\n{prefs}\n\n"
            f"Échange :\n{self._name} : {user_message}\nCrush : {assistant_message}"
        )
        result = await self._llm.complete(
            messages=[{"role": "user", "content": prompt}],
            system=self._micro_system,
            stream=False,
            context="memory",
        )
        updated = str(result).strip()
        if updated and updated != prefs.strip():
            refus = _refus_de_remplacement(prefs, updated)
            if refus is not None:
                # On garde l'ancien contenu. Le prochain échange retentera : une
                # réponse aberrante est un accident, pas un état.
                logger.warning(
                    "AutoDream micro: remplacement des préférences REFUSÉ",
                    raison=refus,
                    recu=updated[:120],
                )
            else:
                self._write_prefs(updated)
                logger.info("AutoDream micro: préférences mises à jour")

        # PHASE 3 — Ingestion parallèle dans le Kernel (best-effort, ne bloque pas).
        if self._ingest is not None:
            try:
                await self._ingest.ingest(
                    content=f"{self._name} : {user_message}\nCrush : {assistant_message}",
                    source="auto_dream_micro",
                    event_type="exchange",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("AutoDream micro: ingest Kernel error", error=str(exc))

    # ── Deep (nocturne, appelé par le scheduler à 3h) ─────────

    async def deep_analyze(self) -> None:
        try:
            await self._run_deep()
        except Exception as e:
            logger.exception("AutoDream deep error", error=str(e))

    async def _run_deep(self) -> None:
        """Une nuit de travail : synthétiser ce qui s'est dit, puis entretenir la base.

        Les deux moitiés sont INDÉPENDANTES. La passe sortait tôt quand aucune
        session n'était à analyser, ce qui liait l'entretien à l'activité.

        PORTÉE EXACTE, vérifiée : `_load_recent_sessions` ne filtre pas par date
        — elle prend les cinq derniers fichiers par nom, et `SessionStore` n'en
        supprime jamais. Dès qu'une seule conversation a eu lieu, la sortie
        anticipée est donc morte. Ce n'est PAS « un week-end sans parler » : le
        seul cas concerné est une installation neuve, ou une base migrée, sans
        aucun fichier de session. L'entretien y tournait jamais — et c'est
        justement là qu'une base importée peut contenir des doublons.
        """
        sessions_text = self._load_recent_sessions()
        if sessions_text:
            await self._synthese_nocturne(sessions_text)
        else:
            logger.debug("AutoDream deep: aucune session — entretien seul")

        # 2bis) Absorption des doublons exacts. `find_active_exact` empêche d'en
        # CRÉER, mais la base en contient déjà : mesuré, 9 faits sur 50. Chacun
        # coûte une place du bloc mémoire injecté dans le prompt, qui est
        # plafonné — l'assistant paraissait répéter ce qu'il savait.
        #
        # Avant le miroir, qui doit refléter l'état fusionné.
        if self._kernel is not None:
            try:
                absorbes = self._kernel.fusionner_doublons()
                if absorbes:
                    logger.info("AutoDream deep: doublons absorbés", nombre=absorbes)
            except Exception as exc:  # noqa: BLE001 — l'entretien ne casse pas la passe
                logger.warning("AutoDream deep: fusion des doublons échouée", error=str(exc))

            # Puis les VARIANTES : même idée, formulation différente. Après
            # l'identique et jamais avant — un groupe déjà réduit à un fait ne
            # peut plus être mal rapproché par l'heuristique.
            try:
                variantes = self._kernel.fusionner_variantes()
                if variantes:
                    logger.info("AutoDream deep: variantes absorbées", nombre=variantes)
            except Exception as exc:  # noqa: BLE001
                logger.warning("AutoDream deep: fusion des variantes échouée", error=str(exc))

        # 2ter) Réindexation sémantique des faits. APRÈS les fusions : indexer
        # avant aurait laissé les doublons absorbés trouvables par la recherche
        # alors qu'ils viennent d'être archivés.
        if self._fact_index is not None:
            try:
                indexes = await self._fact_index.synchroniser()
                logger.info("AutoDream deep: faits réindexés", nombre=indexes)
            except Exception as exc:  # noqa: BLE001
                logger.warning("AutoDream deep: réindexation des faits échouée", error=str(exc))

        # 3) Régénération du miroir Markdown (SQLite → MD unidirectionnel, §6.7).
        # Tourne UNIQUEMENT en deep nocturne — c'est l'instant où la base est
        # stable après ingestion. Échec silencieux : le miroir est secondaire.
        if self._mirror is not None:
            try:
                report = self._mirror.export()
                logger.info(
                    "MemoryMirror exporté",
                    files=len(report.files_written),
                    facts=report.facts_exported,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("MemoryMirror export échec", error=str(exc))

    async def _synthese_nocturne(self, sessions_text: str) -> None:
        """Ce qui dépend des sessions de la nuit : la prose de préférences, puis
        l'ingestion structurée. Séparé de l'entretien de la base, qui doit
        tourner même quand personne n'a parlé."""
        # 1) Synthèse texte → user_prefs.md (comportement historique préservé).
        prefs = self._read_prefs()
        prompt = f"Préférences actuelles :\n{prefs}\n\nSessions récentes :\n{sessions_text}"
        result = await self._llm.complete(
            messages=[{"role": "user", "content": prompt}],
            system=self._deep_system,
            stream=False,
            context="memory",
        )
        updated = str(result).strip()
        if updated:
            # MÊME garde-fou que la passe micro. Ne l'avoir mis que là était une
            # correction à moitié : cette passe-ci écrase aussi tout le fichier
            # sur n'importe quelle sortie non vide. Plus rare — une fois par nuit
            # — mais plus coûteuse, puisqu'elle porte la synthèse d'une nuit
            # entière de sessions.
            refus = _refus_de_remplacement(self._read_prefs(), updated)
            if refus is not None:
                logger.warning(
                    "AutoDream deep: remplacement des préférences REFUSÉ",
                    raison=refus,
                    recu=updated[:120],
                )
            else:
                self._write_prefs(updated)
                logger.info("AutoDream deep: préférences mises à jour")

        # 2) Ingestion batch dans le Memory Kernel — UNE extraction par session
        # entière (jamais par message). Le matcher v2 voit l'état cumulé du
        # Kernel à chaque ingest individuel → dédoublonnage intra-batch garanti.
        if self._ingest is not None:
            await self._ingest_recent_sessions()

    # ── Ingestion batch deep ──────────────────────────────────

    def _list_recent_session_files(self) -> list[Path]:
        """Renvoie les N sessions les plus récentes (par mtime, plus récente first).

        On itère ensuite de la plus ancienne à la plus récente pour que les facts
        des sessions anciennes soient en base AVANT l'extraction des nouvelles.
        Le matcher v2 peut alors confirmer/déduplique correctement intra-batch.
        """
        if not self._sessions_dir.exists():
            return []
        files = sorted(self._sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        return files[-_MAX_SESSIONS_PER_DEEP:]

    @staticmethod
    def _session_to_text(path: Path, name: str = "Max", assistant_name: str = "Crush") -> str:
        """Concatène les messages d'une session JSONL en un texte unique.

        Format : alternance '<prénom> : ...' / 'Crush : ...'.
        Le texte complet est passé à l'extracteur en UN SEUL APPEL — l'extracteur
        raisonne sur la session ENTIÈRE, pas message par message.
        """
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        parts: list[str] = []
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            role = obj.get("role", "")
            content = obj.get("content", "")
            if not isinstance(content, str) or not content.strip():
                continue
            speaker = name if role == "user" else assistant_name
            parts.append(f"{speaker} : {content.strip()}")
        text = "\n".join(parts)
        # Tronque au tail si la session est très longue : on garde le contexte
        # le plus récent (où sont les facts les plus actionnables).
        if len(text) > _MAX_CHARS_PER_SESSION:
            text = "...\n" + text[-_MAX_CHARS_PER_SESSION:]
        return text

    async def _ingest_recent_sessions(self) -> list[IngestResult]:
        """Ingère les N dernières sessions, UNE extraction par session entière.

        Renvoie la liste des IngestResult pour permettre une trace d'observation.
        """
        assert self._ingest is not None
        results: list[IngestResult] = []
        files = self._list_recent_session_files()
        for path in files:
            text = self._session_to_text(path, self._name, self._assistant_name)
            if not text.strip():
                continue
            try:
                r = await self._ingest.ingest(
                    content=text,
                    source=f"session:{path.name}",
                    event_type="session_summary",
                )
                results.append(r)
            except Exception as exc:  # noqa: BLE001 — un échec d'ingest ne bloque pas le batch
                logger.warning(
                    "AutoDream deep: ingest session échec",
                    file=path.name,
                    error=str(exc),
                )
        logger.info(
            "AutoDream deep: ingest batch terminé",
            sessions=len(results),
            arbiter_calls=self._ingest.arbiter_calls,
        )
        return results

    def _load_recent_sessions(self) -> str:
        """Compatibilité historique : concat texte des 5 dernières sessions (8000 chars)."""
        if not self._sessions_dir.exists():
            return ""
        files = sorted(self._sessions_dir.glob("*.jsonl"))[-5:]
        parts: list[str] = []
        for f in files:
            try:
                parts.append(f.read_text(encoding="utf-8"))
            except Exception:
                pass
        return "\n".join(parts)[:8000]
