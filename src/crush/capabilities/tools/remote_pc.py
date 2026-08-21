"""Outil de pilotage d'un poste distant.

L'assistant tourne sur un serveur headless : `execute_cli` s'y exécute, sur le
serveur. Pour agir sur l'ordinateur de l'utilisateur — son volume, ses
applications, son extinction — il faut passer par l'agent qui y tourne.

Le registre est consulté à CHAQUE appel plutôt que capturé à la construction :
un poste peut se connecter ou disparaître entre deux phrases, et la liste des
actions disponibles change avec lui.

Quand rien n'est connecté, le registre (L0) ne peut que constater l'absence :
il ignore tout de l'agent et de sa ligne de commande. C'est donc ici, au plus
près de l'utilisateur, qu'est écrit le mode d'emploi de la connexion.
"""

from __future__ import annotations

from typing import Any

from crush.capabilities.tools.base import Tool, ToolResult
from crush.kernel.remote_agents import registry

# Catalogue de l'agent de référence (scripts/agent_pc.py). Recopié plutôt
# qu'importé : le script tourne sur la machine de l'utilisateur, jamais sur le
# serveur — il n'y a rien à importer ici. Sert à documenter le schéma et à
# expliquer pourquoi une action attendue n'est pas proposée.
_ACTIONS_CONNUES: dict[str, str] = {
    "status": "état de la machine (nom, système, CPU, mémoire, batterie)",
    "screen": (
        "sur quoi il travaille en ce moment — titres des fenêtres ouvertes, "
        "pas une capture d'écran"
    ),
    "volume_set": 'règle le volume — params {"level": 0.0 à 1.0}',
    "volume_mute": "coupe ou rétablit le son",
    "app_launch": 'lance une application — params {"name": "Firefox"}',
    "app_quit": 'ferme une application — params {"name": "Spotify"}',
    "lock": "verrouille la session",
    "sleep": "met la machine en veille",
    "shutdown": 'éteint la machine — params {"delay": minutes, 0 = tout de suite}',
    "cancel_shutdown": "annule une extinction programmée",
}

# Actions irréversibles ou perturbantes. L'agent ne les annonce QUE s'il a été
# lancé avec --autoriser-sensibles ; sans cela il les refuse, protection contre
# une injection de prompt qui ferait éteindre l'ordinateur. Le doublon avec
# scripts/agent_pc.py est assumé : c'est l'agent qui décide, on n'a besoin ici
# que d'expliquer l'absence.
_ACTIONS_SENSIBLES = frozenset({"shutdown", "sleep", "app_quit"})

# `screen` a son propre drapeau cote agent (`--autoriser-ecran`), distinct des
# sensibles : celles-la CASSENT quelque chose, celle-ci RACONTE quelque chose.
# Les melanger obligerait a accepter d'etre observe pour obtenir l'extinction.
_ACTIONS_ECRAN = frozenset({"screen"})

_COMMENT_CONNECTER = (
    "Sur la machine à piloter :\n"
    "  1. pip install websockets\n"
    "  2. python scripts/agent_pc.py --configurer   (adresse du serveur + jeton, "
    "une seule fois)\n"
    "  3. python scripts/agent_pc.py                (laisser tourner : il se "
    "reconnecte seul)\n"
    "Ajouter --autoriser-sensibles à la dernière commande pour permettre "
    "l'extinction, la veille et la fermeture d'applications, et --autoriser-ecran "
    "pour qu'il puisse dire sur quoi vous travaillez."
)


def _catalogue_pour_schema() -> str:
    lignes = []
    for nom, aide in _ACTIONS_CONNUES.items():
        if nom in _ACTIONS_SENSIBLES:
            suffixe = " [sensible]"
        elif nom in _ACTIONS_ECRAN:
            suffixe = " [ecran]"
        else:
            suffixe = ""
        lignes.append(f"{nom} : {aide}{suffixe}")
    return " | ".join(lignes)


class RemotePCTool(Tool):
    """Exécute une action sur un ordinateur connecté à l'assistant."""

    name = "remote_pc"
    description = (
        "Agit sur l'ordinateur de l'utilisateur (et non sur le serveur qui héberge "
        "l'assistant) : régler le volume, lancer ou fermer une application, "
        "verrouiller, mettre en veille, éteindre, connaître son état. "
        "À utiliser dès que la demande vise « mon PC », « mon ordi », ou une action "
        "qui n'a de sens que sur la machine de l'utilisateur. "
        "Cet outil ne fait PAS jouer de musique : mettre un morceau, un artiste ou "
        "une playlist « sur mon PC » designe un appareil de lecture Spotify et passe "
        "par spotify_control, meme quand la demande nomme l'ordinateur. "
        "Appeler d'abord sans argument pour connaître les postes connectés et leurs "
        "actions disponibles. Cela suppose que l'agent scripts/agent_pc.py tourne sur "
        "cette machine ; sinon l'outil explique comment le lancer."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": (
                    "Action à exécuter. Omettre pour lister les postes connectés "
                    "et les actions que chacun annonce. Actions de l'agent standard — "
                    f"{_catalogue_pour_schema()}. Les actions marquées [sensible] ne "
                    "sont proposées que si l'agent a été lancé avec "
                    "--autoriser-sensibles ; un poste peut en annoncer d'autres."
                ),
            },
            "params": {
                "type": "object",
                "description": (
                    'Paramètres de l\'action. Ex. : {"level": 0.4} pour volume_set, '
                    '{"name": "Spotify"} pour app_launch, '
                    '{"delay": 20} pour shutdown.'
                ),
            },
            "machine": {
                "type": "string",
                "description": (
                    "Nom du poste. Inutile s'il n'y en a qu'un seul connecté."
                ),
            },
        },
        "required": [],
    }

    async def execute(self, **kwargs: Any) -> ToolResult:  # noqa: ANN401
        action = kwargs.get("action")
        machine = kwargs.get("machine")
        params = kwargs.get("params") or {}

        agents = registry.list_agents()

        # Sans action : inventaire. C'est aussi la réponse utile quand le LLM
        # ignore ce que le poste sait faire.
        if not action:
            if not agents:
                return ToolResult(
                    content="Aucun ordinateur connecté à l'assistant.\n" + _COMMENT_CONNECTER
                )
            lignes = [
                f"- {a.name} ({a.platform}) : {', '.join(a.actions) or 'aucune action'}"
                for a in agents
            ]
            return ToolResult(content="Postes connectés :\n" + "\n".join(lignes))

        if not isinstance(params, dict):
            return ToolResult(content="`params` doit être un objet.", is_error=True)

        # Sans agent, `dispatch` répondrait « Aucun canal d'agent distant actif »,
        # vrai mais inexploitable : on répond ici avec le remède.
        if not agents:
            return ToolResult(
                content=(
                    f"Impossible d'exécuter « {action} » : aucun ordinateur n'est "
                    "connecté à l'assistant.\n" + _COMMENT_CONNECTER
                ),
                is_error=True,
            )

        agent = registry.get(machine if isinstance(machine, str) else None)
        if agent is None:
            noms = ", ".join(sorted(a.name for a in agents))
            manquant = f"Aucun poste nommé « {machine} »." if machine else "Plusieurs postes."
            return ToolResult(
                content=f"{manquant} Préciser `machine` parmi : {noms}.",
                is_error=True,
            )

        nom_action = str(action)
        if nom_action not in agent.actions:
            return ToolResult(content=self._pourquoi_absente(nom_action, agent), is_error=True)

        resultat = await registry.dispatch(nom_action, params, agent.name)

        if not resultat.get("ok"):
            return ToolResult(
                content=resultat.get("error") or "Action échouée.",
                is_error=True,
            )
        return ToolResult(content=str(resultat.get("detail") or "Fait."))

    def _pourquoi_absente(self, action: str, agent: Any) -> str:  # noqa: ANN401
        """Une action absente a trois causes distinctes ; les confondre égare."""
        disponibles = ", ".join(agent.actions) or "aucune"
        if action in _ACTIONS_SENSIBLES:
            return (
                f"« {action} » est une action sensible : {agent.name} ne l'accepte que "
                "si son agent a été lancé avec `python scripts/agent_pc.py "
                "--autoriser-sensibles`. Le relancer ainsi sur cette machine. "
                f"Actions actuellement annoncées : {disponibles}."
            )
        if action in _ACTIONS_CONNUES:
            return (
                f"« {action} » n'est pas disponible sur {agent.name} — l'agent l'a "
                "écartée au démarrage, faute d'un outil système (par exemple `pactl` "
                "pour le volume sous Linux). "
                f"Actions annoncées : {disponibles}."
            )
        return (
            f"« {action} » n'existe pas. Actions annoncées par {agent.name} : {disponibles}."
        )
