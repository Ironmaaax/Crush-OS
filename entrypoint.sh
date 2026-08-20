#!/usr/bin/env bash
set -euo pipefail

echo "── CRUSH-OS (conteneur) ──────────────────────────"
echo "API + voice agent — LiveKit Cloud (pas de serveur local)"
echo "────────────────────────────────────────────────────"

cleanup() {
  echo "Arrêt en cours..."
  kill $(jobs -p) 2>/dev/null || true
  wait
}
trap cleanup INT TERM

# API (port 8000)
uv run python -m crush.app &

# Laisse l'API démarrer avant de lancer le voice agent
sleep 4

# Voice agent — se connecte à LiveKit Cloud via LIVEKIT_URL/.env
uv run python -m crush.interfaces.voice.agent dev &

wait
