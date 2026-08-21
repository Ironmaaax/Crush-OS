# Copyright (C) 2026 Max Ea
# This file is part of CRUSH-OS,   .


"""Ré-export de kernel.schemas (section Memory) — CDC §A.1.3.

Le foyer canonique des contrats de données Memory Kernel est
`kernel/schemas.py` depuis la Phase A. Ce fichier reste pour préserver
les imports existants (`from memory.schemas import …`) jusqu'à la Phase B.
"""

from __future__ import annotations

from crush.kernel.schemas import (  # noqa: F401
    DecayPolicy,
    Event,
    Fact,
    FactObservation,
    FactRelation,
    FactStatus,
    ObservationType,
    RelationType,
)
