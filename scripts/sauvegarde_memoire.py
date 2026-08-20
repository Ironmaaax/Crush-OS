#!/usr/bin/env python3
# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS,   .
 

"""Archive la mémoire de l'assistant.

Tout ce qu'il sait de son utilisateur — la base SQLite, les fichiers
thématiques, les sessions, le registre de consommation — vit dans `memory_data/`,
sur une carte SD. Les cartes SD meurent, et celle-ci tourne en écriture
24 heures sur 24. Une mémoire perdue ne se reconstruit pas : c'est la seule
donnée du projet qui n'existe nulle part ailleurs.

Le `.env` est délibérément EXCLU : une archive de sauvegarde se recopie, se
transfère, se laisse traîner, et n'a aucune raison de transporter des clés API.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
SOURCE = RACINE / "memory_data"
DESTINATION = RACINE / "sauvegardes"

# Au-delà, les plus anciennes partent : sans purge, une sauvegarde quotidienne
# finirait par remplir la carte qu'elle est censée protéger.
_ARCHIVES_CONSERVEES = 7

# Exclus de l'archive : volumineux et intégralement reconstructibles.
_EXCLUS = {"vector_index", "rpc_workspace"}


def _copie_coherente_sqlite(source: Path, cible: Path) -> bool:
    """Copie une base SQLite en cours d'utilisation, sans la corrompre.

    Le service écrit en permanence, en mode WAL. Copier le fichier à l'octet
    pendant une transaction produit une archive qui s'ouvre mais dont il manque
    la fin — une sauvegarde qu'on croit avoir et qui n'existe pas. L'API
    `backup` de SQLite prend un instantané cohérent.
    """
    try:
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, sqlite3.connect(
            cible
        ) as dst:
            src.backup(dst)
        return True
    except sqlite3.Error:
        return False


def _purger(dossier: Path) -> int:
    archives = sorted(dossier.glob("memoire-*.tar.gz"))
    a_supprimer = archives[:-_ARCHIVES_CONSERVEES] if len(archives) > _ARCHIVES_CONSERVEES else []
    for vieille in a_supprimer:
        vieille.unlink(missing_ok=True)
    return len(a_supprimer)


def main() -> int:
    if not SOURCE.is_dir():
        print(f"Rien à sauvegarder : {SOURCE} n'existe pas.")
        return 1

    DESTINATION.mkdir(parents=True, exist_ok=True)
    horodatage = datetime.now().strftime("%Y-%m-%d_%H%M")
    archive = DESTINATION / f"memoire-{horodatage}.tar.gz"

    bases_traitees = 0
    with tempfile.TemporaryDirectory() as tmp:
        instantanes = Path(tmp)
        with tarfile.open(archive, "w:gz") as tar:
            for chemin in sorted(SOURCE.rglob("*")):
                if not chemin.is_file():
                    continue
                relatif = chemin.relative_to(SOURCE)
                if relatif.parts and relatif.parts[0] in _EXCLUS:
                    continue
                # Les annexes -wal et -shm n'ont pas de sens hors de leur base :
                # l'instantané ci-dessous les intègre déjà.
                if chemin.suffix in {".db-wal", ".db-shm"} or chemin.name.endswith(
                    ("-wal", "-shm")
                ):
                    continue

                if chemin.suffix == ".db":
                    copie = instantanes / relatif.name
                    if _copie_coherente_sqlite(chemin, copie):
                        tar.add(copie, arcname=str(relatif))
                        bases_traitees += 1
                        continue
                    # Repli : mieux vaut une copie brute que pas de sauvegarde,
                    # mais on le dit.
                    print(f"  (instantané impossible pour {relatif}, copie brute)")
                tar.add(chemin, arcname=str(relatif))

    taille_mo = archive.stat().st_size / 1024**2
    supprimees = _purger(DESTINATION)
    libre_go = shutil.disk_usage(RACINE)[2] / 1024**3

    print(
        f"Mémoire sauvegardée : {archive.name}, {taille_mo:.1f} Mo, "
        f"{bases_traitees} base(s) en instantané cohérent. "
        f"{supprimees} archive(s) ancienne(s) supprimée(s), "
        f"{len(list(DESTINATION.glob('memoire-*.tar.gz')))} conservée(s). "
        f"{libre_go:.0f} Go libres sur le disque."
    )
    print(
        "Cette archive est sur la MÊME carte SD que l'original : "
        "la recopier ailleurs est ce qui la rend utile."
    )
    return 0


if __name__ == "__main__":
    os.umask(0o077)  # l'archive contient la mémoire : lisible par son seul propriétaire
    raise SystemExit(main())
