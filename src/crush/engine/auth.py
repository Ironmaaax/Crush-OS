"""Garde-fou réseau — authentification de l'API et des WebSockets.

MODÈLE
======

Trois porteurs d'identité, tous adossés au même secret (`API_TOKEN`) :

1. `Authorization: Bearer <token>` — clients programmatiques (agent PC,
   scripts, curl). Rien de nouveau.
2. Cookie de session signé — navigateurs. Posé par `POST /login`, envoyé
   automatiquement par le navigateur sur toute requête same-origin, **y
   compris le handshake WebSocket**. C'est ce qui permet d'authentifier
   `/ws` sans query string.
3. `?token=<token>` en query string — dernier recours pour un client qui ne
   peut ni poser d'en-tête ni porter de cookie.

POURQUOI CE CHANGEMENT
======================

L'implémentation précédente exemptait **toutes** les connexions WebSocket
(`scope["type"] == "websocket"` → retour immédiat) et **toutes** les pages
HTML de l'UI. Or `/ws` porte le chat complet et l'exécution d'outils, et
`interfaces/api/ui.py` injecte `window.CRUSH_API_TOKEN` en clair dans le
HTML de ces pages exemptées : quiconque atteignait le port récupérait le
jeton, puis pilotait l'assistant. Le modèle ne tenait qu'en 127.0.0.1.

Désormais :
  - les WebSockets sont authentifiés comme le reste ;
  - les pages HTML exigent le cookie, donc l'injection du jeton n'est plus
    lisible que par une session déjà authentifiée ;
  - `/login` est la SEULE route HTML non authentifiée.

DÉFENSE CSRF
============

Un cookie est envoyé automatiquement par le navigateur, y compris sur une
requête déclenchée par un autre site. Deux verrous :
  - `SameSite=Strict` sur le cookie — le navigateur ne l'envoie pas si la
    navigation vient d'une autre origine ;
  - contrôle d'`Origin` (cf. `kernel/network.py`) sur les WebSockets et les
    requêtes mutantes, car `SameSite` n'est pas appliqué uniformément aux
    handshakes WebSocket selon les navigateurs.
"""

from __future__ import annotations

import base64
import hmac
import time
from collections.abc import Sequence
from hashlib import sha256
from typing import NoReturn
from urllib.parse import quote

from fastapi import HTTPException
from loguru import logger
from starlette.exceptions import WebSocketException
from starlette.requests import HTTPConnection
from starlette.status import WS_1008_POLICY_VIOLATION

from crush.kernel.network import origin_allowed
from crush.kernel.settings import settings

SESSION_COOKIE = "crush_session"

# Routes joignables SANS authentification. Périmètre volontairement minimal :
# tout ajout ici est une porte ouverte sur le réseau.
_PUBLIC_EXACT: frozenset[str] = frozenset({
    "/health",
    "/api/health",
    "/login",
    "/api/login",
})
_PUBLIC_PREFIXES: Sequence[str] = (
    # Webhooks entrants : vérification de signature propre côté handler.
    "/api/channels/",
    # Callbacks OAuth : redirections d'un tiers, impossible d'y poser un
    # en-tête ou d'y compter sur un cookie.
    "/api/google/",
)
_PUBLIC_OAUTH_CALLBACKS: frozenset[str] = frozenset({
    "/api/spotify/auth",
    "/api/spotify/callback",
})

# Méthodes qui ne modifient rien : dispensées du contrôle d'Origin, sinon un
# simple lien entrant vers le dashboard casserait la navigation.
_SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})


# ── Cookie de session signé ──────────────────────────────────────────────────
#
# Format : "<expiry_epoch>.<hmac_sha256(expiry, api_token)>" en base64url.
# On ne stocke aucun identifiant : l'assistant est mono-utilisateur, le cookie
# atteste seulement « le porteur connaissait le jeton à la date X ». Signer
# avec `api_token` fait qu'une rotation du jeton invalide toutes les sessions.


def _sign(payload: str) -> str:
    key = settings.api_token.get_secret_value().encode("utf-8")
    digest = hmac.new(key, payload.encode("utf-8"), sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def issue_session_cookie() -> str:
    """Fabrique une valeur de cookie valable `session_max_age_days` jours."""
    expiry = int(time.time()) + settings.session_max_age_days * 86_400
    return f"{expiry}.{_sign(str(expiry))}"


def session_cookie_valid(value: str | None) -> bool:
    """Vérifie signature ET fraîcheur. Tout doute renvoie False."""
    if not value:
        return False
    expiry_raw, _, signature = value.partition(".")
    if not signature:
        return False
    if not hmac.compare_digest(signature, _sign(expiry_raw)):
        return False
    try:
        return int(expiry_raw) > time.time()
    except ValueError:
        return False


def session_max_age_seconds() -> int:
    return settings.session_max_age_days * 86_400


# ── Vérification du secret brut ──────────────────────────────────────────────


def token_valid(candidate: str | None) -> bool:
    """Compare un jeton fourni au jeton attendu, en temps constant."""
    if not candidate:
        return False
    expected = settings.api_token.get_secret_value()
    if not expected:
        # Auth demandée mais aucun jeton configuré : on refuse tout plutôt que
        # d'ouvrir en grand. Le démarrage logue déjà l'incohérence.
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def is_authenticated(request: HTTPConnection) -> bool:
    """True si la connexion porte une identité valide, quel qu'en soit le support."""
    auth_header: str = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer ") and token_valid(auth_header[len("Bearer ") :]):
        return True
    if session_cookie_valid(request.cookies.get(SESSION_COOKIE)):
        return True
    return token_valid(request.query_params.get("token"))


def _is_public(path: str) -> bool:
    if path in _PUBLIC_EXACT or path in _PUBLIC_OAUTH_CALLBACKS:
        return True
    return any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


# ── Dépendance FastAPI globale ───────────────────────────────────────────────


async def verify_api_token(request: HTTPConnection) -> None:
    """Exige une identité valide sur toute route non publique.

    No-op si `api_auth_enabled=False` — l'usage purement local reste inchangé.

    Hors périmètre par construction : les assets statiques, montés comme
    sous-application ASGI (`StaticFiles`), ne passent pas par les dépendances
    du routeur. Ils ne contiennent ni secret ni donnée personnelle.
    """
    if not settings.api_auth_enabled:
        return

    path: str = request.url.path
    if _is_public(path):
        return

    origin = request.headers.get("Origin")
    is_websocket = request.scope.get("type") == "websocket"
    method: str = request.scope.get("method", "GET")

    # Contrôle d'Origin : sur les WebSockets (toujours) et sur les requêtes
    # mutantes. Bloque le détournement inter-site quand l'identité vient du
    # cookie, que le navigateur joint tout seul.
    if (is_websocket or method not in _SAFE_METHODS) and not origin_allowed(origin):
        logger.warning(
            "Auth: Origin refusée",
            path=path,
            origin=origin,
            client=request.client.host if request.client else "?",
        )
        _reject(is_websocket, 403, "Origine non autorisée.")

    if is_authenticated(request):
        return

    logger.warning(
        "Auth: identité absente ou invalide",
        path=path,
        websocket=is_websocket,
        client=request.client.host if request.client else "?",
    )

    # Navigation vers une page : on renvoie vers /login plutôt qu'un 401 JSON
    # que le navigateur afficherait tel quel. `next` permet de revenir où
    # l'utilisateur voulait aller après connexion.
    if _wants_html(request) and not is_websocket:
        target = quote(request.url.path, safe="/")
        raise HTTPException(
            status_code=307,
            detail="Redirection vers /login.",
            headers={"Location": f"/login?next={target}"},
        )

    _reject(is_websocket, 401, "Authentification requise.")


def _reject(is_websocket: bool, status: int, detail: str) -> NoReturn:
    """Refuse la connexion avec le type d'exception que la couche ASGI sait traiter.

    Sur un scope WebSocket, `HTTPException` est ingérable : le gestionnaire
    Starlette correspondant fabrique une `PlainTextResponse`, qu'on ne peut pas
    émettre sur une connexion WebSocket. Il faut `WebSocketException`, qui se
    traduit par une fermeture propre avec un code de police.
    """
    if is_websocket:
        raise WebSocketException(code=WS_1008_POLICY_VIOLATION, reason=detail)
    raise HTTPException(status_code=status, detail=detail)


def _wants_html(request: HTTPConnection) -> bool:
    """Distingue une navigation de page d'un appel `fetch` de l'UI.

    `Sec-Fetch-Mode: navigate` est la marque fiable (posée par le navigateur,
    non falsifiable par script) ; on retombe sur `Accept` pour les navigateurs
    qui ne l'envoient pas.
    """
    if request.headers.get("Sec-Fetch-Mode") == "navigate":
        return True
    accept = request.headers.get("Accept", "")
    return "text/html" in accept and "application/json" not in accept
