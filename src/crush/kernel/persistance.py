# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Écriture de fichiers d'état, résistante à une coupure.

Deux modules du kernel persistent des choix de l'utilisateur — les permissions
runtime et les modes d'approbation. Tous deux tournent sur une Raspberry Pi
sans onduleur, et tous deux verrouillent des capacités : un fichier tronqué au
mauvais moment ne dégrade pas le service, il le referme.

L'helper vivait en double, une fois par module. Ici il n'y en a qu'un.
"""

from __future__ import annotations

import os
from pathlib import Path


def ecrire_atomique(path: Path, payload: str, mode: int | None = None) -> None:
    """Écrit `payload` puis remplace `path` d'un seul coup.

    Une coupure laisse alors soit l'ancien fichier intact, soit le nouveau
    complet — jamais un JSON à moitié écrit. Le temporaire est créé dans le
    même répertoire : `os.replace` n'est atomique qu'au sein d'un même système
    de fichiers.

    `mode` restreint les permissions du fichier final. À utiliser dès que le
    contenu est un secret : un jeton de rafraîchissement OAuth vaut un mot de
    passe, et il naissait en 644 — lisible par tout compte de la machine.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            # Sans fsync, le rename peut atteindre le disque avant les données.
            os.fsync(fh.fileno())
        if mode is not None:
            # Sur le temporaire, AVANT le rename : le fichier final n'existe
            # alors jamais, même brièvement, avec des droits trop larges.
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
