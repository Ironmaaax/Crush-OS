# Copyright (C) 2026 Max Ea
# This file is part of CRUSH-OS,   .
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""crush-OS — assistant vocal et exécutif personnel.

Architecture en couches strictes (CDC §2) :
- kernel       — L0, ne dépend de rien du projet
- providers    — L1, implémente les contrats de kernel
- capabilities — L1, outils et skills
- engine       — L2, orchestration (reçoit providers/capabilities par injection)
- interfaces   — L3, points d'entrée (API, channels, voice, UI)
- bootstrap.py — composition root (Phase C)
- app.py       — factory FastAPI (Phase B+)
"""

from __future__ import annotations
