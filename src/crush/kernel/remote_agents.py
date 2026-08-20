"""Registre des agents distants — machines pilotées par l'assistant.

L'assistant tourne sur un serveur sans écran ni session graphique. « Monte le
son », « lance Spotify », « éteins le PC » n'ont aucun sens à y exécuter : ces
actions visent une AUTRE machine. Un petit agent tourne donc sur le poste à
piloter et se connecte AU serveur.

SENS DE LA CONNEXION
====================

C'est le poste qui appelle le serveur, jamais l'inverse. Conséquences : aucun
port à ouvrir sur le poste, aucune redirection sur la box, et l'agent
fonctionne derrière n'importe quel routeur. Il se reconnecte tout seul, donc
on peut redémarrer le serveur sans y penser.

PLACE DANS LES COUCHES
======================

Ce module vit en L0 pour que l'outil (L1 `capabilities`) puisse déclencher une
action sans importer `interfaces` (L3), ce qu'interdit la RÈGLE 2 du CDC. Il ne
connaît donc RIEN des WebSockets : la couche interfaces enregistre ici une
fonction d'expédition, sur le modèle de `kernel.approval.set_approval_checker`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

# Expédie (nom_machine, action, paramètres) et rend le résultat.
Dispatcher = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class RemoteAgent:
    """Un poste connecté, et ce qu'il annonce savoir faire."""

    name: str
    platform: str
    actions: list[str] = field(default_factory=list)
    version: str = ""


class RemoteAgentRegistry:
    """Agents connectés à l'instant T. Ni persistance ni file d'attente.

    Un agent absent est une machine éteinte : on le dit à l'utilisateur plutôt
    que d'empiler des commandes qu'il exécuterait à son réveil, des heures plus
    tard et hors contexte.
    """

    def __init__(self) -> None:
        self._agents: dict[str, RemoteAgent] = {}
        self._dispatcher: Dispatcher | None = None

    # ── Cycle de vie, appelé par la couche interfaces ──────────────────────

    def set_dispatcher(self, dispatcher: Dispatcher | None) -> None:
        self._dispatcher = dispatcher

    def add(self, agent: RemoteAgent) -> None:
        self._agents[agent.name] = agent
        logger.info(
            "Agent distant connecté : {} ({}, {} action(s))",
            agent.name,
            agent.platform,
            len(agent.actions),
        )

    def remove(self, name: str) -> None:
        if self._agents.pop(name, None) is not None:
            logger.info("Agent distant déconnecté : {}", name)

    # ── Consultation, appelée par l'outil ──────────────────────────────────

    def list_agents(self) -> list[RemoteAgent]:
        return list(self._agents.values())

    def get(self, name: str | None) -> RemoteAgent | None:
        """Résout un nom de machine. Sans nom, l'unique agent connecté.

        Le cas courant est « un seul PC » : exiger que l'utilisateur le nomme
        à chaque phrase serait absurde. Avec plusieurs agents, l'ambiguïté est
        signalée plutôt que tranchée au hasard.
        """
        if name:
            return self._agents.get(name)
        if len(self._agents) == 1:
            return next(iter(self._agents.values()))
        return None

    async def dispatch(
        self, action: str, params: dict[str, Any], machine: str | None = None
    ) -> dict[str, Any]:
        """Exécute une action sur un poste connecté."""
        if self._dispatcher is None:
            return {"ok": False, "error": "Aucun canal d'agent distant actif."}

        agent = self.get(machine)
        if agent is None:
            if not self._agents:
                return {"ok": False, "error": "Aucun poste connecté."}
            noms = ", ".join(sorted(self._agents))
            return {
                "ok": False,
                "error": f"Préciser la machine parmi : {noms}.",
            }

        if action not in agent.actions:
            return {
                "ok": False,
                "error": (
                    f"« {action} » n'est pas disponible sur {agent.name}. "
                    f"Actions annoncées : {', '.join(agent.actions) or 'aucune'}."
                ),
            }

        return await self._dispatcher(agent.name, action, params)


# Singleton — même motif que `kernel.approval` et `kernel.notifications`.
registry = RemoteAgentRegistry()
