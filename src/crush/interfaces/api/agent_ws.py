"""Canal des agents distants — le poste à piloter se connecte ici.

Voir `kernel/remote_agents.py` pour le sens de la connexion et la place dans
les couches. Ce module ne fait que la plomberie WebSocket : accueil, annonce
de capacités, expédition des actions, appariement des réponses.

PROTOCOLE
=========

    poste → serveur   {"type":"hello","name":"max-pc","platform":"windows",
                       "actions":["volume_set","app_launch"],"version":"1.0"}
    serveur → poste   {"type":"action","id":"a1","action":"volume_set",
                       "params":{"level":0.4}}
    poste → serveur   {"type":"result","id":"a1","ok":true,"detail":"Volume à 40 %"}

L'appariement se fait par `id` : plusieurs actions peuvent être en vol, et
rien ne garantit que les réponses reviennent dans l'ordre d'émission.

AUTHENTIFICATION
================

L'agent n'est pas un navigateur : il ne porte pas de cookie de session. Il
s'authentifie au jeton, passé en paramètre d'URL — un client WebSocket ne peut
pas poser d'en-tête au handshake. La dépendance globale de `engine/auth.py`
accepte déjà cette forme.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from crush.kernel.remote_agents import RemoteAgent, registry

router = APIRouter()

# Au-delà, on considère le poste injoignable. Assez large pour lancer une
# application lourde, assez court pour ne pas figer une conversation.
_ACTION_TIMEOUT = 30.0

# Actions en attente de réponse, par identifiant.
_en_vol: dict[str, asyncio.Future[dict[str, Any]]] = {}
# Sockets des postes connectés, par nom de machine.
_sockets: dict[str, WebSocket] = {}


async def _dispatch(machine: str, action: str, params: dict[str, Any]) -> dict[str, Any]:
    """Envoie une action au poste et attend sa réponse."""
    socket = _sockets.get(machine)
    if socket is None:
        return {"ok": False, "error": f"{machine} n'est plus connecté."}

    action_id = uuid.uuid4().hex[:12]
    attente: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    _en_vol[action_id] = attente

    try:
        await socket.send_json(
            {"type": "action", "id": action_id, "action": action, "params": params}
        )
        return await asyncio.wait_for(attente, timeout=_ACTION_TIMEOUT)
    except TimeoutError:
        return {"ok": False, "error": f"{machine} n'a pas répondu en {_ACTION_TIMEOUT:.0f}s."}
    except (WebSocketDisconnect, RuntimeError):
        return {"ok": False, "error": f"Lien perdu avec {machine}."}
    finally:
        _en_vol.pop(action_id, None)


@router.websocket("/ws/agent")
async def websocket_agent(websocket: WebSocket) -> None:
    """Accueille un agent distant pour la durée de sa connexion."""
    await websocket.accept()
    nom: str | None = None

    try:
        # ── Présentation ────────────────────────────────────────────────────
        presentation = await asyncio.wait_for(websocket.receive_json(), timeout=15)
        if presentation.get("type") != "hello" or not presentation.get("name"):
            await websocket.send_json({"type": "error", "error": "Présentation attendue."})
            await websocket.close()
            return

        nom = str(presentation["name"])
        agent = RemoteAgent(
            name=nom,
            platform=str(presentation.get("platform", "inconnu")),
            actions=[str(a) for a in presentation.get("actions", [])],
            version=str(presentation.get("version", "")),
        )
        _sockets[nom] = websocket
        registry.add(agent)
        registry.set_dispatcher(_dispatch)
        await websocket.send_json({"type": "welcome", "name": nom})

        # ── Réponses aux actions ────────────────────────────────────────────
        while True:
            message = await websocket.receive_json()
            if message.get("type") != "result":
                continue
            attente = _en_vol.get(str(message.get("id", "")))
            # `done()` : une action expirée a déjà vu son future résolu par le
            # timeout ; réécrire dessus lèverait InvalidStateError.
            if attente is not None and not attente.done():
                attente.set_result({
                    "ok": bool(message.get("ok")),
                    "detail": message.get("detail", ""),
                    "error": message.get("error", ""),
                })

    except (WebSocketDisconnect, TimeoutError):
        pass
    except Exception as exc:  # noqa: BLE001 — un agent bogué ne doit pas tuer le serveur
        logger.warning("Canal agent distant interrompu : {}", exc)
    finally:
        if nom:
            _sockets.pop(nom, None)
            registry.remove(nom)
        if not _sockets:
            registry.set_dispatcher(None)
