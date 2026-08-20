# Copyright (C) 2026 Max Ea
# This file is part of CRUSH-OS,   .
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

from __future__ import annotations

from collections.abc import Iterator

import pytest

from crush.kernel.settings import Settings


@pytest.fixture
def local_mode() -> Iterator[None]:
    """Bascule settings en mode local pour la durée du test."""
    from crush.kernel.settings import settings

    old = settings.llm_provider
    object.__setattr__(settings, "llm_provider", "local")
    yield
    object.__setattr__(settings, "llm_provider", old)


@pytest.fixture
def api_mode() -> Iterator[None]:
    """Bascule settings en mode api pour la durée du test."""
    from crush.kernel.settings import settings

    old = settings.llm_provider
    object.__setattr__(settings, "llm_provider", "api")
    yield
    object.__setattr__(settings, "llm_provider", old)


def test_is_offline_mode_local(local_mode: None) -> None:
    """is_offline_mode() retourne True quand llm_provider == 'local'."""
    from crush.kernel.connectivity import is_offline_mode

    assert is_offline_mode() is True


def test_is_offline_mode_api(api_mode: None) -> None:
    """is_offline_mode() retourne False quand llm_provider == 'api'."""
    from crush.kernel.connectivity import is_offline_mode

    assert is_offline_mode() is False


def test_settings_llm_provider_is_valid() -> None:
    """llm_provider doit valoir 'api' ou 'local'."""
    s = Settings()
    assert s.llm_provider in ("api", "local")
