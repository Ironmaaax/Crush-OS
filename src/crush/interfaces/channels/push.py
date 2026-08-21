# Copyright (C) 2026 Maxime Song

"""Push sortant — écrire à l'utilisateur sans qu'il ait écrit d'abord.

POURQUOI CE MODULE EXISTE

Un canal de messagerie sait faire deux choses très différentes :

- `send(reponse, cible)` — répondre à un message reçu. La cible vient du message,
  donc il faut qu'il y ait eu un message. C'est ce que la passerelle utilise.
- `send_message(texte)` — écrire à l'owner **sans rien avoir reçu**. C'est la
  seule forme utilisable par un moteur proactif.

La seconde existait sur le canal Telegram, marquée « mode legacy », et n'était
appelée par personne. Le moteur proactif produisait donc des décisions à prendre
que rien ne poussait : les initiatives `VALIDATE` étaient diffusées aux seuls
clients WebSocket connectés, et dormaient sinon dans le Command Center jusqu'à ce
qu'on pense à l'ouvrir. Sur une machine allumée en permanence et consultée depuis
un téléphone, c'est-à-dire : rarement.

`send_message` n'est volontairement PAS dans `ChannelAdapter` : tous les canaux
ne peuvent pas écrire spontanément — un webhook entrant n'a pas de destinataire
par défaut — et l'ajouter à l'interface commune donnerait l'illusion que tous
savent le faire. D'où le Protocol ci-dessous, qui décrit la capacité plutôt que
de l'imposer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from loguru import logger


@runtime_checkable
class CanalPoussant(Protocol):
    """Canal capable d'écrire à l'owner sans avoir reçu de message."""

    async def send_message(self, text: str) -> None: ...


@runtime_checkable
class CanalDecidant(Protocol):
    """Canal capable de proposer un CHOIX, et pas seulement d'annoncer.

    Une initiative `VALIDATE` attend une décision. Poussée en texte, elle laissait
    la question sur le téléphone et la réponse sur l'ordinateur : il fallait ouvrir
    le Command Center pour dire oui. Un canal qui sait afficher deux boutons ferme
    la boucle là où la question est arrivée.

    Capacité séparée de `CanalPoussant` pour la même raison que celui-ci est séparé
    de `ChannelAdapter` : tous les canaux ne savent pas le faire, et un webhook
    sortant n'a rien où accrocher un bouton. Ceux qui ne savent pas reçoivent le
    texte — ils perdent le bouton, pas le message.
    """

    async def send_decision(self, text: str, initiative_id: str) -> None: ...


@dataclass
class DernierEnvoi:
    """Resultat de la derniere tentative reelle d'envoi."""

    horodatage: datetime
    reussi: bool
    erreur: str | None = None


# Etat observe, lu par la page Ecosysteme. Meme motif que
# `kernel/notifications.set_proactive_queue` : un module-global assume plutot
# qu'une dependance tiree a travers trois couches pour un seul affichage.
#
# POURQUOI : le maillon se fondait sur la CONFIGURATION. Or un canal active, avec
# un token valide et le bon identifiant, reste incapable d'ecrire tant que
# l'utilisateur n'a pas parle au bot le premier -- Telegram refuse alors par
# « chat not found ». La page affichait donc un voyant vert pendant que chaque
# envoi echouait, ce que sa propre docstring dit vouloir eviter.
_dernier: DernierEnvoi | None = None


def dernier_envoi() -> DernierEnvoi | None:
    """La derniere tentative, ou None si aucune n'a encore eu lieu."""
    return _dernier


class PushCanaux:
    """Pousse un texte vers tous les canaux capables d'écrire d'eux-mêmes."""

    def __init__(self, canaux: Sequence[object]) -> None:
        # `isinstance` sur un Protocol runtime_checkable ne vérifie que la présence
        # de la méthode, pas sa signature. C'est exactement le contrôle voulu :
        # on trie les canaux par capacité, sans imposer d'héritage.
        self._canaux: list[CanalPoussant] = [c for c in canaux if isinstance(c, CanalPoussant)]
        self._derniere_erreur: str | None = None
        if self._canaux:
            logger.info("Push sortant actif", canaux=[_nom(c) for c in self._canaux])
        else:
            logger.info(
                "Push sortant indisponible — aucun canal ne sait écrire spontanément. "
                "Les initiatives attendront la prochaine conversation."
            )

    def disponible(self) -> bool:
        return bool(self._canaux)

    async def pousser(self, texte: str) -> bool:
        """Envoie sur tous les canaux capables. Vrai si au moins un a abouti.

        On ne s'arrête pas au premier succès : plusieurs canaux configurés, c'est
        le souhait d'être joint sur plusieurs. Et l'échec de l'un — bot arrêté,
        réseau coupé — ne doit pas priver les autres.
        """
        global _dernier
        if not self._canaux:
            return False
        resultats = await asyncio.gather(*(self._envoyer(c, texte) for c in self._canaux))
        abouti = any(resultats)
        _dernier = DernierEnvoi(
            horodatage=datetime.now(),
            reussi=abouti,
            erreur=None if abouti else self._derniere_erreur,
        )
        return abouti

    async def pousser_decision(self, texte: str, initiative_id: str) -> bool:
        """Pousse une question, avec de quoi y répondre là où elle arrive.

        Chaque canal reçoit ce qu'il sait afficher : des boutons s'il en est
        capable, le texte sinon. On ne choisit pas UN canal — être joint sur
        plusieurs, c'est vouloir décider depuis n'importe lequel.
        """
        global _dernier
        if not self._canaux:
            return False
        resultats = await asyncio.gather(
            *(self._envoyer(c, texte, initiative_id) for c in self._canaux)
        )
        abouti = any(resultats)
        _dernier = DernierEnvoi(
            horodatage=datetime.now(),
            reussi=abouti,
            erreur=None if abouti else self._derniere_erreur,
        )
        return abouti

    async def _envoyer(
        self, canal: CanalPoussant, texte: str, initiative_id: str | None = None
    ) -> bool:
        try:
            if initiative_id and isinstance(canal, CanalDecidant):
                await canal.send_decision(texte, initiative_id)
            else:
                await canal.send_message(texte)
        except Exception as exc:  # noqa: BLE001 — un canal muet ne doit pas casser le cycle
            self._derniere_erreur = f"{_nom(canal)} : {exc}"
            logger.warning("Push impossible", canal=_nom(canal), erreur=str(exc))
            return False
        return True


def _nom(canal: object) -> str:
    plateforme = getattr(canal, "platform", None)
    return str(getattr(plateforme, "value", None) or type(canal).__name__)
