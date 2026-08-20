# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests du garde-fou réseau — `engine/auth.py`.

Deux étages :
  - des tests unitaires des primitives (cookie, jeton, Origin), sans HTTP,
    donc exécutés par la lane rapide à chaque push ;
  - des tests d'intégration du vrai middleware HTTP, marqués `integration`.

Ce module a été réécrit avec le modèle cookie + Origin. L'ancienne version
affirmait que les pages HTML de l'UI restaient servies sans jeton — ce qui
ÉTAIT la faille : ces pages injectent `window.CRUSH_API_TOKEN` en clair,
donc quiconque atteignait le port repartait avec le jeton.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from crush.engine import auth
from crush.kernel import network
from crush.kernel.settings import settings

_TOKEN = "jeton-de-test-0123456789abcdef"

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def auth_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Auth activée, avec `testserver` (base_url de TestClient) comme hôte connu."""
    monkeypatch.setattr(settings, "api_auth_enabled", True)
    monkeypatch.setattr(settings, "api_token", SecretStr(_TOKEN))
    monkeypatch.setattr(settings, "api_allowed_origins", "testserver")
    monkeypatch.setattr(settings, "session_max_age_days", 30)
    monkeypatch.setattr(settings, "environment", "development")
    network.allowed_hosts.cache_clear()
    yield
    network.allowed_hosts.cache_clear()


@pytest.fixture
def auth_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "api_auth_enabled", False)
    monkeypatch.setattr(settings, "api_token", SecretStr(""))
    yield


# ─────────────────────────────────────────────────────────────────────────────
# Unitaires — primitives (lane rapide)
# ─────────────────────────────────────────────────────────────────────────────


def test_cookie_emis_est_valide(auth_on: None) -> None:
    assert auth.session_cookie_valid(auth.issue_session_cookie()) is True


def test_cookie_falsifie_est_rejete(auth_on: None) -> None:
    """Changer la signature invalide le cookie — c'est tout l'intérêt du HMAC."""
    cookie = auth.issue_session_cookie()
    expiry, _, signature = cookie.partition(".")
    forged = f"{expiry}.{'A' * len(signature)}"
    assert auth.session_cookie_valid(forged) is False


def test_cookie_avec_expiration_repoussee_est_rejete(auth_on: None) -> None:
    """Repousser la date sans re-signer ne marche pas : la date EST signée."""
    cookie = auth.issue_session_cookie()
    _, _, signature = cookie.partition(".")
    far_future = str(int(time.time()) + 10_000_000)
    assert auth.session_cookie_valid(f"{far_future}.{signature}") is False


def test_cookie_expire_est_rejete(
    auth_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "session_max_age_days", -1)
    assert auth.session_cookie_valid(auth.issue_session_cookie()) is False


def test_rotation_du_jeton_invalide_les_sessions(
    auth_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le cookie est signé avec API_TOKEN : le changer déconnecte tout le monde."""
    cookie = auth.issue_session_cookie()
    monkeypatch.setattr(settings, "api_token", SecretStr("un-tout-autre-jeton"))
    assert auth.session_cookie_valid(cookie) is False


def test_cookie_vide_ou_malforme(auth_on: None) -> None:
    for value in ("", None, "pas-de-point", ".", "abc.def"):
        assert auth.session_cookie_valid(value) is False


def test_jeton_vide_refuse_meme_si_fourni_vide(
    auth_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auth activée sans jeton configuré : on ferme, on n'ouvre pas en grand."""
    monkeypatch.setattr(settings, "api_token", SecretStr(""))
    assert auth.token_valid("") is False
    assert auth.token_valid("n'importe quoi") is False


def test_origin_inconnue_refusee(auth_on: None) -> None:
    assert network.origin_allowed("https://evil.example") is False


def test_origin_absente_acceptee(auth_on: None) -> None:
    """Les clients non-navigateur (agent PC, curl) n'envoient pas d'Origin."""
    assert network.origin_allowed(None) is True


def test_origin_null_refusee(auth_on: None) -> None:
    """`Origin: null` = iframe sandbox ou fichier local — jamais légitime."""
    assert network.origin_allowed("null") is False


def test_origin_declaree_acceptee(auth_on: None) -> None:
    assert network.origin_allowed("http://testserver:8000") is True


def test_localhost_toujours_autorise(auth_on: None) -> None:
    assert network.origin_allowed("http://127.0.0.1:8000") is True


# ─────────────────────────────────────────────────────────────────────────────
# Intégration — surface HTTP réelle
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    from main import app

    return TestClient(app)


@pytest.mark.integration
def test_auth_desactivee_ne_casse_rien(client: TestClient, auth_off: None) -> None:
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/sessions").status_code != 401


@pytest.mark.integration
def test_health_et_login_restent_publics(client: TestClient, auth_on: None) -> None:
    assert client.get("/api/health").status_code == 200
    assert client.get("/login").status_code == 200


@pytest.mark.integration
def test_api_sans_identite_renvoie_401(client: TestClient, auth_on: None) -> None:
    assert client.get("/api/sessions").status_code == 401


@pytest.mark.integration
def test_api_avec_bearer_passe(client: TestClient, auth_on: None) -> None:
    r = client.get("/api/sessions", headers={"Authorization": f"Bearer {_TOKEN}"})
    assert r.status_code != 401


@pytest.mark.integration
def test_api_avec_cookie_de_session_passe(client: TestClient, auth_on: None) -> None:
    client.cookies.set(auth.SESSION_COOKIE, auth.issue_session_cookie())
    r = client.get("/api/sessions")
    assert r.status_code != 401


@pytest.mark.integration
def test_page_html_sans_identite_redirige_vers_login(
    client: TestClient, auth_on: None
) -> None:
    """RÉGRESSION : ces pages étaient servies en clair, jeton API inclus."""
    r = client.get(
        "/dashboard",
        headers={"Sec-Fetch-Mode": "navigate", "Accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 307
    assert r.headers["location"].startswith("/login")


@pytest.mark.integration
def test_login_correct_pose_le_cookie(client: TestClient, auth_on: None) -> None:
    r = client.post(
        "/api/login",
        data={"token": _TOKEN, "next": "/dashboard"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard"
    cookie = r.cookies.get(auth.SESSION_COOKIE)
    assert cookie and auth.session_cookie_valid(cookie)


@pytest.mark.integration
def test_login_incorrect_ne_pose_pas_de_cookie(
    client: TestClient, auth_on: None
) -> None:
    r = client.post(
        "/api/login", data={"token": "mauvais", "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 401
    assert r.cookies.get(auth.SESSION_COOKIE) is None


@pytest.mark.integration
def test_login_bloque_la_redirection_ouverte(client: TestClient, auth_on: None) -> None:
    """`next=https://evil.example` ne doit pas expédier l'utilisateur dehors."""
    r = client.post(
        "/api/login",
        data={"token": _TOKEN, "next": "https://evil.example/phish"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"


@pytest.mark.integration
def test_post_avec_origin_etrangere_refuse(client: TestClient, auth_on: None) -> None:
    """Défense CSRF : même muni du cookie, un POST venu d'ailleurs est bloqué."""
    client.cookies.set(auth.SESSION_COOKIE, auth.issue_session_cookie())
    r = client.post(
        "/api/tools/execute",
        json={"tool": "cli_runner", "args": {}},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403


@pytest.mark.integration
def test_execution_outil_bloquee_sans_identite(
    client: TestClient, auth_on: None
) -> None:
    r = client.post("/api/tools/execute", json={"tool": "cli_runner", "args": {}})
    assert r.status_code == 401


@pytest.mark.integration
def test_websocket_sans_identite_est_refuse(client: TestClient, auth_on: None) -> None:
    """RÉGRESSION : /ws était exempté d'auth alors qu'il porte chat + outils."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc:  # noqa: PT012 — le connect DOIT lever
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()
    assert exc.value.code == 1008


@pytest.mark.integration
def test_websocket_origine_etrangere_est_refuse(
    client: TestClient, auth_on: None
) -> None:
    """Détournement de WebSocket inter-site : le cookie ne doit pas suffire."""
    from starlette.websockets import WebSocketDisconnect

    client.cookies.set(auth.SESSION_COOKIE, auth.issue_session_cookie())
    with pytest.raises(WebSocketDisconnect) as exc:  # noqa: PT012
        with client.websocket_connect(
            "/ws", headers={"Origin": "https://evil.example"}
        ) as ws:
            ws.receive_text()
    assert exc.value.code == 1008
