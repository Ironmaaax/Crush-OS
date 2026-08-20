# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Outil execute_preset — permet à Crush de lancer un preset.

« Preset introuvable » recouvrait deux situations opposées : un nom mal
orthographié parmi des presets existants, et un système où AUCUN preset n'est
installé. La première invite à réessayer, la seconde à installer quelque chose.
Les distinguer évite au modèle de deviner des noms à l'infini.
"""

from __future__ import annotations

from difflib import get_close_matches

from crush.capabilities.skills.base import SkillBase
from crush.capabilities.skills.executor import PresetExecutor
from crush.capabilities.skills.registry import skill_registry
from crush.capabilities.tools.base import Tool, ToolResult
from crush.capabilities.tools.registry import ToolRegistry
from crush.kernel.contracts import TTSEngine
from crush.kernel.notifications import broadcast_event
from crush.kernel.paths import SKILLS_INSTALLED_DIR


def _comment_installer() -> str:
    """Le remède quand le dossier des presets est vide.

    Le chemin est calculé et non écrit en dur : il dépend de la racine du projet,
    qui n'est pas la même sur la Pi et en développement.
    """
    return (
        f"Un preset est un sous-dossier de {SKILLS_INSTALLED_DIR} contenant un "
        "skill.yaml (avec `type: preset` et une liste `steps:`) et un skill.py. "
        "En développement, ~/.crush/extensions/dev/presets/<nom> fait aussi "
        "l'affaire. Une fois le dossier en place, recharger via "
        "POST /api/skills/reload ou redémarrer le service."
    )


class ExecutePresetTool(Tool):
    name = "execute_preset"
    description = (
        "Lance un preset Crush — séquence d'actions automatisées.\n\n"
        "Utilise cet outil quand l'utilisateur demande de lancer un preset "
        "dont tu connais le nom (via les SYSTEM_PROMPT des skills de type preset).\n\n"
        "Appeler SANS argument pour obtenir la liste des presets installés — "
        "c'est la seule façon fiable de connaître les noms valides.\n\n"
        "Exemples :\n"
        '- "lance le mode streameur" → execute_preset(preset_name="mode-streameur")\n'
        '- "mode travail" → execute_preset(preset_name="mode-travail")\n'
        '- "quels presets ai-je ?" → execute_preset()'
    )
    input_schema = {  # noqa: RUF012
        "type": "object",
        "properties": {
            "preset_name": {
                "type": "string",
                "description": (
                    "Nom du preset à lancer (slug kebab-case). "
                    "Omettre pour lister les presets installés."
                ),
            }
        },
        "required": [],
    }

    def __init__(self, *, tool_registry: ToolRegistry, tts_engine: TTSEngine) -> None:
        self._tool_registry = tool_registry
        self._tts_engine = tts_engine

    async def execute(self, preset_name: str = "", **_: object) -> ToolResult:
        presets = skill_registry.get_presets()
        nom = (preset_name or "").strip()

        # Sans nom : inventaire. Ce n'est pas un échec, même vide — c'est la
        # réponse à « qu'est-ce que je peux lancer ? ».
        if not nom:
            return ToolResult(content=self._inventaire(presets))

        preset = skill_registry.get_preset(nom)
        if not preset:
            return ToolResult(content=self._absence(nom, presets), is_error=True)

        executor = PresetExecutor(
            tool_registry=self._tool_registry,
            tts_engine=self._tts_engine,
        )

        results = await executor.execute(preset, broadcast_fn=broadcast_event)

        # L'exécuteur s'arrête net si une application requise manque : ce motif
        # était noyé dans un « 0 étapes réalisées » qui passait pour un succès.
        if not results.get("success", True) and results.get("error"):
            return ToolResult(
                content=f"Preset '{nom}' non lancée — {results['error']}",
                is_error=True,
            )

        done = results["steps_done"]
        skipped = results["steps_skipped"]
        failed = results["steps_failed"]

        msg = f"Preset '{nom}' exécutée — {done} étapes réalisées"
        if skipped:
            msg += f", {skipped} ignorées (plateforme)"
        if failed:
            msg += f", {failed} en erreur"
            details = [
                f"{log['step']} : {log.get('message') or 'sans détail'}"
                for log in results.get("logs", [])
                if log.get("status") == "failed"
            ]
            if details:
                msg += "\nÉchecs — " + " ; ".join(details)

        # Zéro étape réussie et des erreurs : rien n'a eu lieu, il faut le dire
        # comme un échec plutôt que de laisser croire à une exécution partielle.
        return ToolResult(content=msg, is_error=bool(failed) and done == 0)

    def _inventaire(self, presets: dict[str, SkillBase]) -> str:
        if not presets:
            return "Aucun preset n'est installé sur cette machine.\n" + _comment_installer()
        lignes = [
            f"- {p.name} : {p.description or p.label}" for p in presets.values()
        ]
        return f"Presets installés ({len(presets)}) :\n" + "\n".join(lignes)

    def _absence(self, nom: str, presets: dict[str, SkillBase]) -> str:
        if not presets:
            return (
                f"Aucun preset n'est installé sur cette machine : « {nom} » n'est pas "
                "une faute de frappe, la liste des presets est vide.\n"
                + _comment_installer()
            )
        proches = get_close_matches(nom, list(presets), n=2, cutoff=0.6)
        message = (
            f"Preset '{nom}' introuvable. Presets installés : "
            f"{', '.join(sorted(presets))}."
        )
        if proches:
            message += f" Vouliez-vous dire {' ou '.join(proches)} ?"
        return message
