# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .
 

"""Tests unitaires de l'injection du token API dans le HTML UI (interfaces.api.ui)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from crush.interfaces.api import ui


def _fake_settings(
    enabled: bool,
    token: str,
    wakeup_enabled: bool = False,
    assistant_name: str = "Crush",
) -> SimpleNamespace:
    return SimpleNamespace(
        api_auth_enabled=enabled,
        api_token=SecretStr(token),
        wakeup_enabled=wakeup_enabled,
        display_assistant_name=assistant_name,
    )


def test_inject_assistant_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Le nom configuré doit arriver jusqu'au client, échappé en JSON."""
    monkeypatch.setattr(ui, "settings", _fake_settings(False, "", assistant_name="Vendredi"))
    assert 'window.CRUSH_ASSISTANT_NAME="Vendredi"' in ui.inject_client_config("<head></head>")


def test_inject_wakeup_enabled_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ui, "settings", _fake_settings(False, "", wakeup_enabled=True))
    assert "window.CRUSH_WAKEUP_ENABLED=true" in ui.inject_client_config("<head></head>")
    monkeypatch.setattr(ui, "settings", _fake_settings(False, "", wakeup_enabled=False))
    assert "window.CRUSH_WAKEUP_ENABLED=false" in ui.inject_client_config("<head></head>")


def test_inject_adds_token_when_auth_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ui, "settings", _fake_settings(True, "secret-123"))
    out = ui.inject_client_config("<head></head><body></body>")
    assert "window.CRUSH_API_TOKEN" in out
    assert "secret-123" in out


def test_inject_empty_token_when_auth_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ui, "settings", _fake_settings(False, "secret-123"))
    out = ui.inject_client_config("<head></head>")
    assert 'window.CRUSH_API_TOKEN=""' in out
    assert "secret-123" not in out


def test_inject_before_head_close(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ui, "settings", _fake_settings(True, "tok"))
    out = ui.inject_client_config("<head><title>x</title></head>")
    assert out.index("window.CRUSH_API_TOKEN") < out.index("</head>")


def test_inject_prepends_when_no_head(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ui, "settings", _fake_settings(True, "tok"))
    out = ui.inject_client_config("<body>only</body>")
    assert out.startswith("<script>")
    assert "<body>only</body>" in out


def test_inject_token_is_json_escaped(monkeypatch: pytest.MonkeyPatch) -> None:
    tricky = 'a"b</script>'
    monkeypatch.setattr(ui, "settings", _fake_settings(True, tricky))
    out = ui.inject_client_config("<head></head>")
    assert json.dumps(tricky) in out


def test_inject_defines_api_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ui, "settings", _fake_settings(False, ""))
    out = ui.inject_client_config("<head></head>")
    assert "window.CRUSH_API_BASE" in out
