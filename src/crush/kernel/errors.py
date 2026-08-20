# Copyright (C) 2026 Max Ea
# This file is part of CRUSH-OS,   .
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Hiérarchie d'exceptions Crush (CDC §A.1.3).

Toute exception métier hérite de `CrushError` — permet aux couches hautes
(interfaces, bootstrap) d'attraper une famille d'erreurs sans connaître les
détails des couches basses.

Recensement des exceptions ad hoc existantes (baseline 2026-06-10) :
- agent.worker_agent._BudgetExceeded → équivalent : kernel.errors.BudgetExceeded

Les autres modules lèvent essentiellement des exceptions stdlib (ValueError,
RuntimeError, OSError, etc.) ou des erreurs des libs (httpx, anthropic, …).
Les Phases B/C feront migrer les call-sites vers cette hiérarchie sans
changer le comportement (cf. CDC §0 règle 5).
"""

from __future__ import annotations


class CrushError(Exception):
    """Racine de toutes les exceptions métier de Crush."""


# ── Couches L1 ────────────────────────────────────────────────────────────────


class LLMError(CrushError):
    """Erreur du provider LLM (timeout, quota, format de réponse, …)."""


class MemoryError_(CrushError):  # noqa: N801 — `_` pour éviter shadowing du builtin MemoryError
    """Erreur de la couche mémoire (lecture/écriture store, FTS, schémas)."""


class ToolError(CrushError):
    """Erreur d'exécution d'un outil."""


class SkillError(CrushError):
    """Erreur de chargement, d'instanciation ou d'exécution d'un skill."""


# ── Couches L2 ────────────────────────────────────────────────────────────────


class BudgetExceeded(CrushError):
    """Budget LLM dépassé pour la mission/session en cours."""


class PermissionDenied(CrushError):
    """Action refusée par la couche de permissions/approvals."""
