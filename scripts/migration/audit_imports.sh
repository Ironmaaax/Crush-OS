#!/usr/bin/env bash
# Audit des imports différés inter-packages — thermomètre de la migration.
#
# - Section 1 : imports différés (`from <pkg>` à l'intérieur d'un FunctionDef),
#   détectés via AST par scripts/migration/audit_imports.py — pas par grep
#   ligne-à-ligne. L'AST ignore correctement les chaînes/docstrings, donc
#   un compteur à 0 reste à 0 (sans « faux positifs connus »).
# - Section 2 : fichiers concernés (compte unique).
# - Section 3 : imports DYNAMIQUES (`__import__(...)`, `import_module(...)`).
#   Ces appels sont des STRINGS arguments → grep est ici le bon outil.
#
# ─── HISTORIQUE DU GEL (du gel A → GATE C4) ────────────────────────────────
# La version Phase A → C4 utilisait un grep `^[[:space:]]+from (PKGS)`. Ce
# grep matchait aussi du texte à l'intérieur des docstrings et des chaînes
# (textwrap.dedent générant des skill.py, f-strings multilignes). Tant que
# C4 visait « < 15 », ces faux positifs étaient acceptables. À la sortie
# de C (compteur à 3 dont 3 faux positifs), la dette d'imports différés
# est nulle ; le gardien permanent doit afficher 0 (cf. décision Barth :
# « Quand c'est 0, il doit afficher 0 »). Bascule vers un parser AST.
#
# Critères INCLUS dans le compteur (cf. audit_imports.py) :
#   - ast.ImportFrom dont module commence par un des PKGS,
#   - avec un ancêtre FunctionDef / AsyncFunctionDef (lazy au sens runtime),
#   - hors de tout bloc `if TYPE_CHECKING:` (zéro coût autorisé),
#   - sans annotation `# lazy: <raison>` sur la ligne (échappement explicite).
# Scope fichiers : `**/*.py` hors `.git/`, `.venv/`, `__pycache__/`,
# `workspace/`, `tests/` — identique au gel A.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "== Imports différés inter-packages (statiques) =="
uv run python "$SCRIPT_DIR/audit_imports.py" "$REPO_ROOT" \
  | tee /tmp/lazy_imports.txt | wc -l

echo "== Fichiers concernés =="
cut -d: -f1 /tmp/lazy_imports.txt | sort -u | wc -l

echo "== Imports DYNAMIQUES (échappent aux greps ^from — à réécrire aussi en Phase B) =="
grep -rnE "import_module\(|__import__\(" --include="*.py" "$REPO_ROOT" \
  --exclude-dir=.git --exclude-dir=tests --exclude-dir=.venv \
  --exclude-dir=__pycache__ --exclude-dir=workspace \
  | grep -E '"(core|llm|memory|tools|skills|agent|api|proactive|background|channels|audio|vision)\.'
