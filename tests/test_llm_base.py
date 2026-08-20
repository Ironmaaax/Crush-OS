# Copyright (C) 2026 Max Ea
# This file is part of CRUSH-OS,   .
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

from __future__ import annotations

from crush.providers.llm.base import LLMProvider
from crush.providers.llm.factory import get_llm_provider


def test_factory_returns_provider() -> None:
    """Vérifie que la factory instancie un LLMProvider."""
    provider = get_llm_provider()
    assert isinstance(provider, LLMProvider)


def test_provider_has_required_methods() -> None:
    """Vérifie que le provider implémente l'interface."""
    provider = get_llm_provider()
    assert callable(provider.complete)
    assert callable(provider.health_check)
