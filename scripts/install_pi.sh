#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Installation de l'assistant sur Raspberry Pi (Debian/Raspberry Pi OS, aarch64)
# pour un fonctionnement permanent, piloté par systemd.
#
#   bash scripts/install_pi.sh
#
# Le script est IDEMPOTENT : le relancer met à jour l'installation sans rien
# détruire. Il ne touche jamais à un .env existant sauf pour compléter les
# clés manquantes, et ne remplace jamais un jeton déjà généré.
#
# Ce qu'il fait :
#   1. vérifie la plateforme et refuse tôt si elle ne convient pas ;
#   2. installe les paquets système du cœur (aucune compilation) ;
#   3. installe uv, puis synchronise les dépendances Python (cœur seul) ;
#   4. génère un jeton d'accès et active l'authentification si besoin ;
#   5. installe livekit-server si le pipeline vocal est local ;
#   6. installe et active les unités systemd ;
#   7. affiche la marche à suivre pour l'accès HTTPS via Tailscale.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
UNIT_DIR="/etc/systemd/system"
RUN_USER="${SUDO_USER:-$USER}"

B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
step()  { printf "\n${B}── %s${N}\n" "$1"; }
ok()    { printf "  ${G}✓${N} %s\n" "$1"; }
warn()  { printf "  ${Y}!${N} %s\n" "$1"; }
die()   { printf "\n  ${R}✗ %s${N}\n\n" "$1" >&2; exit 1; }

# ── 1. Plateforme ─────────────────────────────────────────────────────────────
step "Vérification de la plateforme"

[ "$(uname -s)" = "Linux" ] || die "Ce script cible Linux. Sur Windows, utilise setup.bat."

ARCH="$(uname -m)"
case "$ARCH" in
  aarch64|arm64) ok "Architecture $ARCH (Raspberry Pi 64 bits)" ;;
  x86_64)        warn "Architecture $ARCH — ça marchera, mais ce script vise le Pi." ;;
  armv7l)        die "Noyau 32 bits détecté. Il faut Raspberry Pi OS 64 bits :
       plusieurs dépendances (onnxruntime, fastembed) n'ont pas de roue armv7." ;;
  *)             warn "Architecture $ARCH non testée." ;;
esac

command -v systemctl >/dev/null 2>&1 || die "systemd introuvable — ce script en dépend."

TOTAL_RAM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)
if [ "$TOTAL_RAM_MB" -gt 0 ] && [ "$TOTAL_RAM_MB" -lt 3500 ]; then
  warn "RAM détectée : ${TOTAL_RAM_MB} Mo. Le socle vise 4 Go et plus."
fi

# ── 2. Paquets système ────────────────────────────────────────────────────────
step "Paquets système"

# libportaudio2 : `sounddevice` est une dépendance dure de livekit-agents. Le
# module est chargé même sans micro sur la machine ; sans la bibliothèque, il
# lève OSError à l'import. On installe donc la lib d'exécution (quelques
# centaines de Ko), PAS portaudio19-dev qui n'apporte que les en-têtes de
# compilation dont on n'a plus besoin.
APT_PACKAGES=(libportaudio2 ca-certificates curl git)

MISSING=()
for pkg in "${APT_PACKAGES[@]}"; do
  dpkg -s "$pkg" >/dev/null 2>&1 || MISSING+=("$pkg")
done

if [ ${#MISSING[@]} -gt 0 ]; then
  printf "  Installation : %s\n" "${MISSING[*]}"
  sudo apt-get update -qq
  sudo apt-get install -y --no-install-recommends "${MISSING[@]}"
fi
ok "Paquets système en place"

# ── 3. uv + dépendances Python ────────────────────────────────────────────────
step "Dépendances Python"

if ! command -v uv >/dev/null 2>&1; then
  printf "  Installation de uv…\n"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || die "uv introuvable après installation. Ajoute ~/.local/bin au PATH."
ok "uv $(uv --version | awk '{print $2}')"

cd "$PROJECT_DIR"
# Cœur seul : pas de vision (torch), pas de face (dlib), pas de local-audio.
# Cf. l'en-tête de pyproject.toml pour le détail des exclusions.
printf "  uv sync (cœur seul, sans extras)…\n"
uv sync --frozen
ok "Environnement Python prêt"

# Assets front lourds (MediaPipe + modèles, ~64 Mo). Hors git : sans eux
# l'interface irait les chercher chez un tiers à chaque chargement de page, ce
# qu'on a justement supprimé. Non bloquant : seules la reconnaissance faciale,
# la séquence de réveil et la détection de gestes en dépendent.
printf "  Assets front (MediaPipe, ~64 Mo)…\n"
if uv run python scripts/vendor_assets.py; then
  ok "Assets front vérifiés"
else
  warn "Assets front non récupérés — reconnaissance faciale et gestes indisponibles.
       Relance plus tard : uv run python scripts/vendor_assets.py"
fi

# ── 4. Configuration ──────────────────────────────────────────────────────────
step "Configuration"

if [ ! -f "$ENV_FILE" ]; then
  cp "$PROJECT_DIR/.env.example" "$ENV_FILE"
  ok ".env créé depuis .env.example"
fi

# Écrit une clé dans .env : remplace la ligne si elle existe, l'ajoute sinon.
set_env() {
  local key="$1" value="$2"
  if grep -q "^${key}=" "$ENV_FILE"; then
    local tmp; tmp="$(mktemp)"
    grep -v "^${key}=" "$ENV_FILE" > "$tmp"
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
    mv "$tmp" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

current_env() {
  # `tr -d '\r'` n'est pas cosmétique : un .env copié depuis Windows arrive en
  # CRLF, et `LIVEKIT_URL=` y vaut "\r" — non vide. Sans ce filtre, le test
  # `-z` échoue et l'installateur conclut à tort à un LiveKit distant, donc
  # n'installe pas le serveur local et laisse le service vocal en boucle
  # d'échec. Constaté au premier déploiement réel sur le Pi.
  grep "^${1}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r' || true
}

normalize_env_line_endings() {
  # python-dotenv tolère le CRLF, mais pas ce script, ni un futur `source .env`.
  if grep -q $'\r' "$ENV_FILE" 2>/dev/null; then
    sed -i 's/\r$//' "$ENV_FILE"
    ok "Fins de ligne du .env normalisées (CRLF → LF)"
  fi
}

# Appelé ici, APRÈS la définition des fonctions : bash résout les appels à
# l'exécution, un appel placé plus haut échouerait en « command not found ».
normalize_env_line_endings

# Jeton d'accès — généré une seule fois. On ne l'écrase JAMAIS : le regénérer
# déconnecterait tous les appareils déjà appairés (il signe les cookies).
TOKEN="$(current_env API_TOKEN)"
if [ -z "$TOKEN" ]; then
  TOKEN="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  set_env API_TOKEN "$TOKEN"
  ok "Jeton d'accès généré"
else
  ok "Jeton d'accès existant conservé"
fi

# L'assistant devient joignable depuis le tailnet : l'authentification n'est
# plus optionnelle (cf. engine/auth.py).
set_env API_AUTH_ENABLED true
set_env HOST 0.0.0.0
ok "Authentification activée, écoute sur toutes les interfaces"

PORT="$(current_env PORT)"; PORT="${PORT:-8000}"

chmod 600 "$ENV_FILE"
ok "Permissions de .env restreintes au propriétaire"

# ── 5. LiveKit ────────────────────────────────────────────────────────────────
step "Pipeline vocal"

LIVEKIT_URL="$(current_env LIVEKIT_URL)"
LIVEKIT_IS_LOCAL=false
if [ -z "$LIVEKIT_URL" ] || printf '%s' "$LIVEKIT_URL" | grep -qE '127\.0\.0\.1|localhost'; then
  LIVEKIT_IS_LOCAL=true
fi

if [ "$LIVEKIT_IS_LOCAL" = true ]; then
  if ! command -v livekit-server >/dev/null 2>&1; then
    printf "  Installation de livekit-server…\n"
    bash "$PROJECT_DIR/scripts/ensure_livekit.sh" || warn "Installation de livekit-server échouée — la voix restera indisponible."
  fi
  command -v livekit-server >/dev/null 2>&1 && ok "livekit-server local" || warn "livekit-server absent"
else
  ok "LiveKit distant configuré ($LIVEKIT_URL) — pas de serveur local"
fi

# ── 6. Services systemd ───────────────────────────────────────────────────────
step "Services systemd"

install_unit() {
  local name="$1"
  sed -e "s|__CRUSH_DIR__|$PROJECT_DIR|g" \
      -e "s|__CRUSH_USER__|$RUN_USER|g" \
      "$PROJECT_DIR/deploy/systemd/$name" | sudo tee "$UNIT_DIR/$name" >/dev/null
  ok "$name installé"
}

install_unit crush-api.service
install_unit crush-voice.service
[ "$LIVEKIT_IS_LOCAL" = true ] && install_unit crush-livekit.service

sudo systemctl daemon-reload

ENABLED_UNITS=(crush-api.service crush-voice.service)
[ "$LIVEKIT_IS_LOCAL" = true ] && ENABLED_UNITS=(crush-livekit.service "${ENABLED_UNITS[@]}")

sudo systemctl enable "${ENABLED_UNITS[@]}" >/dev/null 2>&1
sudo systemctl restart "${ENABLED_UNITS[@]}"
ok "Services activés au démarrage et lancés"

# ── 7. Accès réseau ───────────────────────────────────────────────────────────
step "Accès depuis le téléphone et le PC"

TS_HOST=""
if command -v tailscale >/dev/null 2>&1; then
  TS_HOST="$(tailscale status --json 2>/dev/null \
    | grep -o '"DNSName"[[:space:]]*:[[:space:]]*"[^"]*"' \
    | head -1 | cut -d'"' -f4 | sed 's/\.$//')" || true
fi

cat <<EOF

  ${B}Jeton d'accès${N} — à saisir UNE fois par appareil, sur /login :

      ${B}${TOKEN}${N}

EOF

if [ -n "$TS_HOST" ]; then
  cat <<EOF
  ${B}Tailscale détecté${N} : ${TS_HOST}

  Le micro du navigateur exige un contexte sécurisé : en HTTP simple, Chrome
  et Safari refusent getUserMedia, donc la voix ne démarrera pas. Tailscale
  fournit un vrai certificat, ce qui règle le problème sans bricolage :

      ${B}sudo tailscale serve --bg --https=443 ${PORT}${N}

  L'assistant sera alors sur :

      ${B}https://${TS_HOST}/${N}

  Cette URL fonctionne depuis le téléphone en 4G comme en Wi-Fi, sans ouvrir
  le moindre port sur ta box.
EOF
else
  cat <<EOF
  ${Y}Tailscale n'est pas installé.${N} Sans lui, l'assistant n'est joignable
  qu'en HTTP sur le réseau local — et le micro du navigateur ne fonctionnera
  pas, faute de contexte sécurisé.

      ${B}curl -fsSL https://tailscale.com/install.sh | sh${N}
      ${B}sudo tailscale up${N}
      ${B}sudo tailscale serve --bg --https=443 ${PORT}${N}

  Puis relance ce script pour afficher l'URL finale.
EOF
fi

cat <<EOF

  ${B}Suivi${N} :
      journalctl -u crush-api -f        logs de l'API
      journalctl -u crush-voice -f      logs du pipeline vocal
      systemctl status crush-api        état du service

EOF
