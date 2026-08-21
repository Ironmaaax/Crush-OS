# Copyright (C) 2026 Max Ea
# This file is part of CRUSH-OS,   .


"""Queue pub/sub pour les détections d'objets YOLO → WebSocket browser."""

from __future__ import annotations

import asyncio


class VisionObjectsQueue:
    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[list[dict]]] = []

    def publish(self, objects: list[dict]) -> None:
        for q in self._subscribers:
            try:
                q.put_nowait(objects)
            except asyncio.QueueFull:
                pass  # le browser est trop lent, on lâche la frame

    def subscribe(self) -> asyncio.Queue[list[dict]]:
        q: asyncio.Queue[list[dict]] = asyncio.Queue(maxsize=2)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[list[dict]]) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass


_queue = VisionObjectsQueue()


def get_vision_objects_queue() -> VisionObjectsQueue:
    return _queue
