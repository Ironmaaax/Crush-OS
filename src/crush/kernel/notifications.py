# Copyright (C) 2026 Max Ea
# This file is part of CRUSH-OS,   .
 

"""Singleton accessor pour la file de notifications proactives — Phase F.

Pattern identique à `kernel.approval` (et `kernel.permissions`) : le
singleton vit en kernel pour que `capabilities/tools/{preset,...}` et
`capabilities/skills/executor.py` puissent broadcaster sans importer
`crush.engine.background.notifications` (RÈGLE 2).

La queue concrète (`ProactiveQueue`) vit toujours en
`crush.engine.background.notifications` — le bootstrap appelle
`set_proactive_queue(...)` après construction. Le typage croise les
couches via le Protocol structural `NotificationSink` (kernel.contracts).
"""

from __future__ import annotations

from crush.kernel.contracts import NotificationSink

_proactive_queue: NotificationSink | None = None


def set_proactive_queue(q: NotificationSink) -> None:
    """Injecté par bootstrap.build() après construction du container."""
    global _proactive_queue
    _proactive_queue = q


async def broadcast_event(event: dict) -> None:
    """Push un évènement structuré vers les abonnés WebSocket (UI dashboard).

    No-op si aucune queue n'est câblée (process de test, démarrage à froid).
    """
    if _proactive_queue is not None:
        _proactive_queue.broadcast_event(event)


async def broadcast_audio(audio_bytes: bytes) -> None:
    """Push un blob audio (base64) vers les abonnés WebSocket (TTS streamé)."""
    if _proactive_queue is not None and audio_bytes:
        import base64

        _proactive_queue.broadcast_event(
            {
                "type": "audio",
                "data": base64.b64encode(audio_bytes).decode(),
            }
        )
