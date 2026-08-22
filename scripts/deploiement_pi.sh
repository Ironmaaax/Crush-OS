#!/usr/bin/env bash
# Copyright (C) 2026 Maxime Song
#
# Déploiement vers le Raspberry Pi : envoi, redémarrage EXPLICITE, vérification.
#
# POURQUOI CE SCRIPT EXISTE
#
# Le Pi n'est pas un dépôt git : on y envoie une archive par SSH. Fait à la main,
# c'est trois commandes dont la dernière — le redémarrage — est celle qu'on
# oublie. Or elle est nécessaire : Python a déjà importé les modules, écraser les
# fichiers ne change rien au processus en cours.
#
# Pire, tant que le rechargement automatique était actif (ENVIRONMENT=development
# côté serveur), l'extraction déclenchait un rechargement NON VOULU, parfois en
# pleine écriture. Mesuré le 22/08/2026 : quinze rechargements en une journée,
# tous provoqués par des déploiements, chacun jetant l'état en mémoire — sessions,
# caches, et le modèle fastembed qui met 25 s à revenir.
#
# Ce script rend le redémarrage inévitable, et vérifie que le service revient.
#
# Usage :
#   scripts/deploiement_pi.sh                 # tout ce que git voit de modifié
#   scripts/deploiement_pi.sh src/crush/app.py …  # une liste explicite
#
# Variables : CRUSH_HOTE (défaut jarvis-pi), CRUSH_DIR, CRUSH_SANTE.

set -euo pipefail

HOTE="${CRUSH_HOTE:-jarvis-pi}"
DIR="${CRUSH_DIR:-/home/jarvis/assistant}"
SANTE="${CRUSH_SANTE:-http://127.0.0.1:8001/api/health}"
SERVICE="${CRUSH_SERVICE:-crush-api}"

G='\033[0;32m'; R='\033[0;31m'; Y='\033[0;33m'; N='\033[0m'
ok()   { printf "  ${G}✓${N} %s\n" "$1"; }
warn() { printf "  ${Y}!${N} %s\n" "$1"; }
die()  { printf "\n  ${R}✗ %s${N}\n\n" "$1" >&2; exit 1; }

cd "$(dirname "$0")/.."

# ── 1. Quels fichiers ────────────────────────────────────────────────────────
if [ "$#" -gt 0 ]; then
    FICHIERS=("$@")
else
    # Modifiés ET non suivis, restreints à ce que le serveur exécute. On exclut
    # les tests : ils ne tournent pas sur le Pi, et les envoyer allonge l'archive
    # sans rien changer au service.
    mapfile -t FICHIERS < <(
        { git diff --name-only HEAD; git ls-files --others --exclude-standard; } \
        | grep -E '^(src/crush|prompts|config|scripts)/' \
        | sort -u
    )
fi

[ "${#FICHIERS[@]}" -gt 0 ] || die "Rien à déployer (aucun fichier modifié sous src/, prompts/, config/, scripts/)."

# Un fichier supprimé localement ferait échouer `tar`. On le signale plutôt que
# de laisser l'archive planter à mi-parcours.
MANQUANTS=()
for f in "${FICHIERS[@]}"; do [ -e "$f" ] || MANQUANTS+=("$f"); done
if [ "${#MANQUANTS[@]}" -gt 0 ]; then
    warn "Ignorés (absents du disque, probablement supprimés) : ${MANQUANTS[*]}"
    RESTANTS=()
    for f in "${FICHIERS[@]}"; do [ -e "$f" ] && RESTANTS+=("$f"); done
    FICHIERS=("${RESTANTS[@]}")
    [ "${#FICHIERS[@]}" -gt 0 ] || die "Plus rien à envoyer."
fi

printf "\n  Déploiement vers %s:%s — %d fichier(s)\n" "$HOTE" "$DIR" "${#FICHIERS[@]}"
for f in "${FICHIERS[@]}"; do printf "    %s\n" "$f"; done
printf "\n"

# ── 2. Vérifier le chemin de redémarrage AVANT d'envoyer ─────────────────────
# Sans cette vérification, on écrase les fichiers puis on découvre qu'on ne peut
# pas redémarrer : le service tourne alors sur l'ancien code avec le nouveau sur
# disque, et RIEN ne le dit.
#
# On cherche une règle NOPASSWD, et non « sudo est-il permis ». Deux pièges
# rencontrés en écrivant ce script :
#   - `systemctl restart --dry-run` réussit sans privilège : il ne prouve rien.
#   - `sudo -l <commande>` répond « autorisé » à cause de la règle générale
#     `(ALL : ALL) ALL`, qui exige justement un mot de passe.
# Seule la présence d'un NOPASSWD couvrant `systemctl restart` garantit un
# redémarrage non interactif.
if ! ssh "$HOTE" "sudo -n -l 2>/dev/null | grep -q 'NOPASSWD.*systemctl restart'"; then
    printf "  ${R}✗ Redémarrage non interactif impossible.${N}\n\n"
    printf "  Le compte peut redémarrer le service, mais sudo réclame un mot de passe —\n"
    printf "  donc pas depuis un script. À exécuter UNE FOIS sur le Pi :\n\n"
    printf "    printf 'jarvis ALL=(root) NOPASSWD: /bin/systemctl restart %s\\\\n' \\\\\n" "$SERVICE"
    printf "      | sudo tee /etc/sudoers.d/crush-restart >/dev/null \\\\\n"
    printf "      && sudo chmod 0440 /etc/sudoers.d/crush-restart \\\\\n"
    printf "      && sudo visudo -c\n\n"
    printf "  La dernière commande VALIDE le fichier. Si elle refuse, supprimer\n"
    printf "  /etc/sudoers.d/crush-restart sans fermer la session : un sudoers\n"
    printf "  invalide bloque sudo entièrement.\n\n"
    printf "  Rien n'a été envoyé.\n\n"
    exit 1
fi
ok "Redémarrage autorisé sans mot de passe"

# ── 3. Envoi ─────────────────────────────────────────────────────────────────
tar czf - "${FICHIERS[@]}" | ssh "$HOTE" "cd '$DIR' && tar xzf -" \
    || die "Envoi échoué : les fichiers distants sont peut-être à moitié écrits. Relancer."
ok "Fichiers extraits"

# ── 4. Redémarrage EXPLICITE ─────────────────────────────────────────────────
ssh "$HOTE" "sudo -n systemctl restart $SERVICE" || die "Redémarrage refusé par systemd."
ok "Service redémarré"

# ── 5. Vérification ─────────────────────────────────────────────────────────
# Le démarrage charge des modèles (TTS, fastembed) : la santé peut mettre
# plusieurs secondes. On boucle plutôt que de dormir une durée devinée.
printf "  Attente de la santé"
for _ in $(seq 1 40); do
    CODE=$(ssh "$HOTE" "curl -s -o /dev/null -w '%{http_code}' '$SANTE'" 2>/dev/null || echo 000)
    if [ "$CODE" = "200" ]; then
        printf "\n"
        ok "Service en ligne (HTTP 200)"

        # Les checksums : la preuve que le service exécute bien ce qu'on a envoyé.
        # Un `tar` qui réussit ne garantit pas que le bon fichier est arrivé au bon
        # endroit — un chemin relatif inattendu suffit à le placer ailleurs.
        ECARTS=0
        for f in "${FICHIERS[@]}"; do
            L=$(sha256sum "$f" | cut -d' ' -f1)
            D=$(ssh "$HOTE" "sha256sum '$DIR/$f' 2>/dev/null" | cut -d' ' -f1)
            [ "$L" = "$D" ] || { warn "Divergent : $f"; ECARTS=$((ECARTS + 1)); }
        done
        [ "$ECARTS" -eq 0 ] && ok "Tous les fichiers identiques en production" \
                            || die "$ECARTS fichier(s) divergent(s) — déploiement incomplet."

        printf "\n  ${G}Déploiement terminé.${N}\n\n"
        exit 0
    fi
    printf "."
    sleep 2
done

printf "\n"
printf "  ${R}✗ Le service ne répond pas après 80 s. Dernières lignes du journal :${N}\n\n"
ssh "$HOTE" "journalctl -u $SERVICE --since '-2min' --no-pager | tail -25"
die "Déploiement en échec — le service est peut-être arrêté. Vérifier avant de continuer."
