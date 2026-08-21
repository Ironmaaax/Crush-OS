# Copyright (C) 2026 Max Ea
# This file is part of CRUSH-OS,   .


"""Shim de compatibilité racine — voir CDC §B.2 (4).

Le vrai code vit dans `src/crush/interfaces/voice/agent.py`. Ce shim
existe pour les appelants externes qui font encore `python voice_agent.py
dev`. Retrait → BACKLOG (post-refonte).

À partir de la Phase B.4, le CLI `crush` et le Makefile invoquent
`python -m crush.interfaces.voice.agent` directement et ne dépendent
plus de ce shim.
"""

from __future__ import annotations

from crush.interfaces.voice.agent import main

if __name__ == "__main__":
    main()
