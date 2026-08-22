#!/usr/bin/env bash
# Copyright (C) 2026 Maxime Song
#
# Pousse la dernière sauvegarde locale vers le PC Windows de Max, hors machine.
#
# POURQUOI CE SCRIPT EXISTE, ET PAS UN `BACKUP_COPY_TO`
#
# `SauvegardeMemoire._copier_hors_machine` (crush/providers/memory/sauvegarde.py)
# ne fait qu'un `shutil.copy2` vers un CHEMIN LOCAL — utile pour un partage déjà
# monté (NAS, clé USB), pas pour pousser par SSH vers une autre machine. On
# aurait pu monter la destination en SSHFS et pointer `BACKUP_COPY_TO` dessus,
# mais ce montage introduit un piège : si le PC est éteint et le montage a
# échoué, le point de montage reste un DOSSIER LOCAL VIDE — `mkdir` réussit,
# `copy2` écrit dedans, et la sauvegarde se croit hors machine alors qu'elle
# vient d'atterrir sur le support qu'elle est censée protéger, EN SILENCE.
#
# Un script indépendant qui échoue bruyamment (code de sortie non nul) est plus
# honnête qu'un montage qui échoue en silence. C'est aussi le même principe que
# `crush-sante.timer` + `crush-alerte@` : la vérification tourne HORS de
# l'application, en bash + outils système, pour continuer à fonctionner même si
# l'application est en panne.
#
# POURQUOI UN TIRAGE RÉGULIER ET NON UNE HEURE FIXE
#
# La sauvegarde locale tourne à 4h (`BACKUP_HOUR`). Rien ne garantit que le PC
# Windows soit allumé à cette heure précise — un push unique raterait sa
# fenêtre indéfiniment. Ce script tourne toutes les deux heures (systemd timer)
# et repousse la DERNIÈRE archive si elle diffère de la dernière poussée avec
# succès : dès que le PC se rallume, il rattrape son retard, quel que soit le
# nombre de tentatives ratées entre-temps.
#
# Variables : CRUSH_DIR, CRUSH_CIBLE_HOTE, CRUSH_CIBLE_DOSSIER, CRUSH_CLE.

set -euo pipefail

DIR="${CRUSH_DIR:-/home/jarvis/assistant}"
HOTE="${CRUSH_CIBLE_HOTE:-max-pc}"
UTILISATEUR="${CRUSH_CIBLE_UTILISATEUR:-Maxim}"
DOSSIER_DISTANT="${CRUSH_CIBLE_DOSSIER:-CrushBackups}"
CLE="${CRUSH_CLE:-$HOME/.ssh/id_ed25519_backup_pc}"
MARQUEUR="${CRUSH_MARQUEUR:-$HOME/.local/state/crush/derniere-sauvegarde-poussee}"

DERNIERE=$(ls -t "$DIR"/sauvegardes/memoire-*.tar.gz 2>/dev/null | head -1 || true)
if [ -z "$DERNIERE" ]; then
    echo "Aucune archive locale à pousser." >&2
    exit 0
fi

DEJA_POUSSEE=""
[ -f "$MARQUEUR" ] && DEJA_POUSSEE=$(cat "$MARQUEUR")
if [ "$DEJA_POUSSEE" = "$(basename "$DERNIERE")" ]; then
    echo "Déjà à jour ($(basename "$DERNIERE"))."
    exit 0
fi

if ! scp -i "$CLE" -o BatchMode=yes -o ConnectTimeout=10 \
        "$DERNIERE" "${UTILISATEUR}@${HOTE}:${DOSSIER_DISTANT}/" 2>&1; then
    # Échec attendu et FRÉQUENT : le PC est simplement éteint la plupart du
    # temps. Pas d'alerte ici — seule une staleness prolongée en vaut une
    # (cf. offsite_backup_check.sh), sinon chaque nuit sans PC allumé
    # déclencherait un message inutile.
    echo "Push hors machine impossible ($HOTE injoignable ou refusé)." >&2
    exit 1
fi

mkdir -p "$(dirname "$MARQUEUR")"
basename "$DERNIERE" > "$MARQUEUR"
date +%s > "${MARQUEUR}.horodatage"
echo "Poussé : $(basename "$DERNIERE")"
