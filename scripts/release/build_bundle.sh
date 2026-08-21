#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

BUNDLE_ROOT="$ROOT/bundle"
VENV_PATH="$BUNDLE_ROOT/.venv"
PYTHON_DIR="$BUNDLE_ROOT/python"
MODELS_DIR="$BUNDLE_ROOT/models"
PIPER_DIR="$MODELS_DIR/piper"
BIN_DIR="$BUNDLE_ROOT/bin"

echo "Crush — build offline bundle"
echo "This script downloads Python deps and models once."
echo ""

if ! command -v uv >/dev/null 2>&1; then
  echo "uv missing — install from https://docs.astral.sh/uv/"
  exit 1
fi

mkdir -p "$BUNDLE_ROOT" "$MODELS_DIR" "$PIPER_DIR" "$BIN_DIR"

# A standalone Python is embedded and the venv is created with the std `venv`
# module (--copies). uv's own venv bakes the base interpreter path into its
# python trampoline, which is NOT relocatable across machines. The std venv
# reads `home` from pyvenv.cfg, so it can be re-homed on the target machine
# (see scripts/release/rehome_bundle.sh).
echo "[1/5] Embed relocatable Python into bundle/python"
rm -rf "$PYTHON_DIR"
PY_INSTALL_CACHE="$BUNDLE_ROOT/.python-install"
rm -rf "$PY_INSTALL_CACHE"
export UV_PYTHON_INSTALL_DIR="$PY_INSTALL_CACHE"
uv python install 3.11
MANAGED_PYTHON="$(find "$PY_INSTALL_CACHE" -name 'python3.11' -type f | head -n 1)"
if [[ -z "$MANAGED_PYTHON" ]]; then
  MANAGED_PYTHON="$(find "$PY_INSTALL_CACHE" -name 'python3' -type f | head -n 1)"
fi
if [[ -z "$MANAGED_PYTHON" ]]; then
  echo "managed python not found after install"
  exit 1
fi
MANAGED_ROOT="$(cd "$(dirname "$MANAGED_PYTHON")/.." && pwd)"
mkdir -p "$PYTHON_DIR"
cp -a "$MANAGED_ROOT/." "$PYTHON_DIR/"
rm -rf "$PY_INSTALL_CACHE"
BUNDLE_BASE_PYTHON="$PYTHON_DIR/bin/python3.11"
[[ -x "$BUNDLE_BASE_PYTHON" ]] || BUNDLE_BASE_PYTHON="$PYTHON_DIR/bin/python3"
if [[ ! -x "$BUNDLE_BASE_PYTHON" ]]; then
  echo "bundle base python missing"
  exit 1
fi

echo "[2/5] Create relocatable venv (std venv --copies)"
rm -rf "$VENV_PATH"
"$BUNDLE_BASE_PYTHON" -m venv --copies "$VENV_PATH"
BUNDLE_PYTHON="$VENV_PATH/bin/python"
if [[ ! -x "$BUNDLE_PYTHON" ]]; then
  echo "bundle venv python missing"
  exit 1
fi

echo "[3/5] Install deps + crush into venv"
uv pip install --python "$BUNDLE_PYTHON" -e ".[vision]"
"$BUNDLE_PYTHON" -c "import crush.setup_app"

echo "[4/5] Copy uv binary"
cp "$(command -v uv)" "$BIN_DIR/uv"
chmod +x "$BIN_DIR/uv"

echo "[5/5] Download ML models"
if [[ ! -f yolov8n.pt ]]; then
  uv run --python "$BUNDLE_PYTHON" python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"
fi
cp yolov8n.pt "$MODELS_DIR/yolov8n.pt"

PIPER_ONNX="$PIPER_DIR/fr_FR-upmc-medium.onnx"
PIPER_JSON="${PIPER_ONNX}.json"
if [[ ! -f "$PIPER_ONNX" ]]; then
  BASE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/fr/fr_FR/upmc/medium"
  curl -L --silent -o "$PIPER_ONNX" "${BASE_URL}/fr_FR-upmc-medium.onnx"
  curl -L --silent -o "$PIPER_JSON" "${BASE_URL}/fr_FR-upmc-medium.onnx.json"
fi

# `uname -s` est conserve ici : le manifeste ci-dessous s en sert pour son champ
# `platform`. Il etait defini dans l etape de telechargement de livekit-server,
# retiree avec le reste du pipeline LiveKit.
OS="$(uname -s)"

echo "Write manifest"
cat > "$BUNDLE_ROOT/manifest.json" <<EOF
{
  "version": "2",
  "platform": "$(echo "$OS" | tr '[:upper:]' '[:lower:]')",
  "python": "3.11",
  "venv": ".venv",
  "python_home": "python",
  "relocatable": true,
  "models": {
    "yolo": "models/yolov8n.pt",
    "piper_onnx": "models/piper/fr_FR-upmc-medium.onnx",
    "piper_json": "models/piper/fr_FR-upmc-medium.onnx.json"
  },
  "bin": {
    "uv": "bin/uv"
  }
}
EOF

echo ""
echo "Bundle ready: $BUNDLE_ROOT"
echo "Next: ./crush eclosion"
