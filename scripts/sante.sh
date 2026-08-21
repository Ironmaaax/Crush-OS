#!/usr/bin/env bash
# Copyright (C) 2026 Maxime Song
#
# Surveille l'assistant et prévient quand il ne va pas — parce que rien ne le
# faisait.
#
# CE QUE `systemd` NE VOIT PAS
#
# `crush-api.service` porte `Restart=always` : systemd relance indéfiniment, donc
# l'unité n'entre presque jamais en état `failed` et un `OnFailure=` reste muet.
# Deux pannes lui échappent complètement :
#
#   1. le process vit mais l'API ne répond plus (boucle bloquée, verrou SQLite,
#      épuisement de descripteurs) — pour systemd, tout va bien ;
#   2. le service redémarre en boucle toutes les quelques secondes — systemd
#      fait son travail, personne n'est prévenu, et l'assistant est inutilisable.
#
# Ce script interroge donc l'API pour de vrai, et lit le compteur de
# redémarrages de systemd.
#
# ANTI-SPAM : on alerte sur les TRANSITIONS, pas sur l'état. Un contrôle toutes
# les 5 minutes sur un service mort enverrait douze messages par heure, et la
# première chose qu'on fait alors est de couper les notifications — donc de
# perdre l'alerte utile. Un seul message quand ça tombe, un seul quand ça revient.
#
# CE QUE CE SCRIPT NE PEUT PAS FAIRE : signaler que la machine est éteinte. Rien
# tournant SUR la Pi ne le peut. Il faut un observateur extérieur pour ça.
#
#   bash scripts/sante.sh          # un contrôle
#   bash scripts/sante.sh --etat   # affiche l'état retenu, n'alerte pas

set -uo pipefail

RACINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$RACINE/.env"
UNITE="crush-api.service"

# Hors du dépôt : l'état de surveillance n'a rien à y faire, et un `git clean`
# ne doit pas faire oublier qu'une panne était déjà signalée.
ETAT_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/crush"
ETAT_FICHIER="$ETAT_DIR/sante"

# Au-delà de ce nombre de redémarrages entre deux contrôles, ce n'est plus une
# relance manuelle mais une boucle. En dessous, on se tait : sinon chaque
# `systemctl restart` volontaire déclencherait un message.
SEUIL_BOUCLE=3

lire_env() {
  [ -f "$ENV_FILE" ] || return 0
  grep "^${1}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r' || true
}

PORT="$(lire_env PORT)"; PORT="${PORT:-8000}"

# ── Mesures ───────────────────────────────────────────────────────────────────

api_repond() {
  # /health est public (pas de jeton requis) : c'est justement le point de
  # contrôle prévu pour ça. --max-time 10 : au-delà, l'API est inutilisable même
  # si elle finit par répondre.
  curl -fsS --max-time 10 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1
}

redemarrages() {
  systemctl show "$UNITE" -p NRestarts --value 2>/dev/null | tr -cd '0-9'
}

derniers_logs() {
  journalctl -u "$UNITE" -n 8 --no-pager -o cat 2>/dev/null | tail -8
}

# ── État retenu ───────────────────────────────────────────────────────────────

etat_precedent="ok"
restarts_precedent="0"
if [ -f "$ETAT_FICHIER" ]; then
  # shellcheck disable=SC1090
  . "$ETAT_FICHIER" 2>/dev/null || true
  etat_precedent="${ETAT:-ok}"
  restarts_precedent="${RESTARTS:-0}"
fi

restarts_actuel="$(redemarrages)"; restarts_actuel="${restarts_actuel:-0}"

if [ "${1:-}" = "--etat" ]; then
  printf "  unite            : %s\n" "$UNITE"
  printf "  API repond       : %s\n" "$(api_repond && echo oui || echo NON)"
  printf "  redemarrages     : %s (precedent controle : %s)\n" "$restarts_actuel" "$restarts_precedent"
  printf "  etat retenu      : %s\n" "$etat_precedent"
  printf "  fichier d etat   : %s\n" "$ETAT_FICHIER"
  exit 0
fi

# ── Diagnostic ────────────────────────────────────────────────────────────────

if ! api_repond; then
  etat="panne"
elif [ "$((restarts_actuel - restarts_precedent))" -ge "$SEUIL_BOUCLE" ]; then
  etat="boucle"
else
  etat="ok"
fi

# ── Alerte, uniquement sur transition ─────────────────────────────────────────

if [ "$etat" != "$etat_precedent" ]; then
  case "$etat" in
    panne)
      message="$(printf 'Crush ne repond plus.\n\nL API sur le port %s ne repond pas. Dernieres lignes du journal :\n\n%s' \
                 "$PORT" "$(derniers_logs)")"
      ;;
    boucle)
      message="$(printf 'Crush redemarre en boucle.\n\n%s redemarrages depuis le dernier controle. L API repond, mais le service ne tient pas. Dernieres lignes :\n\n%s' \
                 "$((restarts_actuel - restarts_precedent))" "$(derniers_logs)")"
      ;;
    ok)
      if [ "$etat_precedent" = "panne" ]; then
        message="Crush repond de nouveau."
      else
        message="Crush s est stabilise."
      fi
      ;;
  esac
  bash "$RACINE/scripts/alerte.sh" "$message" || true
fi

# ── Mémorisation ──────────────────────────────────────────────────────────────

mkdir -p "$ETAT_DIR"
printf 'ETAT=%s\nRESTARTS=%s\n' "$etat" "$restarts_actuel" > "$ETAT_FICHIER"

[ "$etat" = "ok" ] && exit 0 || exit 1
