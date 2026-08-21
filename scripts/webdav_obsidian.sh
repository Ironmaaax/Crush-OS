#!/usr/bin/env bash
# Copyright (C) 2026 Maxime Song
#
# Rend le miroir de la mémoire lisible et corrigeable depuis Obsidian, téléphone
# compris.
#
# LE PROBLÈME QUE ÇA RÉSOUT
#
# Le miroir Markdown vit sur la Pi. Obsidian sur téléphone veut un coffre LOCAL,
# et Obsidian Sync ne relie que des clients Obsidian entre eux — la Pi n'en est
# pas un, et ne peut pas l'être : il n'existe pas d'Obsidian sans interface.
# Syncthing ferait l'affaire sur Android, mais n'existe pas sur iOS.
#
# Ce qui marche sur les deux : la Pi sert le dossier en WebDAV, et le greffon
# « Remotely Save » d'Obsidian s'y synchronise. Le service écoute uniquement sur
# la boucle locale ; `tailscale serve` s'occupe du chiffrement et limite l'accès
# au tailnet.
#
#   bash scripts/webdav_obsidian.sh            # installe et démarre
#   bash scripts/webdav_obsidian.sh --etat     # ce qui tourne, sans rien changer
#   bash scripts/webdav_obsidian.sh --mdp      # régénère le mot de passe
#
# Idempotent : relancé, il ne recrée pas le mot de passe (le téléphone serait
# déconnecté sans prévenir) et ne touche pas à la configuration tailscale
# existante.

set -uo pipefail

RACINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FICHIER_ENV="$RACINE/.env.webdav"
UNITE="crush-webdav.service"
PORT_LOCAL=8002
PORT_TAILNET=10000
UTILISATEUR="crush"

vert() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
alerte() { printf '  \033[33m!\033[0m %s\n' "$1"; }
mort() { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

# ── État ──────────────────────────────────────────────────────────────────────

if [ "${1:-}" = "--etat" ]; then
  printf "  unité            : %s (%s)\n" "$UNITE" "$(systemctl is-active "$UNITE" 2>/dev/null || echo inactive)"
  printf "  identifiants     : %s\n" "$([ -f "$FICHIER_ENV" ] && echo présents || echo ABSENTS)"
  printf "  écoute locale    : %s\n" \
    "$(ss -ltn 2>/dev/null | grep -q ":$PORT_LOCAL" && echo "127.0.0.1:$PORT_LOCAL" || echo NON)"
  printf "  adresse tailnet  : %s\n" \
    "$(tailscale serve status 2>/dev/null | grep -o "https://[^ ]*:$PORT_TAILNET" | head -1 || echo "non configurée")"
  exit 0
fi

command -v rclone >/dev/null 2>&1 || mort "rclone absent — « sudo apt-get install -y rclone »."
command -v tailscale >/dev/null 2>&1 || alerte "tailscale absent : le service ne sera joignable que sur la machine."
[ -d "$RACINE/memory_data/mirror" ] || mort "memory_data/mirror/ introuvable — l'assistant a-t-il déjà tourné ?"

# ── Identifiants ──────────────────────────────────────────────────────────────

genere_mdp() {
  # openssl plutôt que /dev/urandom + tr : présent partout où rclone l'est, et le
  # jeu de caractères est sûr pour une URL — le mot de passe finira collé dans un
  # champ de configuration Obsidian, puis dans une requête HTTP Basic.
  openssl rand -base64 24 | tr -d '/+=' | cut -c1-24
}

if [ "${1:-}" = "--mdp" ] || [ ! -f "$FICHIER_ENV" ]; then
  if [ -f "$FICHIER_ENV" ]; then
    alerte "Mot de passe régénéré : il faudra le remettre dans Remotely Save, sur CHAQUE appareil."
  fi
  MDP="$(genere_mdp)"
  # Écrit avant le chmod, mais avec un umask qui interdit déjà les autres : sans
  # ça, le secret existerait en lisible-par-tous pendant un instant.
  (umask 077; printf 'RCLONE_USER=%s\nRCLONE_PASS=%s\n' "$UTILISATEUR" "$MDP" > "$FICHIER_ENV")
  chmod 600 "$FICHIER_ENV"
  vert "Identifiants écrits dans .env.webdav (600)"
else
  MDP="$(grep '^RCLONE_PASS=' "$FICHIER_ENV" | cut -d= -f2- | tr -d '\r')"
  vert "Identifiants existants conservés"
fi

# ── Service ───────────────────────────────────────────────────────────────────

if [ ! -f "/etc/systemd/system/$UNITE" ]; then
  sed -e "s|__CRUSH_DIR__|$RACINE|g" -e "s|__CRUSH_USER__|$(id -un)|g" \
      "$RACINE/deploy/systemd/$UNITE" | sudo tee "/etc/systemd/system/$UNITE" >/dev/null
  vert "$UNITE installée"
fi
sudo systemctl daemon-reload
sudo systemctl enable "$UNITE" >/dev/null 2>&1
sudo systemctl restart "$UNITE"

# On ne se contente pas de « active » : un service qui démarre puis refuse toutes
# les requêtes serait annoncé comme fonctionnel.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  code="$(curl -s -o /dev/null -w '%{http_code}' -u "$UTILISATEUR:$MDP" \
          -X PROPFIND -H 'Depth: 0' "http://127.0.0.1:$PORT_LOCAL/" 2>/dev/null)"
  [ "$code" = "207" ] && break
  sleep 1
done
[ "$code" = "207" ] || mort "le service ne répond pas correctement (HTTP $code) — « journalctl -u $UNITE -n 30 »."
vert "WebDAV répond sur 127.0.0.1:$PORT_LOCAL"

# Et il doit REFUSER sans identifiants : c'est la seule chose qui protège la
# mémoire d'un autre appareil du tailnet.
sans="$(curl -s -o /dev/null -w '%{http_code}' -X PROPFIND -H 'Depth: 0' "http://127.0.0.1:$PORT_LOCAL/" 2>/dev/null)"
[ "$sans" = "401" ] || mort "le serveur accepte les requêtes SANS mot de passe (HTTP $sans) — interrompu."
vert "Accès sans identifiants refusé (401)"

# ── Accès depuis le tailnet ───────────────────────────────────────────────────

ADRESSE=""
if command -v tailscale >/dev/null 2>&1; then
  if ! tailscale serve status 2>/dev/null | grep -q ":$PORT_TAILNET"; then
    sudo tailscale serve --bg "--https=$PORT_TAILNET" "http://127.0.0.1:$PORT_LOCAL" >/dev/null 2>&1 \
      && vert "tailscale serve configuré sur $PORT_TAILNET" \
      || alerte "tailscale serve a échoué — le service reste joignable en local."
  else
    vert "tailscale serve déjà configuré sur $PORT_TAILNET"
  fi
  ADRESSE="$(tailscale serve status 2>/dev/null | grep -o "https://[^ ]*:$PORT_TAILNET" | head -1)"
fi

# ── Ce qu'il reste à faire à la main, dans Obsidian ───────────────────────────

cat <<FIN

  ── À reporter dans Obsidian, sur chaque appareil ──────────────────────────

  Greffon : Remotely Save (Paramètres → Modules complémentaires → Parcourir)

    Service         WebDAV
    Adresse         ${ADRESSE:-http://127.0.0.1:$PORT_LOCAL}
    Utilisateur     $UTILISATEUR
    Mot de passe    $MDP

  Le mot de passe est aussi dans .env.webdav, lisible par toi seul.

  Ce que le téléphone doit pouvoir faire pour que ça marche : joindre le
  tailnet. Si Tailscale n'y est pas installé et connecté, l'adresse ci-dessus
  ne répondra pas — c'est le seul chemin, et c'est voulu.

FIN
