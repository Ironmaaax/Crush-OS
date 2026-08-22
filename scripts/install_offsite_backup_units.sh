#!/usr/bin/env bash
# Copyright (C) 2026 Maxime Song
#
# Installe les quatre unités systemd du push de sauvegarde hors machine. Les
# scripts et les .service/.timer sont déjà sur le disque (déployés le
# 22/08/2026) ; il ne manque que l'écriture dans /etc/systemd/system, qui
# demande sudo — la règle NOPASSWD posée pour crush-restart ne couvre pas ça,
# volontairement étroite.
#
#   sudo bash scripts/install_offsite_backup_units.sh

set -euo pipefail
cd "$(dirname "$0")/.."

# `whoami` vaudrait "root" ici : le script tourne SOUS sudo, mais les scripts
# qu'il installe doivent s'exécuter en tant que jarvis — c'est son HOME qui
# contient la clé dédiée et son marqueur d'état. Même résolution que
# `install_pi.sh` (`RUN_USER="${SUDO_USER:-$USER}"`), pour ne pas réinventer
# une seconde règle qui diverge de la première.
CRUSH_USER="${SUDO_USER:-$USER}"

for f in deploy/systemd/crush-offsite-backup.service \
         deploy/systemd/crush-offsite-backup.timer \
         deploy/systemd/crush-offsite-backup-check.service \
         deploy/systemd/crush-offsite-backup-check.timer; do
    sed -e "s|__CRUSH_DIR__|$(pwd)|g" -e "s|__CRUSH_USER__|$CRUSH_USER|g" "$f" \
        > "/etc/systemd/system/$(basename "$f")"
    echo "  installé : $(basename "$f")"
done

systemctl daemon-reload
systemctl enable crush-offsite-backup.timer crush-offsite-backup-check.timer
systemctl restart crush-offsite-backup.timer crush-offsite-backup-check.timer

echo ""
echo "Terminé. État :"
systemctl list-timers --no-pager | grep -i offsite || true
