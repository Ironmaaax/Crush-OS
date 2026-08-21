# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS,   .


"""Compteur mensuel de chargements de carte, et plafond associé.

POURQUOI CE MODULE EXISTE
=========================

Mapbox offre 50 000 chargements de carte par mois, puis facture. Un usage
personnel en consomme quelques centaines — mais une page laissée ouverte qui
recharge en boucle, ou une vue relancée par une routine, transforment un palier
confortable en facture, sans que rien ne prévienne.

Le garde-fou vit ICI plutôt que dans le navigateur pour une raison simple :
c'est le serveur qui distribue le jeton. Sans jeton, aucune carte ne se charge.
Un compteur côté client se contournerait d'un rechargement, et ne verrait pas
les autres navigateurs.

CE QU'IL COMPTE, EXACTEMENT
===========================

Une remise de jeton, pas un chargement facturé par Mapbox. Les deux coïncident
dans l'usage nominal — la page demande la configuration juste avant de
construire la carte — mais ce n'est pas une équivalence garantie. Le chiffre
est donc une ESTIMATION, et le plafond par défaut garde une marge sous le
palier réel pour l'absorber.

Ce module ne remplace pas le plafond de dépense à configurer chez Mapbox :
celui-là est la seule garantie dure. Celui-ci évite d'y arriver.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from loguru import logger

from crush.kernel.paths import MEMORY_DATA_DIR
from crush.kernel.persistance import ecrire_atomique

FICHIER_QUOTA: Path = MEMORY_DATA_DIR / "quota_cartes.json"


@dataclass(frozen=True)
class EtatQuota:
    """Ce que le compteur sait, à un instant donné."""

    mois: str
    chargements: int
    plafond: int

    @property
    def depasse(self) -> bool:
        # `plafond <= 0` désactive le garde-fou : un plafond nul signifierait
        # « aucune carte », ce qui n'est jamais l'intention de qui met zéro.
        return self.plafond > 0 and self.chargements >= self.plafond

    @property
    def restants(self) -> int:
        return max(0, self.plafond - self.chargements) if self.plafond > 0 else -1


def _mois_courant() -> str:
    return date.today().strftime("%Y-%m")


def _lire(chemin: Path) -> tuple[str, int]:
    """Mois et compteur enregistrés, ou le mois courant à zéro.

    Ne lève jamais : un compteur illisible ne doit pas empêcher d'afficher une
    carte. Le risque d'un fichier corrompu est de repartir de zéro, pas de
    dépasser silencieusement — le plafond reste appliqué sur le reste du mois.
    """
    try:
        donnees = json.loads(chemin.read_text(encoding="utf-8"))
        if not isinstance(donnees, dict):
            raise ValueError
        mois = str(donnees.get("mois", ""))
        chargements = int(donnees.get("chargements", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return _mois_courant(), 0
    # Changement de mois : le palier Mapbox se réinitialise, le compteur aussi.
    if mois != _mois_courant():
        return _mois_courant(), 0
    return mois, max(0, chargements)


def etat(plafond: int, chemin: Path | None = None) -> EtatQuota:
    """Lit le compteur sans l'incrémenter — pour l'affichage."""
    mois, chargements = _lire(chemin or FICHIER_QUOTA)
    return EtatQuota(mois=mois, chargements=chargements, plafond=plafond)


def consommer(plafond: int, chemin: Path | None = None) -> EtatQuota:
    """Compte un chargement et rend l'état APRÈS incrément.

    Incrémente même au-delà du plafond : savoir de combien on a dépassé, si le
    plafond est relevé un jour, vaut mieux qu'un compteur figé à la limite.
    """
    cible = chemin or FICHIER_QUOTA
    mois, chargements = _lire(cible)
    chargements += 1
    try:
        ecrire_atomique(cible, json.dumps({"mois": mois, "chargements": chargements}))
    except OSError as exc:  # noqa: BLE001 — un compteur non écrit ne bloque pas la carte
        logger.warning("Quota cartes : compteur non enregistré ({})", exc)
    resultat = EtatQuota(mois=mois, chargements=chargements, plafond=plafond)
    if resultat.depasse:
        logger.warning(
            "Quota cartes atteint : {} chargements ce mois-ci (plafond {}). "
            "Le jeton n'est plus distribué.",
            chargements,
            plafond,
        )
    return resultat
