#!/usr/bin/env bash
# Copyright (C) 2026 Maxime Song
#
# Alerte si la sauvegarde hors machine traîne trop derrière — jamais sur un
# simple échec ponctuel.
#
# `offsite_backup.sh` échoue chaque fois que le PC de Max est éteint, ce qui est
# la majeure partie du temps : ce n'est pas une panne, c'est l'état normal d'un
# poste personnel. L'alerter à chaque tentative ratée (toutes les deux heures)
# rendrait la notification inutile en une journée. Ce script suit donc
# l'ANCIENNETÉ du dernier succès, pas le résultat de la dernière tentative — et
# n'alerte que sur la TRANSITION vers/depuis l'état "trop vieux", exactement
# comme sante.sh pour l'API.
#
#   bash scripts/offsite_backup_check.sh          # un contrôle
#   bash scripts/offsite_backup_check.sh --etat   # affiche l'état, n'alerte pas

set -uo pipefail

RACINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MARQUEUR="${CRUSH_MARQUEUR:-$HOME/.local/state/crush/derniere-sauvegarde-poussee}"
ETAT_FICHIER="${XDG_STATE_HOME:-$HOME/.local/state}/crush/offsite"

# Au-delà de ce délai sans copie hors machine réussie, l'archive locale est la
# SEULE copie qui existe. 72h : large pour absorber un week-end sans que le PC
# soit allumé, court pour rester une vraie alerte et non un bruit de fond.
SEUIL_HEURES=72

if [ "${1:-}" = "--etat" ]; then
    if [ -f "${MARQUEUR}.horodatage" ]; then
        age=$(( ( $(date +%s) - $(cat "${MARQUEUR}.horodatage") ) / 3600 ))
        printf "  dernier succès : %s, il y a %sh\n" "$(cat "$MARQUEUR" 2>/dev/null)" "$age"
    else
        printf "  aucun succès enregistré\n"
    fi
    exit 0
fi

etat_precedent="ok"
[ -f "$ETAT_FICHIER" ] && { . "$ETAT_FICHIER" 2>/dev/null || true; etat_precedent="${ETAT:-ok}"; }

if [ ! -f "${MARQUEUR}.horodatage" ]; then
    # Jamais réussi une seule fois : distinct d'un simple retard, et plus urgent
    # — ça peut vouloir dire que la clé, le service SSH ou la config a un
    # problème de fond plutôt qu'un PC juste éteint.
    etat="jamais"
else
    age_h=$(( ( $(date +%s) - $(cat "${MARQUEUR}.horodatage") ) / 3600 ))
    if [ "$age_h" -ge "$SEUIL_HEURES" ]; then etat="perime"; else etat="ok"; fi
fi

if [ "$etat" != "$etat_precedent" ]; then
    case "$etat" in
        jamais)
            message="La sauvegarde hors machine n'a encore jamais réussi. La mémoire n'existe qu'en un seul exemplaire, sur la carte du Pi."
            ;;
        perime)
            message="Aucune sauvegarde hors machine réussie depuis plus de ${SEUIL_HEURES}h. Vérifier que le PC est joignable — la mémoire n'existe qu'en un seul exemplaire en attendant."
            ;;
        ok)
            message="La sauvegarde hors machine a repris."
            ;;
    esac
    bash "$RACINE/scripts/alerte.sh" "$message" || true
fi

mkdir -p "$(dirname "$ETAT_FICHIER")"
printf 'ETAT=%s\n' "$etat" > "$ETAT_FICHIER"

[ "$etat" = "ok" ] && exit 0 || exit 1
