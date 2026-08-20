# Copyright (C) 2026 Max Ea
# This file is part of CRUSH-OS,   .
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Shim de compatibilité racine — voir CDC §B.2 (4).

Le vrai code vit dans `src/crush/app.py`. Ce shim existe pour les
appelants externes qui font encore `python main.py`. Retrait → BACKLOG
(post-refonte).

À partir de B.4, le CLI `crush` et le Makefile invoquent
`python -m crush.app` directement et ne dépendent plus de ce shim.

L'export `app` (instance FastAPI) est re-exporté pour les snippets qui
font `from main import app` (notamment scripts/migration/snapshot_routes.py
avant que `from crush.app import app` ne devienne disponible).
"""

from __future__ import annotations

from crush.app import app, main  # noqa: F401

if __name__ == "__main__":
    main()
