"""Reconnaissance vocale — deux moteurs derrière une seule interface.

ARCHITECTURE RETENUE
====================

Le micro est celui du NAVIGATEUR (téléphone ou PC), jamais celui de la machine
qui héberge l'assistant : elle tourne sans écran ni carte son. Le navigateur
envoie des blocs audio compressés sur le WebSocket authentifié, ce module les
transcrit, et le texte repart dans le Gateway comme un message écrit.

C'est le remplacement délibéré du pipeline LiveKit. LiveKit résout la
visioconférence multipartite : SFU, ICE, TURN, détection de tour de parole.
Pour un utilisateur seul face à un assistant, cette machinerie coûte un second
process, une part notable des dépendances, et un problème de contenu mixte
(une page HTTPS ne peut pas ouvrir un WebSocket `ws://` en clair vers le
serveur de signalisation). Un aller-retour audio sur le WebSocket déjà
authentifié n'a aucun de ces défauts.

CHOIX DU MOTEUR
===============

`STT_PROVIDER=auto` (défaut) essaie OpenAI, puis retombe sur le modèle local.
Mesuré sur le Pi 5 pour 3,1 s d'audio :

    OpenAI whisper-1     ~1 s     meilleure qualité, facturé à la minute
    local  tiny          1,2 s    (×2,6 temps réel)
    local  base          2,3 s    (×1,4 temps réel)
    local  small         6,3 s    plus lent que le temps réel — inutilisable

La bascule sert surtout au cas « clé valide mais quota épuisé » (HTTP 429),
qui renvoie un 200 côté `/v1/models` et ne se détecte donc qu'à l'usage.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Protocol, runtime_checkable

import httpx
from loguru import logger

from crush.kernel.contracts import UsageTracker
from crush.kernel.schemas import UsageEntry, calculate_cost
from crush.kernel.settings import settings

_OPENAI_TRANSCRIPTIONS_URL = "https://api.openai.com/v1/audio/transcriptions"
_HTTP_TIMEOUT = 60.0

# Le modèle local est chargé une fois puis gardé en mémoire : le premier
# chargement coûte de 30 s (tiny) à plus de 3 min (small), téléchargement
# compris. Le refaire à chaque phrase rendrait la voix inutilisable.
_local_model: object | None = None
_local_model_name: str | None = None
_local_lock = asyncio.Lock()


@runtime_checkable
class SpeechToText(Protocol):
    """Transcrit un bloc audio en texte."""

    async def transcribe(self, audio: bytes, mime_type: str) -> str: ...


class TranscriptionUnavailable(RuntimeError):
    """Aucun moteur n'a pu transcrire. Porte un message affichable à l'utilisateur."""


# ── Moteur distant : API OpenAI ──────────────────────────────────────────────


class OpenAIWhisperSTT:
    """Transcription via l'API OpenAI. Rapide et précise, facturée à la minute."""

    def __init__(self, model: str | None = None, tracker: UsageTracker | None = None) -> None:
        self._model = model or settings.openai_stt_model
        self._tracker = tracker

    def set_tracker(self, tracker: UsageTracker) -> None:
        self._tracker = tracker

    @property
    def available(self) -> bool:
        return bool(settings.openai_api_key.get_secret_value())

    @property
    def _rend_la_duree(self) -> bool:
        """`verbose_json` — seul format qui renvoie la durée de l'audio.

        Sans elle, impossible de chiffrer une facturation à la minute. Les
        modèles `gpt-4o-*-transcribe` ne l'acceptent pas : le demander leur
        vaut un 400, on s'en passe donc et l'appel n'est pas comptabilisé.
        """
        return self._model.startswith("whisper")

    async def transcribe(self, audio: bytes, mime_type: str) -> str:
        key = settings.openai_api_key.get_secret_value()
        if not key:
            raise TranscriptionUnavailable("Aucune clé OpenAI configurée.")

        filename = f"audio.{_extension_for(mime_type)}"
        data = {"model": self._model, "language": settings.stt_language}
        if self._rend_la_duree:
            data["response_format"] = "verbose_json"

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = await client.post(
                _OPENAI_TRANSCRIPTIONS_URL,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (filename, audio, mime_type)},
                data=data,
            )

        if response.status_code == 429:
            # Clé valide, crédit épuisé. Indiscernable d'une clé saine tant
            # qu'on n'a pas transcrit — d'où la bascule à l'usage.
            raise TranscriptionUnavailable("Quota OpenAI épuisé.")
        if response.status_code == 401:
            raise TranscriptionUnavailable("Clé OpenAI refusée.")
        if response.status_code != 200:
            raise TranscriptionUnavailable(
                f"OpenAI a répondu {response.status_code}."
            )

        payload = response.json()
        self._track(payload.get("duration"))
        return str(payload.get("text", "")).strip()

    def _track(self, duration_s: object) -> None:
        if self._tracker is None or not isinstance(duration_s, int | float):
            return
        minutes = float(duration_s) / 60.0
        self._tracker.track(
            UsageEntry(
                timestamp=datetime.now().isoformat(),
                provider="openai",
                model=self._model,
                audio_minutes=round(minutes, 4),
                cost_usd=calculate_cost("openai", self._model, audio_minutes=minutes),
                context="voice",
            )
        )


# ── Moteur local : faster-whisper ────────────────────────────────────────────


class LocalWhisperSTT:
    """Transcription sur la machine, via faster-whisper. Gratuite et hors-ligne.

    Exige l'extra `local-audio` (`uv sync --extra local-audio`).
    """

    def __init__(self, model_size: str | None = None) -> None:
        self._model_size = model_size or settings.whisper_model

    @property
    def available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return False
        return True

    async def _load(self) -> object:
        global _local_model, _local_model_name
        async with _local_lock:
            if _local_model is not None and _local_model_name == self._model_size:
                return _local_model
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise TranscriptionUnavailable(
                    "Extra `local-audio` absent : uv sync --extra local-audio"
                ) from exc

            logger.info("Chargement du modèle Whisper local ({})…", self._model_size)
            t0 = time.monotonic()
            # int8 : le seul type utile sur CPU ARM. `float16` échoue sur Pi —
            # c'est ce que faisait l'ancien module supprimé, avec device="auto".
            _local_model = await asyncio.to_thread(
                WhisperModel,
                self._model_size,
                device="cpu",
                compute_type="int8",
            )
            _local_model_name = self._model_size
            logger.info("Modèle Whisper prêt en {:.0f}s", time.monotonic() - t0)
            return _local_model

    async def transcribe(self, audio: bytes, mime_type: str) -> str:
        import io

        model = await self._load()

        def _run() -> str:
            # beam_size=1 : le décodage glouton divise le temps par deux pour
            # une perte de qualité marginale sur des phrases courtes.
            segments, _ = model.transcribe(  # type: ignore[attr-defined]
                io.BytesIO(audio),
                language=settings.stt_language,
                beam_size=1,
            )
            return " ".join(seg.text.strip() for seg in segments).strip()

        return await asyncio.to_thread(_run)


# ── Sélection et bascule ─────────────────────────────────────────────────────


class CascadingSTT:
    """Essaie le moteur distant, retombe sur le local à la première défaillance.

    La bascule est MÉMORISÉE : une fois le quota OpenAI constaté épuisé, on
    cesse d'y retourner pour la durée du process. Sans ça, chaque phrase
    paierait un aller-retour réseau perdu avant de se rabattre — soit une
    seconde de latence ajoutée à chaque prise de parole.
    """

    def __init__(self, remote: OpenAIWhisperSTT, local: LocalWhisperSTT) -> None:
        self._remote = remote
        self._local = local
        self._remote_disabled_reason: str | None = None

    async def transcribe(self, audio: bytes, mime_type: str) -> str:
        if self._remote.available and self._remote_disabled_reason is None:
            try:
                return await self._remote.transcribe(audio, mime_type)
            except TranscriptionUnavailable as exc:
                self._remote_disabled_reason = str(exc)
                logger.warning(
                    "STT distant désactivé pour cette session ({}) — bascule sur "
                    "le modèle local `{}`.",
                    exc,
                    settings.whisper_model,
                )
            except httpx.HTTPError as exc:
                logger.warning("STT distant injoignable ({}) — repli local.", exc)

        if not self._local.available:
            raise TranscriptionUnavailable(
                "Aucun moteur de transcription disponible : "
                f"{self._remote_disabled_reason or 'pas de clé OpenAI'}, "
                "et l'extra `local-audio` n'est pas installé."
            )
        return await self._local.transcribe(audio, mime_type)


# Valeurs héritées du pipeline LiveKit, traduites vers les moteurs disponibles
# ici. `deepgram` et `google` n'ont pas d'équivalent dans ce pipeline : plutôt
# que d'échouer au démarrage sur un .env existant, on retombe sur la cascade.
_LEGACY_ALIASES: dict[str, str] = {
    "whisper": "local",
    "deepgram": "auto",
    "google": "auto",
}


def create_stt(tracker: UsageTracker | None = None) -> SpeechToText:
    """Construit le moteur de transcription selon `STT_PROVIDER`."""
    raw = settings.stt_provider
    choice = _LEGACY_ALIASES.get(raw, raw)
    if choice != raw:
        logger.info(
            "STT_PROVIDER={} vient du pipeline LiveKit — interprété comme '{}'.",
            raw,
            choice,
        )

    remote = OpenAIWhisperSTT(tracker=tracker)
    local = LocalWhisperSTT()  # tourne sur la machine : rien à facturer

    if choice == "openai":
        return remote
    if choice == "local":
        return local
    return CascadingSTT(remote, local)


def _extension_for(mime_type: str) -> str:
    """Extension attendue par l'API OpenAI, déduite du type MIME du navigateur.

    Chrome enregistre en `audio/webm;codecs=opus`, Safari en `audio/mp4`.
    L'API se fie à l'extension du nom de fichier, pas au type MIME.
    """
    base = mime_type.split(";")[0].strip().lower()
    return {
        "audio/webm": "webm",
        "audio/ogg": "ogg",
        "audio/mp4": "mp4",
        "audio/mpeg": "mp3",
        "audio/wav": "wav",
        "audio/x-wav": "wav",
    }.get(base, "webm")
