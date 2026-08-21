#!/usr/bin/env python3
# Copyright (C) 2026 Maxime Song

"""Archive la mémoire de l'assistant — déclenchement manuel.

La logique vit désormais dans `crush.providers.memory.sauvegarde`, appelée
automatiquement chaque nuit par le scheduler (réglage `BACKUP_HOUR`). Ce script
reste le moyen de déclencher une passe à la main : avant une migration, avant de
toucher à la base, ou simplement pour vérifier que la chaîne fonctionne sans
attendre 4 h du matin.

    python scripts/sauvegarde_memoire.py
    python scripts/sauvegarde_memoire.py --etat   # ne sauvegarde pas, dit juste où on en est
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from crush.kernel.paths import MEMORY_DATA_DIR, SAUVEGARDES_DIR
from crush.kernel.settings import settings
from crush.providers.memory.sauvegarde import SauvegardeMemoire


def _construire() -> SauvegardeMemoire:
    return SauvegardeMemoire(
        source=MEMORY_DATA_DIR,
        destination=SAUVEGARDES_DIR,
        conserver=settings.backup_keep,
        copier_vers=settings.backup_copy_to,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--etat", action="store_true", help="affiche l'etat sans sauvegarder")
    args = ap.parse_args()

    sauvegarde = _construire()

    if args.etat:
        age = sauvegarde.age_heures()
        derniere = sauvegarde.derniere()
        if age is None:
            print("Aucune archive. La mémoire n'existe qu'en un seul exemplaire.")
            return 1
        print(f"Dernière archive : {derniere.name if derniere else '?'}, il y a {age:.0f} h.")
        if not settings.backup_copy_to.strip():
            print(
                "BACKUP_COPY_TO n'est pas renseigné : les archives sont sur le support\n"
                "qu'elles protègent, ce qui ne couvre pas sa panne."
            )
        return 0

    resultat = asyncio.run(sauvegarde.sauvegarder())

    if not resultat.reussie:
        print(f"Échec : {resultat.erreur}", file=sys.stderr)
        return 1

    mo = resultat.octets / 1024**2
    print(
        f"Mémoire sauvegardée : {resultat.archive}, {mo:.1f} Mo, "
        f"{resultat.bases_instantanees} base(s) en instantané cohérent, "
        f"{resultat.purgees} archive(s) ancienne(s) purgée(s)."
    )
    if resultat.copiee_hors_machine:
        print(f"Copiée hors machine vers {settings.backup_copy_to}.")
    elif resultat.erreur:
        print(f"Attention : {resultat.erreur}", file=sys.stderr)
    else:
        print(
            "Cette archive est sur le MÊME support que l'original : renseigner\n"
            "BACKUP_COPY_TO est ce qui la rend utile contre une panne de support."
        )
    return 0


if __name__ == "__main__":
    os.umask(0o077)  # l'archive contient la mémoire : lisible par son seul propriétaire
    raise SystemExit(main())
