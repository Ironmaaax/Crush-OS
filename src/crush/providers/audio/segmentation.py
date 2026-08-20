"""Découpage d'un flux de texte en fragments prêts à synthétiser.

POURQUOI
========

Synthétiser la réponse entière avant d'émettre le moindre son impose d'attendre
que le LLM ait fini, PUIS que le TTS ait fini. Mesuré sur le Pi 5 avec Piper
(×7,2 temps réel une fois le modèle chargé) : 1,72 s de synthèse pour une
réponse de quatre phrases, à ajouter aux secondes du LLM.

En découpant par phrase, le premier son part après 0,25 s, et la synthèse des
phrases suivantes recouvre la génération du LLM. L'attente perçue s'effondre
sans que rien n'aille plus vite.

DEUX RÉGIMES
============

Le premier fragment est émis dès qu'une phrase est complète, même courte :
c'est lui qui fixe le délai avant le premier son. Les suivants sont regroupés
jusqu'à `TAILLE_CIBLE`, car une succession de fragments trop brefs hache la
prosodie et multiplie les à-coups entre deux lectures.
"""

from __future__ import annotations

import re

# Fin de phrase : ponctuation forte suivie d'une espace. Le lookbehind conserve
# la ponctuation dans le fragment, dont le TTS a besoin pour l'intonation.
_FIN_DE_PHRASE = re.compile(r"(?<=[.!?…])\s+")

# En deçà, un fragment est trop bref pour être lu seul : on l'agrège au suivant.
TAILLE_MIN_PREMIER = 12
# Au-delà, on coupe : regrouper davantage retarderait le son sans gain.
TAILLE_CIBLE = 200


def _nettoyer(fragment: str) -> str:
    """Retire ce qui ne se prononce pas : puces, dièses de titre, astérisques."""
    fragment = re.sub(r"[*_`#]+", "", fragment)
    fragment = re.sub(r"^\s*[-•]\s*", "", fragment)
    return " ".join(fragment.split())


class SentenceAccumulator:
    """Accumule des morceaux de flux et rend les fragments prêts à dire.

    Usage :

        acc = SentenceAccumulator()
        for morceau in flux:
            for fragment in acc.push(morceau):
                synthetiser(fragment)
        for fragment in acc.flush():
            synthetiser(fragment)
    """

    def __init__(self, taille_cible: int = TAILLE_CIBLE) -> None:
        self._tampon = ""
        self._taille_cible = taille_cible
        self._premier_emis = False

    def push(self, morceau: str) -> list[str]:
        """Ajoute un morceau et retourne les fragments devenus complets."""
        self._tampon += morceau
        prets: list[str] = []

        while True:
            fragment, reste = self._decouper()
            if fragment is None:
                break
            self._tampon = reste
            nettoye = _nettoyer(fragment)
            if nettoye:
                prets.append(nettoye)
                self._premier_emis = True

        return prets

    def flush(self) -> list[str]:
        """Rend ce qui reste, phrase incomplète comprise (fin de réponse)."""
        reste = _nettoyer(self._tampon)
        self._tampon = ""
        return [reste] if reste else []

    def _decouper(self) -> tuple[str | None, str]:
        """Extrait un fragment complet du tampon, ou (None, tampon) s'il n'y en a pas."""
        parties = _FIN_DE_PHRASE.split(self._tampon, maxsplit=1)
        if len(parties) < 2:
            # Aucune fin de phrase en vue. On coupe quand même si le tampon
            # devient très long : un LLM peut produire un paragraphe entier
            # sans ponctuation forte, et il ne faut pas attendre la fin.
            if len(self._tampon) > self._taille_cible * 2:
                coupure = self._tampon.rfind(" ", 0, self._taille_cible)
                if coupure > 0:
                    return self._tampon[:coupure], self._tampon[coupure:]
            return None, self._tampon

        phrase, reste = parties[0], parties[1]

        # Premier fragment : on l'émet au plus tôt, c'est lui qui fixe le délai
        # avant le premier son. Les suivants sont regroupés pour la prosodie.
        if not self._premier_emis:
            if len(phrase.strip()) >= TAILLE_MIN_PREMIER:
                return phrase, reste
            # Trop court pour être dit seul : on garde tout et on réessaie.
            return None, self._tampon

        if len(phrase) >= self._taille_cible:
            return phrase, reste

        # On tente d'agréger la phrase suivante tant qu'on reste sous la cible.
        suite = _FIN_DE_PHRASE.split(reste, maxsplit=1)
        if len(suite) < 2:
            return (phrase, reste) if len(phrase) >= TAILLE_MIN_PREMIER else (None, self._tampon)
        if len(phrase) + len(suite[0]) <= self._taille_cible:
            return f"{phrase} {suite[0]}", suite[1]
        return phrase, reste
