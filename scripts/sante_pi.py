#!/usr/bin/env python3
# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS,   .
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""État matériel de la machine qui héberge l'assistant.

Elle tourne sans écran ni clavier : la seule façon de savoir comment elle va
est de le lui demander. Sortie destinée à être LUE À VOIX HAUTE — des phrases,
pas un tableau, et des chiffres arrondis.

Volontairement sans dépendance à `crush` : ce script doit rester exécutable
même quand le service ne démarre plus, précisément le moment où l'on veut
savoir si la carte chauffe ou si le disque est plein.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# Bits renvoyés par `vcgencmd get_throttled`. Ceux « depuis le démarrage »
# comptent : un throttling passé explique des lenteurs qu'on ne verrait plus.
_DRAPEAUX_THROTTLE = {
    0: "sous-tension en ce moment",
    1: "fréquence bridée en ce moment",
    2: "throttling thermique en ce moment",
    16: "sous-tension survenue depuis le démarrage",
    18: "throttling thermique survenu depuis le démarrage",
}


def _commande(args: list[str]) -> str:
    try:
        return subprocess.run(  # noqa: S603 — arguments littéraux, aucun shell
            args, capture_output=True, text=True, timeout=5, check=False
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _temperature() -> str:
    brut = _commande(["vcgencmd", "measure_temp"])  # temp=60.4'C
    if "=" not in brut:
        return "température indisponible"
    valeur = brut.split("=", 1)[1].replace("'C", "").strip()
    try:
        degres = float(valeur)
    except ValueError:
        return "température indisponible"
    # 80 °C est le seuil où le Pi 5 commence à se brider.
    juge = "normale" if degres < 70 else ("élevée" if degres < 80 else "critique")
    return f"{degres:.0f} degrés, {juge}"


def _throttling() -> str:
    brut = _commande(["vcgencmd", "get_throttled"])  # throttled=0x0
    if "=" not in brut:
        return ""
    try:
        drapeaux = int(brut.split("=", 1)[1], 16)
    except ValueError:
        return ""
    if drapeaux == 0:
        return ""
    actifs = [libelle for bit, libelle in _DRAPEAUX_THROTTLE.items() if drapeaux & (1 << bit)]
    return " ; ".join(actifs) if actifs else ""


def _disque() -> str:
    total, _, libre = shutil.disk_usage("/")
    go = libre / 1024**3
    pct = libre / total * 100
    juge = "" if pct > 20 else " — il commence à se remplir"
    return f"{go:.0f} gigaoctets libres{juge}"


def _memoire() -> str:
    try:
        lignes = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return "mémoire indisponible"
    valeurs = {}
    for ligne in lignes:
        cle, _, reste = ligne.partition(":")
        if cle in {"MemTotal", "MemAvailable"}:
            valeurs[cle] = int(reste.strip().split()[0])
    if len(valeurs) < 2:
        return "mémoire indisponible"
    dispo_go = valeurs["MemAvailable"] / 1024**2
    pct = valeurs["MemAvailable"] / valeurs["MemTotal"] * 100
    return f"{dispo_go:.1f} gigaoctets de mémoire disponibles, soit {pct:.0f} pour cent"


def _uptime() -> str:
    try:
        secondes = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return ""
    jours = int(secondes // 86400)
    heures = int((secondes % 86400) // 3600)
    if jours:
        return f"allumée depuis {jours} jour{'s' if jours > 1 else ''} et {heures} heures"
    return f"allumée depuis {heures} heures"


def main() -> int:
    phrases = [
        f"Température : {_temperature()}.",
        f"Disque : {_disque()}.",
        f"Mémoire : {_memoire()}.",
    ]
    if duree := _uptime():
        phrases.append(f"Machine {duree}.")
    if alerte := _throttling():
        # Placé en dernier : c'est ce qu'on retient d'une réponse parlée.
        phrases.append(f"Attention : {alerte}.")
    print(" ".join(phrases))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
