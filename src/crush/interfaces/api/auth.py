"""Routes de connexion — échange du jeton contre un cookie de session.

Seule porte d'entrée non authentifiée de l'UI (cf. `engine/auth.py`). Le
formulaire prend le jeton `API_TOKEN` ; en retour, le navigateur reçoit un
cookie signé qui l'authentifiera ensuite partout, WebSocket compris.

Le rendu est volontairement autonome (CSS en ligne, aucun asset externe) :
cette page doit s'afficher même si les fichiers statiques sont inaccessibles,
puisque c'est elle qui débloque tout le reste.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from crush.engine.auth import (
    SESSION_COOKIE,
    issue_session_cookie,
    session_max_age_seconds,
    token_valid,
)
from crush.kernel.settings import settings

router = APIRouter()

# Ralentit la force brute sur le jeton. Le secret fait 32 octets, donc le
# risque réel est faible ; ce délai borne surtout le débit d'un script qui
# tenterait un dictionnaire depuis le tailnet.
_FAILED_ATTEMPT_DELAY = 1.0


def _safe_next(raw: str | None) -> str:
    """N'accepte qu'un chemin relatif interne — bloque l'open redirect.

    Sans ce filtre, `/login?next=https://evil.example` renverrait l'utilisateur
    fraîchement authentifié vers un site tiers.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    return raw


def _login_page(next_url: str, *, error: str | None = None) -> str:
    error_block = (
        f'<p class="err">{html_mod.escape(error)}</p>' if error else ""
    )
    assistant = html_mod.escape(settings.display_assistant_name)
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{assistant} — Connexion</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100dvh; display: grid; place-items: center;
    background: #0a0e16; color: #e8ecf4; padding: 24px;
    font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  }}
  form {{
    width: 100%; max-width: 22rem; display: flex; flex-direction: column; gap: 14px;
  }}
  h1 {{ margin: 0 0 4px; font-size: 1.4rem; letter-spacing: .02em; }}
  p.sub {{ margin: 0 0 12px; color: #8b98ad; font-size: .9rem; }}
  input {{
    width: 100%; padding: 14px; border-radius: 10px; font-size: 16px;
    border: 1px solid #26304a; background: #121826; color: inherit;
  }}
  input:focus {{ outline: 2px solid #4ca8e8; outline-offset: 1px; }}
  button {{
    padding: 14px; border: 0; border-radius: 10px; font-size: 16px;
    font-weight: 600; background: #4ca8e8; color: #04121f; cursor: pointer;
  }}
  button:active {{ transform: translateY(1px); }}
  p.err {{
    margin: 0; padding: 10px 12px; border-radius: 8px; font-size: .9rem;
    background: #3b1620; color: #ffb4b4; border: 1px solid #6d2233;
  }}
</style>
</head>
<body>
  <form method="post" action="/api/login">
    <h1>{assistant}</h1>
    <p class="sub">Saisis le jeton d'accès pour ouvrir une session sur cet appareil.</p>
    {error_block}
    <input type="hidden" name="next" value="{html_mod.escape(next_url)}">
    <input type="password" name="token" placeholder="Jeton d'accès"
           autocomplete="current-password" autofocus required>
    <button type="submit">Se connecter</button>
  </form>
</body>
</html>"""


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    """Formulaire de connexion. Court-circuité si une session est déjà valide."""
    next_url = _safe_next(request.query_params.get("next"))

    if not settings.api_auth_enabled:
        # Auth désactivée : la page n'a pas de sens, on renvoie à l'accueil.
        return RedirectResponse(url=next_url, status_code=303)

    return HTMLResponse(
        _login_page(next_url),
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/login")
async def login_submit(
    token: str = Form(...),
    next: str = Form("/"),  # noqa: A002 — nom imposé par le champ du formulaire
) -> Response:
    """Vérifie le jeton et pose le cookie de session."""
    next_url = _safe_next(next)

    if not token_valid(token):
        # Délai constant : ne renseigne pas sur la proximité du jeton fourni.
        await asyncio.sleep(_FAILED_ATTEMPT_DELAY)
        return HTMLResponse(
            _login_page(next_url, error="Jeton invalide."),
            status_code=401,
            headers={"Cache-Control": "no-store"},
        )

    response = RedirectResponse(url=next_url, status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=issue_session_cookie(),
        max_age=session_max_age_seconds(),
        httponly=True,  # illisible depuis JavaScript → immunise contre le vol par XSS
        samesite="strict",  # premier verrou CSRF (le second est le contrôle d'Origin)
        # `secure` seulement en HTTPS : en LAN pur (http://192.168.x.x), un
        # cookie Secure ne serait jamais envoyé et la connexion boucherait.
        secure=settings.environment == "production",
        path="/",
    )
    return response


@router.post("/api/logout")
async def logout() -> Response:
    """Efface le cookie de session de cet appareil."""
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(key=SESSION_COOKIE, path="/")
    return response


@router.get("/api/auth/status")
async def auth_status() -> dict:
    """État de l'authentification. Route protégée : un 200 prouve la session."""
    return {
        "authenticated": True,
        "auth_enabled": settings.api_auth_enabled,
    }


def generate_token() -> str:
    """Jeton d'accès neuf — utilisé par le setup et les scripts d'installation."""
    return secrets.token_hex(32)
