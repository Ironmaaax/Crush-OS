"""Pipeline vocal — un aller-retour audio sur le WebSocket authentifié.

    navigateur (micro)  ──audio──▶  transcription  ──▶  Gateway  ──▶  TTS
            ▲                                                          │
            └──────────────────── audio de la réponse ─────────────────┘

Le micro est celui du client (téléphone, PC) : la machine hôte n'a ni écran ni
carte son. Aucun WebRTC, aucun serveur de signalisation — la connexion est le
même WebSocket que le chat, donc elle hérite du cookie de session et du
contrôle d'Origin de `engine/auth.py`.

PROTOCOLE
=========

Client → serveur, un message JSON par prise de parole :

    {"audio": "<base64>", "mime": "audio/webm;codecs=opus",
     "session_id": "uuid|null", "want_audio": true}

Serveur → client, dans l'ordre :

    {"type": "transcript", "text": "..."}     ce qui a été compris
    {"type": "chunk",      "content": "..."}  la réponse, en flux
    {"type": "audio",      "data": "<base64>", "mime": "audio/wav"}
    {"type": "done"}
    {"type": "error",      "content": "..."}

`transcript` part AVANT la réponse pour que l'interface affiche tout de suite
ce qu'elle a entendu : c'est ce qui rend une erreur de transcription lisible,
au lieu d'une réponse incohérente sans explication.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from crush.engine.background.worker import BackgroundTask, BackgroundWorker
from crush.engine.gateway import Gateway
from crush.engine.router import RouteEnum
from crush.engine.session import Session
from crush.providers.audio.segmentation import SentenceAccumulator
from crush.providers.audio.stt import SpeechToText, TranscriptionUnavailable
from crush.providers.audio.tts import tts_engine

router = APIRouter()

# Garde-fou mémoire : une prise de parole normale pèse quelques dizaines de Ko
# en Opus. Au-delà, on refuse plutôt que de charger le Pi — un client bogué
# pourrait sinon pousser des centaines de Mo dans la RAM du process.
_MAX_AUDIO_BYTES = 8 * 1024 * 1024


@router.websocket("/ws/voice")
async def websocket_voice(websocket: WebSocket) -> None:
    """Boucle vocale. L'authentification est déjà faite par la dépendance globale."""
    await websocket.accept()
    logger.info("WebSocket vocal ouvert")

    gateway: Gateway = websocket.app.state.voice_gateway
    stt: SpeechToText = websocket.app.state.stt

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                await _error(websocket, "Message illisible (JSON invalide).")
                continue

            # Le client peut parler OU écrire : l'interface mobile expose les
            # deux sur la même connexion, pour que la réponse vocale et le fil
            # de conversation partagent la même session.
            texte_direct = payload.get("text")
            if isinstance(texte_direct, str) and texte_direct.strip():
                transcript = texte_direct.strip()
            else:
                audio = _decode_audio(payload.get("audio"))
                if audio is None:
                    await _error(websocket, "Ni texte ni audio exploitable.")
                    continue
                if len(audio) > _MAX_AUDIO_BYTES:
                    await _error(websocket, "Enregistrement trop long.")
                    continue

                mime = str(payload.get("mime") or "audio/webm")

                # ── 1. Transcription ────────────────────────────────────────
                try:
                    transcript = await stt.transcribe(audio, mime)
                except TranscriptionUnavailable as exc:
                    logger.warning("Transcription indisponible : {}", exc)
                    await _error(websocket, f"Transcription indisponible — {exc}")
                    continue

                if not transcript:
                    # Silence ou bruit : on le dit, sinon l'utilisateur croit
                    # à un gel de l'interface.
                    await websocket.send_json({"type": "transcript", "text": ""})
                    await _error(websocket, "Je n'ai rien entendu.")
                    continue

                await websocket.send_json({"type": "transcript", "text": transcript})
                logger.info("Vocal — transcription : {}", transcript)

            # ── 2. Réponse, par le même Gateway que le chat écrit ───────────
            session, route, stream = await gateway.handle(
                message=transcript,
                session_id=payload.get("session_id"),
                stream=True,
            )
            await websocket.send_json({"type": "session", "session_id": str(session.id)})

            # ── 3. Réponse et synthèse, EN RECOUVREMENT ─────────────────────
            #
            # La synthèse ne peut pas attendre la fin de la génération : ce
            # serait le temps du LLM PLUS celui du TTS. On découpe le flux en
            # phrases et on synthétise chacune dès qu'elle est complète, si
            # bien que le premier son part pendant que le LLM écrit encore la
            # suite. Mesuré sur Pi 5 : 0,25 s avant le premier son au lieu de
            # 1,72 s pour une réponse de quatre phrases.
            veut_audio = bool(payload.get("want_audio", True))
            file_tts: asyncio.Queue[str | None] = asyncio.Queue()
            tache_tts = (
                asyncio.create_task(_parler(websocket, file_tts))
                if veut_audio
                else None
            )

            accumulateur = SentenceAccumulator()
            reply = ""
            try:
                if isinstance(stream, str):
                    reply = stream
                    await websocket.send_json({"type": "chunk", "content": reply})
                    for fragment in accumulateur.push(reply):
                        file_tts.put_nowait(fragment)
                else:
                    async for chunk in stream:
                        reply += chunk
                        await websocket.send_json({"type": "chunk", "content": chunk})
                        for fragment in accumulateur.push(chunk):
                            file_tts.put_nowait(fragment)
                for fragment in accumulateur.flush():
                    file_tts.put_nowait(fragment)
            finally:
                # Le sentinelle clôt la file même si le flux a levé : sans lui,
                # la tâche de synthèse attendrait indéfiniment.
                file_tts.put_nowait(None)

            session.add_message("assistant", reply)

            # Apprentissage — le chemin vocal en était privé : `user_model.fire`
            # et `auto_dream` n'étaient déclenchés que par le WebSocket de chat
            # texte. Parler à l'assistant ne lui apprenait donc rien, alors que
            # c'est le mode d'usage principal depuis le téléphone.
            # Fire-and-forget : ces mises à jour ne doivent jamais retarder la
            # réponse suivante.
            if reply.strip():
                _apprendre(websocket, transcript, reply)

            if tache_tts is not None:
                await tache_tts

            await websocket.send_json({"type": "done"})

            # ── 4. Travail de fond, APRÈS la réponse parlée ─────────────────
            _lancer_travail_de_fond(websocket, route, session, transcript)

    except WebSocketDisconnect:
        logger.info("WebSocket vocal fermé")
    except Exception as exc:  # noqa: BLE001 — on ne laisse pas mourir la socket en silence
        logger.opt(exception=True).error("Erreur du WebSocket vocal : {}", exc)
        await _error(websocket, "Erreur interne du pipeline vocal.")


def _lancer_travail_de_fond(
    websocket: WebSocket,
    route: RouteEnum,
    session: Session,
    demande: str,
) -> None:
    """Soumet la tâche de fond ou la mission décidée par le routage.

    La voix ignorait complètement la route rendue par le Gateway : `[BG]` ne
    soumettait rien et `[BG:PROJECT]` ne lançait aucune mission. L'assistant
    répondait « c'est parti » à l'oral, et il ne partait rien — le canal texte,
    lui, câblait bien les deux. C'est ce qui explique un `workspace/projects`
    resté vide : le téléphone est le mode d'usage principal.

    Appelé APRÈS l'envoi de « done », comme côté texte : le travail de fond ne
    doit jamais retarder la fin de la réponse parlée.
    """
    etat = websocket.app.state

    if route is RouteEnum.BACKGROUND:
        worker: BackgroundWorker | None = getattr(etat, "worker", None)
        if worker is None:
            logger.warning("Vocal — [BG] demandé mais aucun worker disponible")
            return
        worker.submit(BackgroundTask(session_id=str(session.id), instruction=demande))
        logger.info("Vocal — tâche de fond soumise")
        return

    if route is not RouteEnum.PROJECT:
        return

    orchestrator = getattr(etat, "orchestrator", None)
    if orchestrator is None:
        logger.warning("Vocal — [BG:PROJECT] demandé mais aucun orchestrateur disponible")
        return

    async def _mener_mission(msg: str = demande, orch: object = orchestrator) -> None:
        try:
            await orch.create_and_run(msg)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — la conversation continue sans
            logger.opt(exception=True).error("Vocal — mission échouée : {}", exc)
            notifications = getattr(etat, "notifications", None)
            if notifications is not None:
                notifications.add(f"La mission n'a pas pu démarrer : {exc}")

    asyncio.create_task(_mener_mission(), name=f"mission-voix-{str(session.id)[:8]}")
    logger.info("Vocal — mission lancée : {}", demande[:70])


def _apprendre(websocket: WebSocket, question: str, reponse: str) -> None:
    """Alimente le modèle utilisateur et la mémoire depuis un échange vocal.

    Sans blocage ni propagation d'erreur : un défaut d'apprentissage ne doit
    pas casser la conversation en cours.
    """
    etat = websocket.app.state

    user_model = getattr(etat, "user_model", None)
    if user_model is not None:
        try:
            user_model.fire(user_message=question, assistant_message=reponse)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Mise à jour du modèle utilisateur échouée : {}", exc)

    auto_dream = getattr(etat, "auto_dream", None)
    if auto_dream is not None:
        asyncio.create_task(
            auto_dream._run_micro_safe(user_message=question, assistant_message=reponse),
            name="autodream-micro-voix",
        )


async def _parler(websocket: WebSocket, file_tts: asyncio.Queue[str | None]) -> None:
    """Synthétise les fragments dans l'ordre et les émet au fil de l'eau.

    Tourne en parallèle de la génération : pendant que le LLM écrit la phrase
    suivante, celle-ci est déjà en train d'être synthétisée. Sur le Pi, Piper
    tient ×7,2 le temps réel une fois chargé, donc la synthèse rattrape
    toujours la lecture — le client ne subit jamais de trou.

    L'ordre est garanti par la file : un seul consommateur, séquentiel.
    """
    index = 0
    while True:
        fragment = await file_tts.get()
        if fragment is None:
            return
        try:
            audio, mime = await tts_engine.synthesize_with_mime(fragment)
        except Exception as exc:  # noqa: BLE001 — le texte est déjà parti
            # Un échec de synthèse ne doit pas perdre la réponse : le client
            # l'a reçue en texte et continue de l'afficher.
            logger.warning("Synthèse échouée sur « {} » : {}", fragment[:40], exc)
            continue
        if not audio:
            continue
        await websocket.send_json({
            "type": "audio",
            "seq": index,
            "text": fragment,
            "data": base64.b64encode(audio).decode("ascii"),
            "mime": mime,
        })
        index += 1


def _decode_audio(value: object) -> bytes | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None


async def _error(websocket: WebSocket, message: str) -> None:
    await websocket.send_json({"type": "error", "content": message})
