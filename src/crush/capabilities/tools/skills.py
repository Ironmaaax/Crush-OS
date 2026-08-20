# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Outils LLM pour la gestion des skills Crush (création, amélioration, liste)."""

from __future__ import annotations

import difflib
from pathlib import Path

import yaml
from loguru import logger

from crush.capabilities.skills import synthesizer as synthesizer_module
from crush.capabilities.skills.lab import SkillLab
from crush.capabilities.skills.lifecycle import SkillLifecycle
from crush.capabilities.skills.registry import skill_registry

# Même règle de nommage que l'installateur agentskills : deux définitions du
# « nom valide » finiraient par diverger, et c'est celle-ci qui décide si
# l'argument du modèle peut devenir un segment de chemin.
from crush.capabilities.skills.standard import _is_valid_name
from crush.capabilities.skills.synthesizer import SkillSynthesizer
from crush.capabilities.tools.base import Tool, ToolResult
from crush.kernel.paths import SKILLS_CANDIDATES_DIR
from crush.kernel.schemas import SkillRecord, SkillStatus
from crush.kernel.settings import settings

# Le générateur ne réclame que 1 à 3 phrases, et lui, il ne tronque pas ce
# champ : sans borne ici, un historique collé en entier part tel quel dans
# l'appel LLM.
_TACHE_MAX_CARACTERES = 2000
# La description d'une candidate sort d'un skill.yaml ecrit par le LLM dans la
# zone tampon, et se retrouve dans le contexte du modele juste au-dessus du
# lien de promotion. Non bornee, elle pouvait noyer la reponse de skill_list ;
# le prompt de synthese demande 200 caracteres mais rien ne l'imposait.
_DESCRIPTION_CANDIDATE_MAX = 300

# Notes du Lab qui désignent le banc de test lui-même et non la candidate.
# Les confondre fait chercher un défaut dans un code qui n'a rien fait de mal
# — c'est le cas quand la sandbox ne voit pas les dépendances de Crush.
# Le Lab ne persiste que la note, pas son drapeau `environment_error` : la
# reconnaissance se fait donc sur le texte, couche d'abord. Les deux
# formulations du Lab sont gardées, l'ancienne survivant dans les lignes de
# lifecycle déjà écrites.
# « parse » est volontairement ABSENT : cette couche se declenche des que la
# sortie du banc n'est pas du JSON, ce qu'une candidate provoque en ecrivant
# simplement sur stdout. L'y inclure laissait le code fautif se disculper
# lui-meme et envoyait l'utilisateur reparer une machine saine. Meme
# definition que _ENVIRONMENT_LAYERS dans skills/lab.py, qui fait foi.
_COUCHES_ENVIRONNEMENT = frozenset({"sandbox_env", "sandbox_error"})
_SIGNATURES_ENVIRONNEMENT = (
    "environnement sandbox incomplet",
    "skillbase indisponible dans la sandbox",
    "erreur infrastructure sandbox",
    "sortie sandbox non-json",
)


# ── Lecture des notes du Lab ──────────────────────────────────────────────────


def _couche_et_detail(notes: str | None) -> tuple[str, str]:
    """Sépare le tag `[couche]` que le Lab préfixe à ses notes du reste."""
    texte = (notes or "").strip()
    if texte.startswith("[") and "]" in texte:
        fin = texte.index("]")
        return texte[1:fin].strip(), texte[fin + 1 :].strip()
    return "", texte


def _est_panne_d_environnement(couche: str, detail: str) -> bool:
    if couche in _COUCHES_ENVIRONNEMENT:
        return True
    minuscules = detail.lower()
    return any(signature in minuscules for signature in _SIGNATURES_ENVIRONNEMENT)


def _tags_lisibles(valeur: object) -> list[str]:
    """Les tags viennent d'un YAML écrit par un LLM : tout sauf une liste arrive."""
    if not isinstance(valeur, list):
        return []
    return [str(tag) for tag in valeur]


def _entrees_exploitables(valeur: object, cle_obligatoire: str = "") -> list[dict]:
    """Ne garde que ce que le synthesizer sait relire.

    Ces listes arrivent d'un JSON produit par le modèle. Une chaîne à la place
    d'une liste, ou une entrée sans la clé attendue, fait lever le synthesizer
    trois couches plus bas, sur un message qui ne désigne plus l'appel fautif.
    """
    if not isinstance(valeur, list):
        return []
    return [
        entree
        for entree in valeur
        if isinstance(entree, dict) and (not cle_obligatoire or cle_obligatoire in entree)
    ]


class SkillCreateTool(Tool):
    """Propose une nouvelle skill candidate via le SkillLab (PHASE 4).

    Le LLM ne peut PLUS installer une skill directement : ce tool passe
    obligatoirement par `SkillLab.propose_from_trajectory()` qui écrit en
    zone tampon `skills/candidates/{name}/` ET lance le test sandbox.
    La promotion vers `skills/installed/` exige une validation humaine
    explicite via l'endpoint `POST /api/skills/lab/{name}/promote`.
    """

    name = "skill_create"
    description = (
        "Propose une nouvelle skill Crush CANDIDATE depuis une tâche accomplie. "
        "La skill est générée puis testée en sandbox automatique. "
        "Elle N'EST PAS installée tant qu'un humain ne l'a pas validée via "
        "l'endpoint /api/skills/lab/{name}/promote — c'est intentionnel pour "
        "éviter qu'un agent installe du code arbitraire dans le système. "
        "Appeler après avoir réussi une tâche non-triviale et répétable pour "
        "soumettre le savoir-faire à la validation."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "task_description": {
                "type": "string",
                "description": (
                    "Description concise de la tâche accomplie (1-3 phrases). "
                    "Obligatoire et non vide : c'est le seul matériau du "
                    f"générateur. Tronquée au-delà de {_TACHE_MAX_CARACTERES} caractères."
                ),
            },
            "messages": {
                "type": "array",
                "description": (
                    "Extrait de l'historique de conversation (liste de {role, content}). "
                    "Les entrées sans clé 'role' sont ignorées."
                ),
                "items": {"type": "object"},
            },
            "tool_calls": {
                "type": "array",
                "description": "Outils utilisés pendant la tâche (liste de {name, result}).",
                "items": {"type": "object"},
            },
            "result": {
                "type": "string",
                "description": "Résultat ou livrable final de la tâche.",
            },
        },
        "required": ["task_description"],
    }

    def __init__(self, lab: SkillLab) -> None:
        # Lab requis : aucun chemin sans gate. Pas de fallback "construct
        # default" pour éviter qu'un appelant oublie l'injection et bypass
        # accidentellement le sandbox.
        self._lab = lab

    async def execute(  # type: ignore[override]
        self,
        task_description: str,
        messages: list[dict] | None = None,
        tool_calls: list[dict] | None = None,
        result: str = "",
    ) -> ToolResult:
        tache = str(task_description or "").strip()
        if not tache:
            return ToolResult(
                content=(
                    "task_description est vide : il n'y a rien à capitaliser. "
                    "Décrire en 1 à 3 phrases ce qui a été fait, avec quels outils "
                    "et pour quel résultat — c'est le seul matériau du générateur. "
                    "Aucun appel LLM lancé, aucune skill créée."
                ),
                is_error=True,
            )
        tronquee = len(tache) > _TACHE_MAX_CARACTERES
        tache = tache[:_TACHE_MAX_CARACTERES]

        trajectory: dict = {
            "task_description": tache,
            "messages": _entrees_exploitables(messages, "role"),
            "tool_calls": _entrees_exploitables(tool_calls),
            "result": str(result or ""),
        }
        try:
            record = await self._lab.propose_from_trajectory(trajectory)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"Erreur Lab : {exc}", is_error=True)

        if record is None:
            return ToolResult(
                content=(
                    "Aucune candidate produite, rien n'a été installé. Deux causes "
                    "possibles : le générateur n'a pas rendu de SKILL.md exploitable "
                    "(LLM indisponible, ou champ 'name' kebab-case absent du "
                    "frontmatter), ou les fichiers de la candidate n'ont pas été "
                    "écrits sur disque. Le journal du Lab porte la cause exacte. "
                    "Réessayer avec une task_description plus explicite."
                ),
                is_error=True,
            )

        suffixe_troncature = (
            f" (task_description tronquée à {_TACHE_MAX_CARACTERES} caractères)"
            if tronquee
            else ""
        )
        return self._verdict_en_clair(record, suffixe_troncature)

    @staticmethod
    def _verdict_en_clair(record: SkillRecord, suffixe_troncature: str) -> ToolResult:
        """Traduit le statut et les notes du Lab en une suite à donner.

        Un rejet n'appelle pas la même action selon sa cause : corriger le code
        généré, réparer la machine, ou simplement relancer. Rendre les trois
        sous la même phrase revient à n'en rendre aucune.
        """
        if record.status == SkillStatus.SANDBOXED_PASS:
            return ToolResult(
                content=(
                    f"Skill candidate '{record.name}' générée, contrôles automatiques "
                    "passés"
                    f"{suffixe_troncature}. "
                    f"En attente de validation humaine "
                    f"(POST /api/skills/lab/{record.name}/promote, "
                    f"refus : POST /api/skills/lab/{record.name}/reject). "
                    f"La skill n'est PAS installée tant que la validation "
                    f"n'a pas eu lieu : inutile de la reproposer, et prévenir "
                    f"l'utilisateur qu'elle attend son accord."
                )
            )

        if record.status != SkillStatus.SANDBOXED_FAIL:
            # Le Lab n'a pas rendu son verdict : le dire, plutôt que de faire
            # passer un état intermédiaire pour un rejet.
            return ToolResult(
                content=(
                    f"Skill candidate '{record.name}' laissée en statut "
                    f"'{record.status.value}' : le gate sandbox n'a pas rendu de "
                    f"verdict. Aucune installation. Détail : "
                    f"{record.sandbox_notes or '(aucune note)'}."
                ),
                is_error=True,
            )

        couche, detail = _couche_et_detail(record.sandbox_notes)
        entete = (
            f"Skill candidate '{record.name}' REJETÉE par le gate sandbox, "
            f"aucune installation{suffixe_troncature}."
        )
        if couche == "timeout":
            return ToolResult(
                content=(
                    f"{entete} Cause : le test ne s'est pas terminé "
                    f"({detail or 'délai dépassé'}). Soit le code généré boucle, "
                    f"soit la machine était saturée — relancer une fois avant de "
                    f"conclure que la skill est fautive."
                ),
                is_error=True,
            )
        if _est_panne_d_environnement(couche, detail):
            return ToolResult(
                content=(
                    f"{entete} Cause : le banc de test lui-même est cassé, la skill "
                    f"n'est PAS en cause — rien à corriger dans le code généré. "
                    f"Détail : {detail or '(non précisé)'}. "
                    f"Les fichiers de la candidate restent en zone tampon ; "
                    f"relancer skill_create une fois l'environnement de test réparé, "
                    f"et signaler la panne à l'utilisateur."
                ),
                is_error=True,
            )
        etape = f" à l'étape {couche}" if couche else ""
        return ToolResult(
            content=(
                f"{entete} Cause : le code généré est fautif{etape}. "
                f"Détail : {detail or '(détail manquant)'}. "
                f"Relancer avec une description plus précise si le savoir-faire "
                f"mérite d'être capitalisé."
            ),
            is_error=True,
        )


class SkillImproveTool(Tool):
    """Améliore un skill existant à partir d'une nouvelle expérience."""

    name = "skill_improve"
    description = (
        "Affine et améliore un skill Crush DÉJÀ INSTALLÉ avec une nouvelle expérience. "
        "Appeler quand une tâche déjà couverte par un skill a révélé des cas "
        "non gérés, des meilleures pratiques ou des corrections utiles. "
        "Utiliser skill_list d'abord pour connaître les noms exacts : une "
        "candidate en attente de validation ne peut pas être améliorée par ici."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": (
                    "Nom kebab-case d'un skill installé (ex: 'web-research'). "
                    "Ni chemin, ni séparateur : le nom seul."
                ),
            },
            "new_experience": {
                "type": "string",
                "description": (
                    "Description de la nouvelle expérience à intégrer : "
                    "ce qui a changé, ce qui a mieux fonctionné, les cas limites découverts."
                ),
            },
        },
        "required": ["skill_name", "new_experience"],
    }

    def __init__(self, synthesizer: SkillSynthesizer) -> None:
        self._synthesizer = synthesizer

    @staticmethod
    def _skills_ameliorables() -> list[str]:
        """Noms des skills que `improve_skill` peut réellement relire.

        Volontairement lu depuis le module du synthesizer et non depuis le
        registre : le registre expose aussi la zone dev et les skills sans
        SKILL.md, que l'amélioration ne sait pas ouvrir. Annoncer un nom qu'on
        refusera ensuite vaut à peine mieux que ne rien annoncer.
        """
        racine: Path = synthesizer_module.SKILLS_INSTALLED_DIR
        try:
            return sorted(
                dossier.name for dossier in racine.iterdir() if (dossier / "SKILL.md").exists()
            )
        except OSError:
            return []

    @classmethod
    def _inventaire(cls, skill_name: str) -> str:
        installes = cls._skills_ameliorables()
        if not installes:
            return (
                "Aucun skill n'est installé : il n'y a rien à améliorer. "
                "Passer par skill_create pour proposer une nouvelle skill "
                "(elle attendra une validation humaine avant installation)."
            )
        proches = difflib.get_close_matches(skill_name, installes, n=2, cutoff=0.6)
        suggestion = ""
        if proches:
            suggestion = f" Vouliez-vous dire {' ou '.join(repr(p) for p in proches)} ?"
        return f"Skills installés ({len(installes)}) : {', '.join(installes)}.{suggestion}"

    async def execute(  # type: ignore[override]
        self,
        skill_name: str,
        new_experience: str,
    ) -> ToolResult:
        nom = str(skill_name or "").strip()
        # Le nom devient un segment de chemin sous skills/installed/ : sans ce
        # filtre, '../candidates/x' laisse réécrire une candidate déjà validée
        # en sandbox, donc contourner le gate au moment où l'humain l'approuve.
        if not _is_valid_name(nom):
            return ToolResult(
                content=(
                    f"Nom de skill invalide : '{skill_name}'. Attendu : kebab-case "
                    f"seul, 1-64 caractères (minuscules, chiffres, tirets simples), "
                    f"sans chemin ni séparateur. {self._inventaire(nom)}"
                ),
                is_error=True,
            )
        if not str(new_experience or "").strip():
            return ToolResult(
                content=(
                    f"new_experience est vide : rien à intégrer dans '{nom}'. "
                    f"Décrire ce que la dernière exécution a appris — cas non géré, "
                    f"correction, meilleure pratique. Skill inchangé."
                ),
                is_error=True,
            )

        try:
            await self._synthesizer.improve_skill(nom, new_experience)
        except FileNotFoundError as exc:
            return ToolResult(content=f"{exc}. {self._inventaire(nom)}", is_error=True)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"Erreur amélioration de '{nom}' : {exc}", is_error=True)

        # L'amélioration n'existe que sur disque : l'instance chargée garde son
        # ancien prompt jusqu'au prochain démarrage. Sans ce rechargement,
        # l'outil annonce un effet que la conversation suivante ne verra pas.
        # Même geste que l'installateur après une pose de skill.
        try:
            # Cible, pas global : reload() relirait tout installed/ et
            # executerait chaque skill.py, sur une action que le modele
            # declenche. On ne recharge que celui qu'on vient d'ecrire.
            skill_registry.reload_one(nom)
        except Exception as exc:  # noqa: BLE001
            logger.warning("skill_improve : rechargement du registre échoué ({})", exc)
            return ToolResult(
                content=(
                    f"Skill '{nom}' amélioré sur disque, mais le registre n'a pas pu "
                    f"être rechargé ({exc}) : la version en mémoire reste l'ancienne "
                    f"jusqu'au redémarrage de Crush."
                )
            )
        return ToolResult(
            content=f"Skill '{nom}' amélioré avec la nouvelle expérience, et rechargé."
        )


class SkillListTool(Tool):
    """Liste les skills installés et les candidates en attente de validation."""

    name = "skill_list"
    description = (
        "Liste les skills Crush : ceux qui sont INSTALLÉS et utilisables, et les "
        "CANDIDATES qui ont passé le test sandbox mais attendent l'accord explicite "
        "de l'utilisateur pour être installées. Utiliser avant de proposer un "
        "nouveau skill (pour ne pas dupliquer une candidate déjà en attente) et "
        "pour dire à l'utilisateur ce qui attend sa validation."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "filter_tag": {
                "type": "string",
                "description": "Filtrer par tag (optionnel). Ex: 'research', 'coding'.",
            },
        },
        "required": [],
    }

    def __init__(
        self,
        lifecycle: SkillLifecycle | None = None,
        candidates_dir: Path | None = None,
    ) -> None:
        # Injection optionnelle : bootstrap.py construit l'outil sans argument,
        # et un outil de lecture ne doit pas devenir impossible à enregistrer
        # pour autant. À défaut, le lifecycle est ouvert à la demande et
        # seulement s'il existe déjà — pas de base créée depuis un tool.
        self._lifecycle = lifecycle
        self._candidates_dir = Path(candidates_dir) if candidates_dir else SKILLS_CANDIDATES_DIR

    def _lifecycle_utilisable(self) -> SkillLifecycle | None:
        if self._lifecycle is not None:
            return self._lifecycle
        # Même expression que bootstrap.py : deux façons de nommer la base
        # finiraient par désigner deux fichiers différents.
        base = Path(settings.memory_dir) / "crush_memory.db"
        if not base.exists():
            return None
        try:
            self._lifecycle = SkillLifecycle(db_path=base)
        except Exception as exc:  # noqa: BLE001 — une liste ne doit jamais planter
            logger.warning("skill_list : lifecycle illisible ({})", exc)
            return None
        return self._lifecycle

    def _fiche_candidate(self, nom: str) -> tuple[str, list[str]]:
        """Description et tags lus dans la zone tampon, au mieux.

        Ce skill.yaml sort d'un LLM : il peut manquer, être tronqué ou mal
        typé. Une liste ne doit pas disparaître parce qu'une de ses lignes
        est illisible.
        """
        fichier = self._candidates_dir / nom / "skill.yaml"
        try:
            donnees = yaml.safe_load(fichier.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError, yaml.YAMLError):
            return "", []
        if not isinstance(donnees, dict):
            return "", []
        description = str(donnees.get("description", "")).strip()
        if len(description) > _DESCRIPTION_CANDIDATE_MAX:
            description = description[:_DESCRIPTION_CANDIDATE_MAX].rstrip() + "…"
        return description, _tags_lisibles(donnees.get("tags"))

    def _candidates_en_attente(self, filter_tag: str) -> list[tuple[str, str]]:
        lifecycle = self._lifecycle_utilisable()
        if lifecycle is None:
            return []
        try:
            records = lifecycle.list_by_status(SkillStatus.SANDBOXED_PASS)
        except Exception as exc:  # noqa: BLE001 — une liste ne doit jamais planter
            logger.warning("skill_list : candidates illisibles ({})", exc)
            return []
        en_attente = []
        for record in records:
            description, tags = self._fiche_candidate(record.name)
            if filter_tag and filter_tag.lower() not in [t.lower() for t in tags]:
                continue
            en_attente.append((record.name, description))
        return en_attente

    async def execute(self, filter_tag: str = "") -> ToolResult:  # type: ignore[override]
        filtre = str(filter_tag or "").strip()
        skills = skill_registry.list_installed()
        if filtre:
            skills = [
                s
                for s in skills
                if filtre.lower() in [t.lower() for t in _tags_lisibles(s.get("tags"))]
            ]

        blocs: list[str] = []
        if skills:
            lignes = [f"## Skills installés ({len(skills)})"]
            for s in skills:
                tags_str = ", ".join(_tags_lisibles(s.get("tags"))) or "—"
                lignes.append(
                    f"**{s.get('name', '?')}** v{s.get('version', '?')} — "
                    f"{s.get('description', '')}\n"
                    f"  Tags : {tags_str} | Type : {s.get('type', 'conversational')}"
                )
            blocs.append("\n\n".join(lignes))
        else:
            blocs.append(
                "Aucun skill installé" + (f" avec le tag '{filtre}'" if filtre else "") + "."
            )

        # Sans cette section, une candidate validée en sandbox attend
        # indéfiniment : l'utilisateur est le seul à pouvoir l'installer, et
        # rien d'autre ne peut le lui apprendre au fil d'une conversation.
        attente = self._candidates_en_attente(filtre)
        if attente:
            lignes = [
                f"## En attente de validation humaine ({len(attente)})",
                "Contrôles automatiques passés, mais PAS installées : elles ne "
                "servent à rien tant que l'utilisateur ne les a pas acceptées.",
            ]
            for nom, description in attente:
                lignes.append(
                    f"**{nom}** — {description or '(pas de description)'}\n"
                    f"  Accepter : POST /api/skills/lab/{nom}/promote\n"
                    f"  Refuser : POST /api/skills/lab/{nom}/reject"
                )
            blocs.append("\n\n".join(lignes))

        return ToolResult(content="\n\n".join(blocs))
