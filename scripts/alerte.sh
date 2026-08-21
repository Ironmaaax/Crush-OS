#!/usr/bin/env bash
# Copyright (C) 2026 Maxime Song
#
# Envoie une alerte à l'utilisateur, quand l'assistant lui-même ne peut plus le
# faire.
#
# POURQUOI CE SCRIPT N'EST PAS EN PYTHON
#
# Il est appelé précisément quand l'application est en panne. S'appuyer sur son
# environnement virtuel, ses dépendances ou sa configuration, c'est risquer que
# le messager tombe pour la même raison que le message. Ici : bash, curl, et la
# lecture directe du .env. Rien d'autre.
#
#   bash scripts/alerte.sh "texte du message"
#
# Sortie 0 si au moins un canal a reçu le message, 1 sinon. Un échec n'est jamais
# bruyant : l'appelant est déjà en train de gérer une panne.

set -uo pipefail

MESSAGE="${1:-}"
[ -z "$MESSAGE" ] && { echo "usage: alerte.sh <message>" >&2; exit 2; }

RACINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$RACINE/.env"

# `tr -d '\r'` : un .env copié depuis Windows arrive en CRLF, et le \r final
# part alors dans l'URL du jeton — l'API Telegram répond 404 sans expliquer
# pourquoi. Même piège que dans install_pi.sh.
lire_env() {
  [ -f "$ENV_FILE" ] || return 0
  grep "^${1}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r' || true
}

TG_TOKEN="$(lire_env TELEGRAM_BOT_TOKEN)"
TG_OWNER="$(lire_env TELEGRAM_OWNER_ID)"

envoye=1

if [ -n "$TG_TOKEN" ] && [ -n "$TG_OWNER" ]; then
  # --max-time : sans plafond, un réseau qui pend bloquerait l'unité systemd
  # indéfiniment. 20 s suffisent largement pour un message texte.
  reponse="$(curl -sS --max-time 20 \
    -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TG_OWNER}" \
    --data-urlencode "text=${MESSAGE}" 2>&1)" || reponse=""
  case "$reponse" in
    *'"ok":true'*) envoye=0 ;;
    # On journalise la DESCRIPTION de l'erreur, jamais l'URL : elle porte le jeton.
    *) printf 'alerte: Telegram a refuse le message (%s)\n' \
         "$(printf '%s' "$reponse" | grep -o '"description":"[^"]*"' | head -1)" >&2 ;;
  esac
else
  echo "alerte: TELEGRAM_BOT_TOKEN ou TELEGRAM_OWNER_ID absent du .env" >&2
fi

exit "$envoye"
