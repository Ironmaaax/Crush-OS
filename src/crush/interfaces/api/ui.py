# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .


from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel

from crush.kernel.settings import settings

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    version: str


def inject_client_config(html: str) -> str:
    token = settings.api_token.get_secret_value() if settings.api_auth_enabled else ""
    api_base = ""
    snippet = (
        "<script>"
        f"window.CRUSH_API_TOKEN={json.dumps(token)};"
        f"window.CRUSH_API_BASE={json.dumps(api_base)};"
        f"window.CRUSH_WAKEUP_ENABLED={json.dumps(bool(settings.wakeup_enabled))};"
        f"window.CRUSH_ASSISTANT_NAME={json.dumps(settings.display_assistant_name)};"
        "</script>"
    )
    marker = "</head>"
    if marker in html:
        return html.replace(marker, snippet + marker, 1)
    return snippet + html


def _ui_html_response(html_path: Path, assets: list[tuple[str, str]] | None = None) -> Response:
    if assets:
        content = _versioned_html(html_path, assets)
    else:
        content = html_path.read_text(encoding="utf-8")
    return Response(
        content=inject_client_config(content),
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


def _versioned_html(html_path: Path, assets: list[tuple[str, str]]) -> str:
    """Injecte ?v=<mtime> dans les refs CSS/JS pour forcer le cache-busting."""
    content = html_path.read_text(encoding="utf-8")
    for src_attr, asset_path in assets:
        try:
            v = int(Path(asset_path).stat().st_mtime)
            content = re.sub(
                r'((?:href|src)=["\'])(' + re.escape(src_attr) + r')(["\'])',
                lambda m, _v=v: m.group(1) + m.group(2) + "?v=" + str(_v) + m.group(3),
                content,
            )
        except OSError:
            pass
    return content


@router.get("/command", include_in_schema=False)
async def command_center_ui() -> Response:
    return _ui_html_response(Path("src/crush/interfaces/ui/static/command.html"))


@router.get("/dashboard", include_in_schema=False)
async def dashboard_ui() -> Response:
    return _ui_html_response(
        Path("src/crush/interfaces/ui/static/dashboard.html"),
        [
            ("/_shared.css", "src/crush/interfaces/ui/static/_shared.css"),
            ("/dashboard.css", "src/crush/interfaces/ui/static/dashboard.css"),
            ("/_shared.js", "src/crush/interfaces/ui/static/_shared.js"),
            ("/dashboard.js", "src/crush/interfaces/ui/static/dashboard.js"),
        ],
    )


@router.get("/settings", include_in_schema=False)
async def settings_ui() -> Response:
    return _ui_html_response(
        Path("src/crush/interfaces/ui/static/settings.html"),
        [
            ("/_shared.css", "src/crush/interfaces/ui/static/_shared.css"),
            ("/settings.css", "src/crush/interfaces/ui/static/settings.css"),
            ("/_shared.js", "src/crush/interfaces/ui/static/_shared.js"),
            ("/settings-charts.js", "src/crush/interfaces/ui/static/settings-charts.js"),
            ("/settings.js", "src/crush/interfaces/ui/static/settings.js"),
        ],
    )


@router.get("/", include_in_schema=False)
async def home_ui() -> Response:
    return _ui_html_response(
        Path("src/crush/interfaces/ui/static/home.html"),
        [
            ("/_shared.css", "src/crush/interfaces/ui/static/_shared.css"),
            ("/home.css", "src/crush/interfaces/ui/static/home.css"),
            ("/_shared.js", "src/crush/interfaces/ui/static/_shared.js"),
            ("/three.min.js", "src/crush/interfaces/ui/static/three.min.js"),
            ("/orb.js", "src/crush/interfaces/ui/static/orb.js"),
            ("/home.js", "src/crush/interfaces/ui/static/home.js"),
        ],
    )


@router.get("/graphe", include_in_schema=False)
async def graphe_ui() -> Response:
    """La vue Cerveau — le graphe de ce dont l'assistant est fait.

    Sert `three.min.js` : la simulation et le rendu en dépendent, et c'est la
    copie locale, pas un CDN.
    """
    return _ui_html_response(
        Path("src/crush/interfaces/ui/static/graphe.html"),
        [
            ("/_shared.css", "src/crush/interfaces/ui/static/_shared.css"),
            ("/graphe.css", "src/crush/interfaces/ui/static/graphe.css"),
            ("/three.min.js", "src/crush/interfaces/ui/static/three.min.js"),
            ("/_shared.js", "src/crush/interfaces/ui/static/_shared.js"),
            ("/apercu.js", "src/crush/interfaces/ui/static/apercu.js"),
            ("/graphe.js", "src/crush/interfaces/ui/static/graphe.js"),
        ],
    )


@router.get("/capabilities", include_in_schema=False)
async def capabilities_ui() -> Response:
    return _ui_html_response(
        Path("src/crush/interfaces/ui/static/capabilities.html"),
        [
            ("/_shared.css", "src/crush/interfaces/ui/static/_shared.css"),
            ("/capabilities.css", "src/crush/interfaces/ui/static/capabilities.css"),
            ("/_shared.js", "src/crush/interfaces/ui/static/_shared.js"),
            ("/capabilities.js", "src/crush/interfaces/ui/static/capabilities.js"),
        ],
    )


@router.get("/mobile", include_in_schema=False)
async def mobile_ui() -> Response:
    """Interface téléphone — micro du navigateur, voix et texte.

    Servie par une route FastAPI et non par le mount `StaticFiles` : ce
    dernier est une sous-application ASGI, donc hors des dépendances du
    routeur, et la page échapperait à l'authentification. Ses ressources
    (CSS, JS, icônes, manifeste) restent servies par le mount — elles ne
    contiennent aucun secret.
    """
    return _ui_html_response(
        Path("src/crush/interfaces/ui/static/mobile/index.html"),
        [
            ("/mobile/style.css", "src/crush/interfaces/ui/static/mobile/style.css"),
            ("/mobile/app.js", "src/crush/interfaces/ui/static/mobile/app.js"),
            # Les trois scripts de l'orbe sont versionnés eux aussi : un
            # `orb.js` resté en cache sur le téléphone, sans le correctif
            # `resize()`, suffit à faire disparaître l'orbe après une fraction
            # de seconde. Le `?v=<mtime>` rend la copie périmée inatteignable.
            ("/three.min.js", "src/crush/interfaces/ui/static/three.min.js"),
            ("/config/sphereStyle.js", "src/crush/interfaces/ui/static/config/sphereStyle.js"),
            ("/orb.js", "src/crush/interfaces/ui/static/orb.js"),
        ],
    )


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Point de contrôle — vérifie que le serveur est up."""
    return HealthResponse(status="ok", version="0.1.0")
