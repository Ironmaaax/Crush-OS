# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .


"""Chemins runtime du projet — source de vérité unique (CDC §B.5).

Ce module évite le piège n°1 de la migration Phase B : tous les
`Path(__file__).parent...` qui cassent dès que le fichier source se déplace.

`PROJECT_ROOT` est résolu en remontant depuis ce fichier jusqu'au premier
parent contenant `pyproject.toml`. Robuste à la profondeur du package
(que `kernel/paths.py` soit à `kernel/`, `src/crush/kernel/` ou ailleurs).

Toute constante de chemin du projet vit ici. Plus AUCUN `Path("memory_data/...")`
ou `Path(__file__).parent.parent / "..."` ne doit subsister hors de ce module
en fin de Phase B (GATE B7a vérifie).
"""

from __future__ import annotations

import os
from pathlib import Path

# Surcharge explicite, pour les contextes où la remontée ne peut pas aboutir.
_ENV_PROJECT_ROOT = "CRUSH_PROJECT_ROOT"


def _find_project_root() -> Path:
    """Racine du projet : surcharge d'environnement, sinon remontée vers pyproject.toml.

    La remontée est plus robuste qu'un chemin relatif à profondeur fixe, mais
    elle suppose que `pyproject.toml` soit atteignable. Ce n'est pas le cas
    dans le conteneur du banc d'essai, qui monte `src/` seul et délibérément :
    monter la racine y exposerait le `.env`. L'import de n'importe quel module
    crush y échouait donc AVANT toute vérification, et le Skill Lab rendait
    « SkillBase inimportable » sans pouvoir dire pourquoi.
    """
    surcharge = os.environ.get(_ENV_PROJECT_ROOT, "").strip()
    if surcharge:
        chemin = Path(surcharge).resolve()
        if chemin.is_dir():
            return chemin

    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    raise RuntimeError(
        f"PROJECT_ROOT introuvable : aucun pyproject.toml trouvé en remontant depuis {here}. "
        f"Dans un contexte où la racine n'est pas atteignable (conteneur ne montant que "
        f"`src/`), renseignez {_ENV_PROJECT_ROOT} avec un répertoire inscriptible."
    )


PROJECT_ROOT: Path = _find_project_root()

# ── Données utilisateur (gitignored, hors package) ─────────────────────────
MEMORY_DATA_DIR: Path = PROJECT_ROOT / "memory_data"
SKILLS_DATA_DIR: Path = PROJECT_ROOT / "skills_data"
SKILLS_INSTALLED_DIR: Path = SKILLS_DATA_DIR / "installed"
SKILLS_CANDIDATES_DIR: Path = SKILLS_DATA_DIR / "candidates"
VISION_DATA_DIR: Path = PROJECT_ROOT / "vision_data"
FACES_DIR: Path = VISION_DATA_DIR / "faces"
WORKSPACE_DIR: Path = PROJECT_ROOT / "workspace"

# ── Assets / code-as-data (trackés en git) ────────────────────────────────
PROMPTS_DIR: Path = PROJECT_ROOT / "prompts"
CONFIG_DIR: Path = PROJECT_ROOT / "config"
NOTICES_DIR: Path = PROJECT_ROOT / "notices"

# ── UI statique (déplacée vers src/crush/interfaces/ui/ en B) ──────────────
UI_DIR: Path = PROJECT_ROOT / "src" / "crush" / "interfaces" / "ui"
UI_STATIC_DIR: Path = UI_DIR / "static"
