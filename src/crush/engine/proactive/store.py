# Copyright (C) 2026 Max Ea
# This file is part of CRUSH-OS,   .


"""
InitiativeStore — persistance des initiatives sur le disque.
Format : JSONL dans memory_data/initiatives/
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

from crush.engine.proactive.schemas import ExecutionMode, Initiative, InitiativeType, Priority
from crush.engine.vocab import AutonomyLevel
from crush.kernel.paths import MEMORY_DATA_DIR


def _title_key(title: str) -> str:
    return re.sub(r"\W+", "", title.lower())


def _jaccard(a: str, b: str) -> float:
    wa = set(re.findall(r"\w+", a.lower()))
    wb = set(re.findall(r"\w+", b.lower()))
    if not wa and not wb:
        return 1.0
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _shares_keyword(a: str, b: str, min_len: int = 7) -> bool:
    """True if both titles share at least one meaningful word of length ≥ min_len."""
    wa = {w for w in re.findall(r"\w+", a.lower()) if len(w) >= min_len}
    wb = {w for w in re.findall(r"\w+", b.lower()) if len(w) >= min_len}
    return bool(wa & wb)


# Mots qui ne portent aucune information distinctive dans un titre d'initiative.
# Sans cette liste, « Météo : Pluie à 18h00 » et « Pluie imminente à 18h »
# partagent surtout « à », ce qui gonfle l'union et fait chuter la similarité.
_MOTS_VIDES = frozenset(
    """a au aux avec ce ces dans de des du en et la le les mais ne ou par
       pas pour que qui sa se ses son sur ta te tes ton un une vers ton
       ta tu il elle on nous vous ils elles est sont etre avoir plus moins
       tres deja encore alors donc si non oui ton""".split()
)


def _mots_significatifs(titre: str) -> set[str]:
    """Les mots qui distinguent vraiment un titre d'un autre.

    Les CHIFFRES sont retirés, et c'est le point important : « Pluie à 18h00
    (85%) » et « Pluie imminente (98%) à 18h » ne partageaient presque rien
    parce que « 85 », « 98 », « 18h00 » et « 18h » comptaient comme des mots
    distincts. Or ce sont les mêmes prévisions à deux mesures près.

    Les accents sont retirés aussi : « météo » et « meteo » sortent tous deux
    d'un modèle de langage selon l'humeur du moment.
    """
    sans_accent = unicodedata.normalize("NFKD", titre.lower())
    sans_accent = "".join(c for c in sans_accent if not unicodedata.combining(c))
    mots = re.findall(r"[a-z]{2,}", sans_accent)
    return {m for m in mots if m not in _MOTS_VIDES}


def _contenance(a: set[str], b: set[str]) -> float:
    """Part du plus PETIT ensemble qui se retrouve dans l'autre.

    Jaccard punit la différence de longueur : un titre court entièrement contenu
    dans un titre long obtient un score bas alors qu'il dit la même chose. La
    contenance répond à la vraie question — « l'un est-il l'autre en plus
    court ? ».
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _similar(a: str, b: str, type_a: str = "", type_b: str = "") -> bool:
    """Deux initiatives disent-elles la même chose ?

    PRUDENCE DÉLIBÉRÉE. Fusionner à tort fait DISPARAÎTRE une initiative que
    l'utilisateur n'aura jamais vue ; laisser passer un doublon l'agace. Les deux
    ne coûtent pas la même chose, donc aucun critère ne se déclenche sur un seul
    mot partagé — la contenance forte n'est retenue que si les deux titres sont
    du même TYPE, c'est-à-dire nés du même genre de déclencheur.
    """
    if _title_key(a) == _title_key(b):
        return True
    ma, mb = _mots_significatifs(a), _mots_significatifs(b)
    if _jaccard(a, b) >= 0.35 or _shares_keyword(a, b):
        return True
    # Titres courts et de même nature : deux façons de dire la même alerte.
    memes_types = bool(type_a) and type_a == type_b
    if memes_types and min(len(ma), len(mb)) <= 5 and _contenance(ma, mb) >= 0.5:
        return True
    return False


def _type_de(initiative: object) -> str:
    brut = getattr(initiative, "type", "")
    return str(getattr(brut, "value", brut))


def _dedup_initiatives(initiatives: list) -> list:
    """Keep the oldest initiative when two titles are semantically similar."""
    kept: list = []
    for candidate in initiatives:
        for existing in kept:
            if _similar(
                candidate.title, existing.title, _type_de(candidate), _type_de(existing)
            ):
                break
        else:
            kept.append(candidate)
    return kept


INITIATIVES_DIR = MEMORY_DATA_DIR / "initiatives"


class InitiativeStore:
    def __init__(self) -> None:
        INITIATIVES_DIR.mkdir(parents=True, exist_ok=True)

    # ── Helpers privés ────────────────────────────────────────────────────────

    def _days_files(self, days: int) -> list[Path]:
        """Retourne les fichiers JSONL des N derniers jours CALENDAIRES,
        triés du plus ancien au plus récent."""
        from datetime import date, timedelta

        cutoff = (date.today() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        return sorted(f for f in INITIATIVES_DIR.glob("*.jsonl") if f.stem >= cutoff)

    def _parse_initiative(self, data: dict) -> Initiative:
        # PHASE 6 — nouveaux champs avec .get(...) defaults pour compat JSONL legacy.
        deadline_str = data.get("deadline")
        return Initiative(
            id=data["id"],
            type=InitiativeType(data["type"]),
            title=data["title"],
            context=data["context"],
            reasoning=data["reasoning"],
            action=data["action"],
            priority=Priority(data["priority"]),
            execution_mode=ExecutionMode(data["execution_mode"]),
            draft_content=data.get("draft_content"),
            mission_description=data.get("mission_description"),
            status=data.get("status", "pending"),
            created_at=datetime.fromisoformat(data["created_at"]),
            autonomy_level=AutonomyLevel(
                int(data.get("autonomy_level", int(AutonomyLevel.SUGGEST)))
            ),
            permission_required=data.get("permission_required", "agent_mission"),
            cost_max_usd=data.get("cost_max_usd"),
            risk=data.get("risk", "low"),
            deadline=datetime.fromisoformat(deadline_str) if deadline_str else None,
            next_action=data.get("next_action", ""),
            requires_validation=bool(data.get("requires_validation", False)),
        )

    def _find_file_for_id(self, initiative_id: str, days: int = 7) -> Path | None:
        """Retourne le fichier JSONL qui contient l'initiative, ou None."""
        for f in reversed(self._days_files(days)):
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line:
                    continue
                try:
                    if json.loads(line).get("id") == initiative_id:
                        return f
                except Exception:
                    pass
        return None

    def _all_pending_titles(self) -> list[str]:
        """Collect titles of all pending initiatives across the last 7 days."""
        titles = []
        for f in self._days_files(7):
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if d.get("status") == "pending":
                        titles.append(d.get("title", ""))
                except Exception:
                    pass
        return titles

    # ── Écriture ──────────────────────────────────────────────────────────────

    def save(self, initiative: Initiative) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = INITIATIVES_DIR / f"{today}.jsonl"

        # Dédup cross-cycle sur les 7 derniers jours
        for etitle in self._all_pending_titles():
            if _similar(initiative.title, etitle):
                return

        with log_file.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "id": initiative.id,
                        "type": initiative.type,
                        "title": initiative.title,
                        "context": initiative.context,
                        "reasoning": initiative.reasoning,
                        "action": initiative.action,
                        "priority": initiative.priority,
                        "execution_mode": initiative.execution_mode,
                        "draft_content": initiative.draft_content,
                        "mission_description": initiative.mission_description,
                        "status": initiative.status,
                        "created_at": initiative.created_at.isoformat(),
                        # PHASE 6 — champs gouvernance §10.1
                        "autonomy_level": int(initiative.autonomy_level),
                        "permission_required": initiative.permission_required,
                        "cost_max_usd": initiative.cost_max_usd,
                        "risk": initiative.risk,
                        "deadline": (
                            initiative.deadline.isoformat() if initiative.deadline else None
                        ),
                        "next_action": initiative.next_action,
                        "requires_validation": initiative.requires_validation,
                    }
                )
                + "\n"
            )

    # ── Lecture ───────────────────────────────────────────────────────────────

    def load_pending(self) -> list[Initiative]:
        """Charge toutes les initiatives en attente du jour, dédupliquées."""
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = INITIATIVES_DIR / f"{today}.jsonl"

        if not log_file.exists():
            return []

        initiatives = []
        for line in log_file.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
                if data.get("status") == "pending":
                    initiatives.append(self._parse_initiative(data))
            except Exception:
                pass

        return _dedup_initiatives(initiatives)

    def load_pending_all(self, days: int = 7) -> list[Initiative]:
        """Charge toutes les initiatives 'pending' des N derniers jours, dédupliquées."""
        all_initiatives: list[Initiative] = []
        for f in self._days_files(days):
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if data.get("status") == "pending":
                        all_initiatives.append(self._parse_initiative(data))
                except Exception:
                    pass
        return _dedup_initiatives(all_initiatives)

    def list_recent(self, days: int = 7, statuses: list[str] | None = None) -> list[Initiative]:
        """Retourne les initiatives des N derniers jours filtrées par statut (tous si None)."""
        all_items: list[Initiative] = []
        target = set(statuses) if statuses else None
        for f in self._days_files(days):
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if target is None or data.get("status") in target:
                        all_items.append(self._parse_initiative(data))
                except Exception:
                    pass
        return all_items

    def get_by_id(self, initiative_id: str, days: int = 7) -> Initiative | None:
        """Recherche une initiative par ID sur les N derniers jours (plus récent en premier)."""
        for f in reversed(self._days_files(days)):
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if data.get("id") == initiative_id:
                        return self._parse_initiative(data)
                except Exception:
                    pass
        return None

    # ── Mise à jour ───────────────────────────────────────────────────────────

    def update_initiative(self, initiative_id: str, updates: dict) -> None:
        """Met à jour les champs d'une initiative existante (cherche dans N derniers jours)."""
        log_file = self._find_file_for_id(initiative_id)
        if not log_file:
            return

        lines = log_file.read_text(encoding="utf-8").strip().split("\n")
        updated = []
        for line in lines:
            if not line:
                continue
            try:
                data = json.loads(line)
                if data.get("id") == initiative_id:
                    data.update(updates)
                line = json.dumps(data)
            except Exception:
                pass
            updated.append(line)

        log_file.write_text("\n".join(updated) + "\n", encoding="utf-8")

    def expirer(self, jours: int = 5) -> int:
        """Marque `expired` ce qui attend depuis trop longtemps. Rend le compte.

        POURQUOI CE N'ÉTAIT PAS DÉJÀ LE CAS

        `load_pending_all(days=7)` ne lit que les sept derniers jours : une
        initiative jamais tranchée sortait donc de la liste au huitième jour,
        sans que rien ne le dise et sans que son statut change. Elle restait
        `pending` pour l'éternité dans un fichier que plus personne ne lit.

        Deux conséquences, toutes deux mauvaises : on ne pouvait pas savoir
        combien de questions étaient restées sans réponse, et le taux de rejet
        mesuré ignorait tout ce qui s'était évaporé. Un statut explicite rend
        les deux visibles.
        """
        limite = datetime.now() - timedelta(days=jours)
        expirees = 0
        for f in self._days_files(30):
            lignes = f.read_text(encoding="utf-8").splitlines()
            sorties: list[str] = []
            touche = False
            for ligne in lignes:
                if not ligne:
                    continue
                try:
                    data = json.loads(ligne)
                    if data.get("status") == "pending":
                        cree = datetime.fromisoformat(data["created_at"])
                        if cree < limite:
                            data["status"] = "expired"
                            expirees += 1
                            touche = True
                    ligne = json.dumps(data)
                except Exception:  # noqa: BLE001 — une ligne illisible est recopiee
                    pass
                sorties.append(ligne)
            if touche:
                f.write_text("\n".join(sorties) + "\n", encoding="utf-8")
        if expirees:
            logger.info("Initiatives expirees", nombre=expirees, apres_jours=jours)
        return expirees

    def resume_pour_generateur(self, jours: int = 7, par_categorie: int = 14) -> dict[str, list]:
        """Ce que le générateur doit savoir de son propre passé.

        C'ÉTAIT LE TROU CENTRAL. Le générateur repartait de l'état du monde seul,
        toutes les trois heures, sans savoir ce qu'il avait déjà proposé ni ce qui
        avait été rejeté. Il redécouvrait donc les mêmes choses — d'où deux
        alertes pour la même pluie et trois initiatives pour le même projet — et
        reproposait indéfiniment des genres que l'utilisateur écarte
        systématiquement. Sur sept jours : 44 rejets sur 76 propositions.

        Rendu en TITRES et non en objets complets : c'est ce qui part dans un
        prompt, et le contexte se paie.
        """
        en_attente: list[str] = []
        rejetes: list[str] = []
        approuves: list[str] = []
        for item in self.list_recent(days=jours):
            statut = str(getattr(item, "status", ""))
            titre = str(getattr(item, "title", "")).strip()
            if not titre:
                continue
            if statut == "pending":
                en_attente.append(titre)
            elif statut in ("rejected", "dismissed"):
                rejetes.append(titre)
            elif statut == "approved":
                approuves.append(titre)
        return {
            # Les plus récents d'abord : un rejet d'hier renseigne mieux qu'un
            # rejet de la semaine dernière.
            "en_attente": en_attente[-par_categorie:],
            "rejetes": rejetes[-par_categorie:],
            "approuves": approuves[-par_categorie:],
        }

    def update_status(self, initiative_id: str, status: str) -> None:
        """Met à jour le statut d'une initiative (cherche dans N derniers jours)."""
        log_file = self._find_file_for_id(initiative_id)
        if not log_file:
            return

        lines = log_file.read_text(encoding="utf-8").strip().split("\n")
        updated = []
        for line in lines:
            if not line:
                continue
            try:
                data = json.loads(line)
                if data.get("id") == initiative_id:
                    data["status"] = status
                line = json.dumps(data)
            except Exception:
                pass
            updated.append(line)

        log_file.write_text("\n".join(updated) + "\n", encoding="utf-8")
