# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .


"""
Outils de délégation — sous-agent isolé et exécution de script via RPC.

SpawnSubagentTool : délègue un workstream à un sous-agent ISOLÉ (contexte propre).
ScriptRPCTool    : execute un script Python qui appelle les outils via RPC —
                   un pipeline de N appels = un seul tour LLM (zéro coût contexte).

Les deux outils délèguent à quelque chose qui peut ne jamais répondre : une
boucle d'outils LLM pour l'un, un sous-processus sandboxé pour l'autre. Tout
`await` sortant de ce module est donc borné par un `asyncio.wait_for`, et
l'expiration produit un message qui dit quoi faire — jamais un silence.

Inspiré de hermes-agent delegate_tool.py et execute_code.py
(NousResearch — voir notices/exec-backends.md).
"""

from __future__ import annotations

import asyncio
import contextvars
import tempfile
import uuid
from pathlib import Path

from loguru import logger

from crush.capabilities.tools.base import Tool, ToolResult
from crush.capabilities.tools.registry import ToolRegistry
from crush.engine.agent import Agent
from crush.engine.mission.backend_factory import get_backend_ephemere
from crush.engine.mission.backends.rpc import ScriptRPCRunner
from crush.kernel.approval import get_approval_checker
from crush.kernel.schemas import Session


def _borner(valeur: object, minimum: int, maximum: int, defaut: int) -> int:
    """Ramène un timeout fourni par le LLM dans un intervalle exploitable.

    Le modèle produit régulièrement des valeurs absurdes (0, None, "300",
    86400). Un timeout non borné réintroduit exactement le blocage que ce
    module cherche à supprimer, donc on écrase silencieusement plutôt que de
    rendre une erreur de validation que le LLM ne saurait pas corriger.
    """
    try:
        entier = int(valeur)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return defaut
    return max(minimum, min(maximum, entier))


# ── SpawnSubagentTool ─────────────────────────────────────────────────────────

# Profondeur de délégation en cours. Un ContextVar suit automatiquement les
# tâches asyncio filles : un sous-agent qui rappelle spawn_subagent hérite de
# la valeur du parent sans qu'on ait à la faire transiter dans les signatures.
_PROFONDEUR_DELEGATION: contextvars.ContextVar[int] = contextvars.ContextVar(
    "crush_profondeur_delegation", default=0
)

# Au-delà, on n'est plus dans une délégation mais dans une récursion qui brûle
# du budget sans jamais rendre de réponse au parent.
_PROFONDEUR_MAX = 2


class SpawnSubagentTool(Tool):
    """Délègue un workstream à un sous-agent ISOLÉ avec son propre contexte.

    Le parent ne reçoit qu'un résumé compact — aucune contamination de contexte.
    La session du sous-agent est fraîche (aucun historique hérité du parent).
    """

    name = "spawn_subagent"
    description = (
        "Délègue une tâche INTERNE (sans livrable persistent à sauvegarder) "
        "à un sous-agent isolé avec son propre contexte. Retourne un résumé "
        "compact. Réservé aux sous-questions ou analyses temporaires dont le "
        "résultat est consommé immédiatement. "
        "INTERDIT pour toute demande qui produit un fichier, document, email "
        "ou script que l'utilisateur voudra retrouver — émets [BG:PROJECT] à la place."
    )

    TIMEOUT_MIN = 10
    TIMEOUT_MAX = 300
    TIMEOUT_DEFAUT = 120

    input_schema = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Description complète de la tâche à déléguer.",
            },
            "context": {
                "type": "string",
                "description": "Contexte additionnel optionnel pour le sous-agent.",
            },
            "timeout": {
                "type": "integer",
                "description": (
                    f"Budget du sous-agent en secondes "
                    f"(défaut {TIMEOUT_DEFAUT}, borné à [{TIMEOUT_MIN}, {TIMEOUT_MAX}])."
                ),
                "default": TIMEOUT_DEFAUT,
            },
        },
        "required": ["task"],
    }

    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def execute(  # type: ignore[override]
        self,
        task: str,
        context: str = "",
        timeout: int = TIMEOUT_DEFAUT,  # noqa: ASYNC109
    ) -> ToolResult:

        profondeur = _PROFONDEUR_DELEGATION.get()
        if profondeur >= _PROFONDEUR_MAX:
            return ToolResult(
                content=(
                    f"[Sous-agent refusé] Profondeur de délégation maximale atteinte "
                    f"({_PROFONDEUR_MAX}). Traite cette sous-tâche directement au lieu "
                    f"de la déléguer à nouveau."
                ),
                is_error=True,
            )

        budget = _borner(timeout, self.TIMEOUT_MIN, self.TIMEOUT_MAX, self.TIMEOUT_DEFAUT)
        prompt = f"{context}\n\n---\nTâche : {task}" if context else task
        session = Session()
        session.add_message("user", prompt)

        logger.info("SpawnSubagent démarré", task=task[:60], budget=budget)
        jeton = _PROFONDEUR_DELEGATION.set(profondeur + 1)
        try:
            # respond_tools() est une boucle d'outils complète : elle peut
            # tourner tant que le LLM réclame des appels, et un provider muet
            # (socket ouverte, aucun octet) ne la fait jamais sortir.
            result = await asyncio.wait_for(
                self._agent.respond_tools(session),
                timeout=budget,
            )
            summary = str(result)[:2000]
            logger.info("SpawnSubagent terminé", chars=len(summary))
            return ToolResult(content=f"[Sous-agent terminé]\n{summary}")
        except TimeoutError:
            # TimeoutError dérive d'OSError : sans cette branche, le `except
            # Exception` plus bas l'avalerait et masquerait la vraie cause.
            logger.warning("SpawnSubagent timeout", task=task[:60], budget=budget)
            return ToolResult(
                content=(
                    f"[Sous-agent interrompu] Aucun résultat après {budget}s — "
                    f"le sous-agent a été annulé. Découpe la tâche en étapes plus "
                    f"petites, ou relance avec un timeout plus élevé "
                    f"(maximum {self.TIMEOUT_MAX}s)."
                ),
                is_error=True,
            )
        except Exception as exc:
            logger.error("SpawnSubagent erreur", error=str(exc))
            return ToolResult(
                content=f"[Sous-agent erreur] {exc}",
                is_error=True,
            )
        finally:
            _PROFONDEUR_DELEGATION.reset(jeton)


# ── ScriptRPCTool ─────────────────────────────────────────────────────────────

# ScriptRPCRunner écrit les fichiers RPC dans le workspace hôte puis réécrit
# leur chemin en `/workspace` avant de les passer au backend (rpc.py). Cette
# réécriture est inconditionnelle : elle suppose un backend conteneurisé qui
# monte le workspace sur /workspace. Tout backend qui ne fournit pas ce point
# de montage reçoit un chemin inexistant et échoue sur un « No such file ».
_POINT_MONTAGE_RPC = "/workspace"

# Backends dont on sait qu'ils honorent ce point de montage.
_BACKENDS_MONTANT_WORKSPACE = frozenset({"DockerBackend"})

# Backends qui exécutent sur une AUTRE machine que celle où les fichiers RPC
# ont été écrits : le pont ne peut structurellement pas fonctionner.
_BACKENDS_DISTANTS = frozenset({"SSHBackend", "RemoteBackend"})


def _diagnostic_pont_rpc(backend: object, workspace: Path) -> str | None:
    """Retourne None si le pont RPC peut fonctionner, sinon la raison précise.

    Vérification faite AVANT toute demande d'approbation : inutile de réveiller
    l'utilisateur pour autoriser un script qui ne peut de toute façon pas être
    lancé.
    """
    nom = type(backend).__name__

    if nom in _BACKENDS_MONTANT_WORKSPACE:
        return None

    if nom in _BACKENDS_DISTANTS:
        return (
            f"Le backend {nom} exécute sur une machine distante, alors que les "
            f"fichiers du pont RPC sont écrits en local dans {workspace}. "
            f"Aucun transfert n'est prévu : le script ne trouverait rien. "
            f"Utilisez le backend Docker (config/backends.json → "
            f'"default_backend": "docker").'
        )

    # as_posix() plutot que str() : le point de montage est un chemin POSIX,
    # et str() d'un Path le rend avec les separateurs de la plateforme. La
    # comparaison echouait donc hors Linux, y compris en developpement.
    if nom == "LocalBackend" and workspace.as_posix() != _POINT_MONTAGE_RPC:
        return (
            f"Le pont RPC réécrit les chemins vers {_POINT_MONTAGE_RPC} avant de les "
            f"passer au backend, mais LocalBackend exécute sur l'hôte où le workspace "
            f"est {workspace} — le script serait cherché dans "
            f"{_POINT_MONTAGE_RPC}/… qui n'existe pas sur cette machine. "
            f"Activer ALLOW_UNSANDBOXED_EXEC ne corrige PAS ce point. "
            f"Il faut le backend Docker (workspace monté sur "
            f"{_POINT_MONTAGE_RPC}) : installez Docker, mettez DOCKER_ENABLED=true "
            f'dans .env et "default_backend": "docker" dans config/backends.json.'
        )

    # Backend inconnu (sous-classe maison) : on le laisse tenter sa chance,
    # le timeout dur en aval reste le garde-fou.
    return None


async def _arreter(executeur: object | None) -> None:
    """Arrête le conteneur éphémère, sans jamais lever.

    Un conteneur qui refuse de s'arrêter laisse au pire un processus
    orphelin (il est lancé avec --rm) ; le faire remonter transformerait
    un échec déjà expliqué en trace illisible.
    """
    if executeur is None:
        return
    try:
        await executeur.stop()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Conteneur ephemere non arrete : {}", exc)


class ScriptRPCTool(Tool):
    """Exécute un script Python dans le sandbox avec accès aux outils Crush via RPC.

    Un pipeline de N appels d'outils = un seul tour LLM.
    Le script importe `crush_tools` (stub généré) pour appeler les outils.
    Seul le stdout remonte au LLM — les résultats intermédiaires n'entrent
    jamais dans le contexte.

    Tout dispatch d'outil passe par le backend sandboxé + approval_checker.
    """

    name = "execute_script"
    description = (
        "Exécute un script Python dans le sandbox Docker de la machine qui héberge "
        "Crush (une Raspberry Pi, PAS l'ordinateur de l'utilisateur — pour agir sur "
        "celui-ci, utilise remote_pc). "
        "Le script peut appeler `import crush_tools` pour chaîner des outils. "
        "Idéal pour les pipelines multi-étapes : N appels d'outils = 1 seul tour LLM. "
        "Seul le stdout final remonte — les résultats intermédiaires sont hors contexte. "
        "Nécessite un backend Docker actif ; sans lui l'outil refuse immédiatement."
    )

    TIMEOUT_MIN = 5
    TIMEOUT_MAX = 120
    TIMEOUT_DEFAUT = 60

    # Marge laissée au runner pour annuler son dispatcher et effacer le rpc_dir
    # après que le backend a rendu la main sur son propre timeout. Au-delà,
    # c'est le runner lui-même qui est coincé et on coupe.
    MARGE_ARRET = 15

    # Un backend peut sonder son environnement par sous-processus
    # (DockerExecutor.is_available lance `docker ps` SANS timeout) : un daemon
    # Docker figé bloquerait donc la sonde de disponibilité elle-même.
    DELAI_SONDE = 5

    # Attente maximale d'une réponse humaine à la demande d'approbation.
    # L'ApprovalChecker attend 120 s de son côté — bien plus que ce qu'un tour
    # d'outil peut se permettre quand personne ne regarde l'interface.
    DELAI_APPROBATION = 25

    input_schema = {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": (
                    "Script Python à exécuter. "
                    "Peut `import crush_tools` puis appeler les fonctions disponibles."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": (
                    f"Timeout d'exécution en secondes "
                    f"(défaut {TIMEOUT_DEFAUT}, borné à [{TIMEOUT_MIN}, {TIMEOUT_MAX}])."
                ),
                "default": TIMEOUT_DEFAUT,
            },
        },
        "required": ["script"],
    }

    def __init__(
        self,
        tool_registry: ToolRegistry,
        workspace_path: str | None = None,
    ) -> None:
        self._tool_registry = tool_registry
        self._workspace_path = workspace_path

    async def execute(self, script: str, timeout: int = TIMEOUT_DEFAUT) -> ToolResult:  # type: ignore[override]  # noqa: ASYNC109

        budget = _borner(timeout, self.TIMEOUT_MIN, self.TIMEOUT_MAX, self.TIMEOUT_DEFAUT)

        # Workspace : injecté à la construction ou répertoire temporaire
        if self._workspace_path:
            workspace = Path(self._workspace_path)
        else:
            workspace = Path(tempfile.mkdtemp(prefix="crush-rpc-"))

        backend, executeur, indisponible = await self._preparer_backend(workspace)
        if indisponible is not None:
            # Aucun script n'a été écrit ni lancé : on le dit, pour que le
            # modèle ne re-tente pas le même appel en boucle.
            return ToolResult(content=indisponible, is_error=True)

        refus = await self._demander_approbation(script)
        if refus is not None:
            return ToolResult(content=refus, is_error=True)

        runner = ScriptRPCRunner(backend, self._tool_registry, workspace)  # type: ignore[arg-type]
        logger.info("ScriptRPCTool démarré", script_len=len(script), budget=budget)

        try:
            # Filet de dernier recours : le backend est censé honorer `budget`,
            # mais LocalBackend/DockerBackend abandonnent leur `communicate()`
            # sans tuer le processus fils, et le runner peut rester bloqué dans
            # son nettoyage. Ce wait_for garantit qu'un appel d'outil rend
            # toujours la main.
            result = await asyncio.wait_for(
                runner.run(script, timeout=budget),
                timeout=budget + self.MARGE_ARRET,
            )
        except TimeoutError:
            logger.error("ScriptRPCTool timeout dur", budget=budget)
            return ToolResult(
                content=(
                    f"Script interrompu : aucune réponse du sandbox après "
                    f"{budget + self.MARGE_ARRET}s (budget demandé {budget}s). "
                    f"Le sous-processus a pu survivre à l'annulation — vérifiez "
                    f"`docker ps` sur la machine hôte. Relancez avec un script plus "
                    f"court, ou un timeout plus élevé (maximum {self.TIMEOUT_MAX}s)."
                ),
                is_error=True,
            )
        except Exception as exc:
            logger.error("ScriptRPCTool erreur", error=str(exc))
            return ToolResult(content=f"Échec du sandbox : {exc}", is_error=True)
        finally:
            # Le conteneur est ephemere : cree pour cet appel, detruit avec
            # lui. Sans ce finally, un timeout dur en laisserait un par
            # script lance, et la machine finirait par en porter des dizaines.
            await _arreter(executeur)

        parts = [
            f"succès={result['success']}  appels_outils={result['tool_calls']}",
        ]
        if result["stderr"]:
            parts.append(f"stderr:\n{result['stderr'][:500]}")
        if result["stdout"]:
            parts.append(result["stdout"])

        return ToolResult(
            content="\n".join(parts),
            is_error=not result["success"],
        )

    async def _preparer_backend(
        self, workspace: Path
    ) -> tuple[object | None, object | None, str | None]:
        """Retourne (backend, exécuteur à arrêter, None) ou (None, None, message).

        Ce pré-vol n'écrit rien et n'exécute aucun code fourni par le modèle.
        Il tourne donc AVANT la demande d'approbation, ce qui évite de bloquer
        l'utilisateur sur une confirmation dont la réponse ne changerait rien.
        """
        backend, executeur = await get_backend_ephemere(str(workspace))
        if backend is None:
            return None, None, (
                "Sandbox indisponible : aucun backend d'exécution n'a pu être "
                "construit. Vérifiez config/backends.json — avec "
                '"default_backend": "docker" il faut aussi DOCKER_ENABLED=true '
                "dans .env, puis redémarrer le service crush-api."
            )

        probleme = _diagnostic_pont_rpc(backend, workspace)
        if probleme is not None:
            await _arreter(executeur)
            return None, None, f"Sandbox inopérant sur cette machine. {probleme}"

        try:
            disponible = await asyncio.wait_for(
                backend.is_available(),  # type: ignore[attr-defined]
                timeout=self.DELAI_SONDE,
            )
        except TimeoutError:
            await _arreter(executeur)
            return None, None, (
                f"Sandbox indisponible : le backend {type(backend).__name__} n'a pas "
                f"répondu à la sonde de disponibilité en {self.DELAI_SONDE}s. "
                f"Le daemon Docker est probablement figé — sur la machine hôte : "
                f"`sudo systemctl restart docker`."
            )
        except Exception as exc:
            await _arreter(executeur)
            return None, None, f"Sandbox indisponible : sonde du backend en échec ({exc})."

        if not disponible:
            await _arreter(executeur)
            return None, None, (
                f"Sandbox indisponible : le backend {type(backend).__name__} se déclare "
                f"hors service. Pour Docker : vérifiez que le daemon tourne "
                f"(`docker ps`) et que DOCKER_ENABLED=true dans .env, puis "
                f"redémarrez crush-api."
            )

        return backend, executeur, None

    async def _demander_approbation(self, script: str) -> str | None:
        """Retourne None si l'exécution est approuvée, sinon le motif du refus.

        L'attente est bornée et l'expiration REFUSE (fail-closed) : on ne
        troque pas la sécurité contre la réactivité, on borne seulement la
        durée pendant laquelle le tour d'outil reste muet.
        """
        checker = get_approval_checker()
        if checker is None:
            return None

        try:
            approuve = await asyncio.wait_for(
                checker.check(
                    "code_write",
                    f"Script RPC : {script[:80]}…",
                    f"script-rpc-{uuid.uuid4().hex[:8]}",
                ),
                timeout=self.DELAI_APPROBATION,
            )
        except TimeoutError:
            logger.warning("ScriptRPCTool : approbation sans réponse", delai=self.DELAI_APPROBATION)
            return (
                f"Exécution refusée : aucune réponse à la demande d'approbation en "
                f"{self.DELAI_APPROBATION}s. Répondez à la demande affichée dans "
                f"l'interface web, ou basculez la catégorie en automatique : "
                f"PATCH /api/approvals/config/code_write "
                f'{{"mode": "always"}} (persisté dans memory_data/approvals.json).'
            )

        if not approuve:
            return (
                "Exécution de script refusée (catégorie d'approbation `code_write`). "
                "Pour l'autoriser : PATCH /api/approvals/config/code_write "
                '{"mode": "always"}.'
            )

        return None
