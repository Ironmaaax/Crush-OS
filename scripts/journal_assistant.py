#!/usr/bin/env python3
# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS,   .
 

"""Ce que l'assistant a journalisé récemment, résumé pour être dit à voix haute.

La machine n'a pas d'écran. Quand quelque chose cloche, la seule façon de le
savoir sans ouvrir une session SSH est de le demander à l'assistant lui-même.

Restitue un COMPTE et les derniers messages notables, pas le journal brut :
cinquante lignes de DEBUG lues à voix haute sont inexploitables, et gonflent le
contexte du modèle pour rien.
"""

from __future__ import annotations

import re
import subprocess

_UNITE = "crush-api"
_FENETRE = "-30min"
# Au-delà, la réponse cesse d'être écoutable.
_MAX_MESSAGES = 5

# Bruit récurrent et sans intérêt : la découverte GPU échoue à chaque démarrage
# sur une machine sans carte graphique, et le modèle d'embarquement prévient
# d'un changement de comportement qu'on a déjà acté.
_BRUIT = re.compile(
    r"DiscoverDevicesForPlatform|device_discovery|mean pooling|"
    r"NotOpenSSLWarning|urllib3",
    re.IGNORECASE,
)
_NIVEAU = re.compile(r"\b(ERROR|WARNING|CRITICAL)\b")


def main() -> int:
    try:
        sortie = subprocess.run(  # noqa: S603 — arguments littéraux, aucun shell
            ["journalctl", "-u", _UNITE, "--since", _FENETRE, "--no-pager"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Journal illisible : {exc}")
        return 1

    if sortie.returncode != 0:
        print(
            "Journal inaccessible — l'utilisateur du service doit pouvoir lire "
            f"le journal de {_UNITE} (groupe systemd-journal ou adm)."
        )
        return 1

    lignes = [ligne for ligne in sortie.stdout.splitlines() if ligne.strip()]
    incidents = [
        ligne for ligne in lignes if _NIVEAU.search(ligne) and not _BRUIT.search(ligne)
    ]

    if not incidents:
        print(
            f"Rien à signaler sur les trente dernières minutes : "
            f"{len(lignes)} lignes journalisées, aucune anomalie."
        )
        return 0

    print(f"{len(incidents)} anomalie(s) sur les trente dernières minutes. Les plus récentes :")
    for ligne in incidents[-_MAX_MESSAGES:]:
        # On ne garde que l'heure et le message : le nom d'hôte et le PID
        # n'apprennent rien et alourdissent la lecture.
        morceaux = ligne.split(" — ", 1)
        message = morceaux[1] if len(morceaux) > 1 else ligne
        heure = ligne[7:15].strip()
        print(f"- {heure} : {message.strip()[:180]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
