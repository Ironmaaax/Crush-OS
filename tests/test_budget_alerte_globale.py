# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS,   .
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Alerte de plafond global sur la dépense CONSTATÉE.

RÉGRESSION : `reserve()` n'est appelée que par le worker de mission et
l'exécuteur proactif. La conversation et l'indexation mémoire — l'essentiel de
la facture — n'y passaient jamais, donc le plafond mensuel pouvait être franchi
sans que rien ne se déclenche. Ces tests verrouillent le second chemin.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crush.engine.budget import BudgetGuard
from crush.engine.tracking import UsageTracker
from crush.kernel.events import BudgetThresholdReached, EventBus
from crush.kernel.settings import Settings


class _BusEspion(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.publies: list[BudgetThresholdReached] = []

    async def publish(self, event: object) -> None:  # type: ignore[override]
        if isinstance(event, BudgetThresholdReached):
            self.publies.append(event)


def _guard(
    tmp_path: Path, plafond: float = 1.0, actif: bool = True
) -> tuple[BudgetGuard, _BusEspion]:
    tracker = UsageTracker()
    tracker.CONSO_DIR = tmp_path / "conso"
    tracker.CONSO_DIR.mkdir(parents=True, exist_ok=True)
    reglages = Settings(
        budget_enabled=actif,
        budget_monthly_usd=plafond,
        budget_warn_pct=80.0,
    )
    bus = _BusEspion()
    return BudgetGuard(settings=reglages, tracker=tracker, bus=bus), bus


@pytest.mark.asyncio
async def test_alerte_au_seuil_d_avertissement(tmp_path: Path) -> None:
    guard, bus = _guard(tmp_path, plafond=1.0)

    await guard.noter_depense(0.50)
    assert not bus.publies, "à 50 % du plafond, rien ne doit être signalé"

    await guard.noter_depense(0.35)  # total 0,85 → 85 %
    assert len(bus.publies) == 1
    assert bus.publies[0].scope == "global"
    assert bus.publies[0].ratio == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_une_seule_alerte_par_seuil(tmp_path: Path) -> None:
    """Sans mémoire des seuils franchis, chaque appel LLM alerterait à nouveau."""
    guard, bus = _guard(tmp_path, plafond=1.0)

    await guard.noter_depense(0.85)
    await guard.noter_depense(0.01)
    await guard.noter_depense(0.01)
    assert len(bus.publies) == 1


@pytest.mark.asyncio
async def test_le_plafond_prime_sur_l_avertissement(tmp_path: Path) -> None:
    """Un franchissement direct du plafond ne doit pas produire deux messages."""
    guard, bus = _guard(tmp_path, plafond=1.0)

    await guard.noter_depense(1.20)
    assert len(bus.publies) == 1
    assert bus.publies[0].ratio >= 1.0


@pytest.mark.asyncio
async def test_drapeau_baisse_aucune_alerte(tmp_path: Path) -> None:
    guard, bus = _guard(tmp_path, plafond=1.0, actif=False)

    await guard.noter_depense(5.0)
    assert not bus.publies


@pytest.mark.asyncio
async def test_la_depense_conversationnelle_compte(tmp_path: Path) -> None:
    """Le total global n'est pas réservé aux missions.

    C'était le trou : seul `record("project:…")` alimentait le garde-fou, et le
    contexte « conversation » n'y contribuait pas.
    """
    guard, bus = _guard(tmp_path, plafond=0.10)

    for _ in range(9):
        await guard.noter_depense(0.01)  # 0,09 → 90 %
    assert len(bus.publies) == 1
