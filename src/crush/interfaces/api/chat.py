# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .


from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from crush.engine.background.notifications import broadcast_event
from crush.engine.background.worker import BackgroundTask
from crush.engine.router import RouteEnum
from crush.providers.audio.tts import tts_engine

router = APIRouter()


# ── Tools API ─────────────────────────────────────────────────────────────────


@router.get("/api/tools")
async def list_tools_endpoint(request: Request) -> list[dict]:
    registry = getattr(request.app.state, "tool_registry", None)
    if not registry:
        return []
    return [
        {"name": s.get("name", ""), "description": s.get("description", "")}
        for s in registry.core_schemas()
    ]


class ToolExecuteRequest(BaseModel):
    tool: str
    params: dict = {}


@router.post("/api/tools/execute")
async def execute_tool(body: ToolExecuteRequest, request: Request) -> dict:
    """Bridge générique — un client programmatique exécute un outil Crush par HTTP.

    Servait au process vocal LiveKit, supprimé ; reste utilisé par les clients
    hors-process (agent de poste distant).
    """
    registry = request.app.state.tool_registry
    result = await registry.call(body.tool, body.params)
    return {
        "success": not result.is_error,
        "result": result.content,
    }


# ── Voice API ─────────────────────────────────────────────────────────────────


@router.post("/api/voice/speak")
async def voice_speak(body: dict) -> dict:
    """
    Synthétise un texte en audio via TTS et retourne les bytes en base64.
    Le frontend joue l'audio directement avec Web Audio API.
    """
    import base64

    text = body.get("text", "").strip()
    if not text:
        return {"status": "error", "audio_b64": None}

    audio_bytes = await tts_engine.synthesize(text)
    return {
        "status": "ok",
        "audio_b64": base64.b64encode(audio_bytes).decode() if audio_bytes else None,
    }


class VoiceGenerateRequest(BaseModel):
    message: str
    session_id: str | None = None


@router.post("/api/voice/generate")
async def voice_generate(body: VoiceGenerateRequest, request: Request) -> StreamingResponse:
    """Bridge voix → gateway Crush.
    Même pipeline que le chat texte (Claude + outils + mémoire).
    Partage la session si session_id fourni.
    """
    import asyncio

    gateway = request.app.state.voice_gateway
    worker = request.app.state.worker
    orchestrator = getattr(request.app.state, "orchestrator", None)
    consolidation = request.app.state.consolidation
    auto_dream = request.app.state.auto_dream

    voice_msg = f"{body.message}\n[voix]"

    session, route, response = await gateway.handle(
        message=voice_msg,
        session_id=body.session_id,
        stream=True,
    )

    message_original = body.message

    async def _stream() -> AsyncGenerator[str, None]:
        full = ""
        try:
            if isinstance(response, str):
                full = response
                yield response
            else:
                async for chunk in response:
                    full += chunk
                    yield chunk
        except Exception as e:
            from loguru import logger as _log

            from crush.engine.llm_errors import friendly_llm_error

            _log.error("Voice generate stream error", error=str(e))
            full = friendly_llm_error(e)
            yield full

        session.add_message("assistant", full)

        if route is RouteEnum.BACKGROUND:
            worker.submit(BackgroundTask(session_id=str(session.id), instruction=message_original))
        elif route is RouteEnum.PROJECT and orchestrator:
            asyncio.create_task(
                orchestrator.create_and_run(message_original),
                name=f"voice-project-{str(session.id)[:8]}",
            )

        asyncio.create_task(
            consolidation._run_safe(user_message=message_original, assistant_message=full),
            name="voice-consolidation",
        )
        asyncio.create_task(
            auto_dream._run_micro_safe(user_message=message_original, assistant_message=full),
            name="voice-autodream",
        )

    return StreamingResponse(
        _stream(),
        media_type="text/plain",
        headers={"X-Session-Id": str(session.id)},
    )


# ── Internal broadcast ────────────────────────────────────────────────────────


@router.post("/internal/broadcast", include_in_schema=False)
async def internal_broadcast(request: Request) -> dict:
    """Endpoint interne utilisé par le voice agent pour envoyer des événements UI."""

    event = await request.json()
    await broadcast_event(event)
    return {"ok": True}


# ── Internal : proxy d'exécution des tools mémoire (process voix) ──────────────
# Un client hors-process appelle ces tools par HTTP plutôt que d'instancier son
# PROPRE modèle d'embeddings (~470 MB) : l'API a déjà le modèle chargé. On évite
# ainsi de doubler la RAM (et le chargement lent au 1er appel) côté voix.
# Restreint aux tools mémoire — pas d'exécution d'outils arbitraire via HTTP.
_PROXYABLE_MEMORY_TOOLS = frozenset(
    {"memory_search", "session_recall", "memory_write", "memory_load_topic"}
)


@router.post("/internal/memory_tool", include_in_schema=False)
async def internal_memory_tool(request: Request) -> dict:
    """Exécute un tool mémoire côté API pour le process voix. Retourne {content, is_error}."""
    body = await request.json()
    name = str(body.get("name", ""))
    args = body.get("args") or {}
    if name not in _PROXYABLE_MEMORY_TOOLS:
        return {"content": f"[ERREUR] tool '{name}' non proxifiable", "is_error": True}
    result = await request.app.state.tool_registry.call(name, args)
    return {"content": result.content, "is_error": result.is_error}
