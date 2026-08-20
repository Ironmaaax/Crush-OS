# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .
 

from __future__ import annotations

import asyncio
import io
import os
import wave
from datetime import datetime
from pathlib import Path

import httpx
from loguru import logger
from piper import PiperVoice

from crush.kernel.contracts import UsageTracker
from crush.kernel.schemas import UsageEntry, calculate_cost
from crush.kernel.settings import settings


class TTSEngine:
    """Moteur TTS avec routing ElevenLabs / Piper selon TTS_PROVIDER.

    Phase C — étape 2 (d) : `tracker` reçu par injection (constructeur ou
    `set_tracker`). Aucun import depuis `crush.engine.*` (CYCLE 1 bouclé).
    """

    def __init__(self, tracker: UsageTracker | None = None) -> None:
        self._piper_voice: object = None
        self._tracker = tracker

    def set_tracker(self, tracker: UsageTracker) -> None:
        """Injection post-construction (le singleton module-level est créé
        avant que le Container n'existe ; bootstrap.build() pousse le tracker
        ici juste après instanciation)."""
        self._tracker = tracker

    async def synthesize(self, text: str) -> bytes:
        """Synthétise un texte → octets audio. Le format dépend du fournisseur.

        Conservée pour les appelants qui n'ont pas besoin du type MIME (skills,
        endpoint /api/voice/speak). Le pipeline vocal utilise
        `synthesize_with_mime`, car il renvoie l'audio au navigateur, qui a
        besoin de savoir ce qu'il reçoit.
        """
        audio, _ = await self.synthesize_with_mime(text)
        return audio

    async def synthesize_with_mime(self, text: str) -> tuple[bytes, str]:
        """Synthétise et rend (octets, type MIME).

        Cascade `edge` → `piper`, reprise du projet précédent : Edge donne des
        voix nettement plus naturelles, gratuitement et sans clé, mais c'est un
        service EN LIGNE. Piper tourne sur la machine, donc insensible au réseau
        comme aux quotas : c'est le dernier rempart qui garantit que l'assistant
        garde une voix quoi qu'il arrive.
        """
        if not text.strip():
            return b"", "audio/wav"

        provider = settings.tts_provider
        if provider == "elevenlabs":
            return await self._synthesize_elevenlabs(text), "audio/mpeg"
        if provider in ("gemini", "google"):
            return await self._synthesize_gemini(text), "audio/wav"
        if provider == "edge":
            try:
                return await self._synthesize_edge(text), "audio/mpeg"
            except Exception as exc:  # noqa: BLE001 — le repli local doit toujours jouer
                logger.warning("Edge TTS indisponible ({}) — repli sur Piper.", exc)
                return await self._synthesize_piper(text), "audio/wav"
        return await self._synthesize_piper(text), "audio/wav"

    async def _synthesize_edge(self, text: str) -> bytes:
        """Microsoft Edge TTS — voix neuronales, gratuit, sans clé ni quota.

        Le service renvoie du MP3 par morceaux ; on les concatène en mémoire
        plutôt que de passer par un fichier temporaire, que le pipeline devrait
        relire puis effacer à chaque phrase.
        """
        import edge_tts

        voix = settings.edge_voice
        communicate = edge_tts.Communicate(text, voice=voix)
        morceaux = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                morceaux.extend(chunk["data"])
        if not morceaux:
            raise RuntimeError("Edge TTS n'a renvoyé aucun audio.")
        return bytes(morceaux)

    async def _synthesize_elevenlabs(self, text: str) -> bytes:
        """ElevenLabs streaming TTS — modèle turbo, latence ~300ms."""
        voice_id = (
            settings.quebec_voice_id if settings.quebec_mode else settings.elevenlabs_voice_id
        )
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
        headers = {
            "xi-api-key": settings.elevenlabs_api_key.get_secret_value(),
            "Content-Type": "application/json",
        }
        payload = {
            "text": text,
            "model_id": settings.elevenlabs_model,
            "voice_settings": {
                # stability + : voix plus posée, moins de variations
                # similarity_boost + : reste proche de la voix de référence
                # speed : débit de parole, réglable via ELEVENLABS_SPEED (0.7–1.2,
                #         1.0 = normal). Défaut 1.0 (était 0.88 = volontairement lent).
                # use_speaker_boost : présence renforcée
                "stability": 0.72,
                "similarity_boost": 0.88,
                "style": 0.0,
                "use_speaker_boost": True,
                "speed": float(os.getenv("ELEVENLABS_SPEED", "1.0")),
            },
            "optimize_streaming_latency": 3,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json=payload, headers=headers)
                if response.status_code == 200:
                    logger.debug(
                        f"ElevenLabs TTS done — {len(text)} chars, {len(response.content)} bytes"
                    )
                    cost = calculate_cost(
                        "elevenlabs", settings.elevenlabs_model, characters=len(text)
                    )
                    if self._tracker is not None:
                        self._tracker.track(
                            UsageEntry(
                                timestamp=datetime.now().isoformat(),
                                provider="elevenlabs",
                                model=settings.elevenlabs_model,
                                characters=len(text),
                                cost_usd=cost,
                                context="conversation",
                            )
                        )
                    return response.content
                logger.error(f"ElevenLabs error {response.status_code} — {response.text[:300]}")
        except Exception as e:
            logger.error("ElevenLabs request failed", error=str(e))
        # Fallback Piper si ElevenLabs échoue
        logger.warning("Falling back to Piper TTS")
        return await self._synthesize_piper(text)

    async def _synthesize_gemini(self, text: str) -> bytes:
        """Gemini TTS (Google) — voix naturelle, auth GOOGLE_API_KEY.

        L'API Gemini renvoie du PCM brut 16-bit mono 24kHz ; on l'emballe en WAV
        pour que le navigateur puisse le décoder (decodeAudioData exige un
        conteneur). Fallback Piper si pas de clé ou en cas d'erreur.
        """
        api_key = settings.google_api_key.get_secret_value()
        if not api_key:
            logger.warning("Gemini TTS: GOOGLE_API_KEY absente — fallback Piper")
            return await self._synthesize_piper(text)
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=api_key)
            config = types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=settings.gemini_tts_voice
                        )
                    )
                ),
            )
            # Sans consigne explicite, le modèle TTS « génère du texte » au lieu de
            # parler sur les phrases courtes/ambiguës (erreur 400 ou réponse sans
            # audio). On force le mode TTS via une instruction (comme le plugin
            # livekit-plugins-google), et comme le modèle preview reste non
            # déterministe, on retente une fois avant de tomber sur Piper.
            prompt = (
                "Lis ce texte à voix haute, naturellement, sans rien ajouter, "
                f'omettre ni répondre :\n"{text}"'
            )
            pcm = b""
            for attempt in range(2):
                resp = await client.aio.models.generate_content(
                    model=settings.gemini_tts_model, contents=prompt, config=config
                )
                pcm = _extract_gemini_pcm(resp)
                if pcm:
                    break
                logger.warning("Gemini TTS: pas d'audio (tentative {}/2)", attempt + 1)
            if not pcm:
                logger.error("Gemini TTS: aucun audio après retry — fallback Piper")
                return await self._synthesize_piper(text)
            if self._tracker is not None:
                # Le coût était figé à 0,0 : les appels apparaissaient au
                # registre mais gratuits, alors que la sortie audio de Gemini
                # se paie vingt fois le tarif d'un token de texte. Les vrais
                # compteurs sont dans `usage_metadata` de la réponse.
                usage = getattr(resp, "usage_metadata", None)
                entree = getattr(usage, "prompt_token_count", 0) or 0
                sortie = getattr(usage, "candidates_token_count", 0) or 0
                self._tracker.track(
                    UsageEntry(
                        timestamp=datetime.now().isoformat(),
                        provider="gemini",
                        model=settings.gemini_tts_model,
                        input_tokens=int(entree),
                        output_tokens=int(sortie),
                        characters=len(text),
                        cost_usd=calculate_cost(
                            "gemini",
                            settings.gemini_tts_model,
                            input_tokens=int(entree),
                            output_tokens=int(sortie),
                        ),
                        context="voice",
                    )
                )
            logger.debug(f"Gemini TTS done — {len(text)} chars, {len(pcm)} pcm bytes")
            return _pcm_to_wav(pcm, sample_rate=24000)
        except Exception as e:
            msg = str(e)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower():
                logger.warning(
                    "Gemini TTS: QUOTA atteint (free tier limité/jour sur les modèles "
                    "*-preview-tts). Repli Piper. Lie un compte de facturation Google, "
                    "ou repasse TTS_PROVIDER=elevenlabs/piper. ({})",
                    msg[:140],
                )
            else:
                logger.error("Gemini TTS failed: {}", msg[:200])
            return await self._synthesize_piper(text)

    async def _synthesize_piper(self, text: str) -> bytes:
        """Piper local — fallback ou provider principal."""
        logger.debug("Piper TTS request", chars=len(text))
        data = await asyncio.to_thread(self._piper_sync, text)
        logger.debug("Piper TTS done", bytes=len(data))
        return data

    def _piper_sync(self, text: str) -> bytes:

        if self._piper_voice is None:
            model_path = Path(settings.piper_model_path)
            if not model_path.exists():
                raise FileNotFoundError(
                    f"Modèle Piper introuvable : {model_path}. "
                    "Lance : mkdir -p models/piper && "
                    "curl -L -o models/piper/fr_FR-upmc-medium.onnx <url>"
                )
            self._piper_voice = PiperVoice.load(str(model_path))
            logger.info("Piper model loaded", model=str(model_path))

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            self._piper_voice.synthesize_wav(text, wf)  # type: ignore[union-attr]
        buf.seek(0)
        return buf.read()

    async def warmup(self) -> None:
        """Précharge le moteur TTS avant la première vraie demande.

        Sans cela, la toute première réponse vocale paie le chargement du
        modèle : mesuré à ~2,6 s sur le Pi 5 pour Piper, ce qui fait passer le
        premier échange de 1,2 s à 4,9 s. Une fois chargé, le modèle reste en
        mémoire et la synthèse tient x7,2 le temps réel.

        Un échec est journalisé sans être propagé : un TTS indisponible ne doit
        pas empêcher l'assistant de démarrer ni de répondre en texte.
        """
        try:
            await self.synthesize("Initialisation.")
        except Exception as exc:  # noqa: BLE001 — le texte doit survivre au TTS
            logger.warning("Préchargement TTS impossible : {}", exc)
            return
        logger.info("TTS préchargé", provider=settings.tts_provider)


def _extract_gemini_pcm(resp: object) -> bytes:
    """Concatène les chunks audio inline (PCM) d'une réponse Gemini generate_content."""
    out = bytearray()
    for cand in getattr(resp, "candidates", None) or []:
        content = getattr(cand, "content", None)
        for part in getattr(content, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None) if inline else None
            mime = str(getattr(inline, "mime_type", "")) if inline else ""
            if data and mime.startswith("audio/"):
                out.extend(data)
    return bytes(out)


def _pcm_to_wav(pcm: bytes, sample_rate: int = 24000) -> bytes:
    """Emballe du PCM 16-bit mono en conteneur WAV (décodable par le navigateur)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    buf.seek(0)
    return buf.read()


tts_engine = TTSEngine()
