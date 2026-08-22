# Copyright (C) 2026 Maxime Song

"""Transcrire un fichier audio en texte.

POURQUOI CET OUTIL N'EXISTAIT PAS, ET POURQUOI C'ÉTAIT UN PROBLÈME

La reconnaissance vocale était déjà là, et bonne : `CascadingSTT` essaie
OpenAI Whisper puis retombe sur un Whisper local. Mais elle n'était branchée
qu'au WebSocket vocal — `interfaces/api/voice_ws.py`, en L3, qui a le droit
d'importer `providers`. Aucun OUTIL n'y accédait, parce que `capabilities` ne
peut importer que `kernel` (RÈGLE 2) et que le Protocol vivait côté provider.

Résultat observé sur le Pi : une compétence `audio-to-text-transcription`
installée, qui échouait au chargement à chaque démarrage, et dont le contenu
n'était de toute façon que de la prose — « utiliser un modèle ASR performant
comme Whisper », « convertir avec ffmpeg ». Réparer son import aurait fait
promettre à l'assistant une transcription qu'il n'avait aucun moyen d'exécuter.
C'est cet outil qui rend la consigne vraie.

Le périmètre de fichiers et les refus sont ceux de `filesystem.py`, réutilisés
tels quels : un second jeu de règles d'accès finirait par diverger du premier,
et c'est exactement là que naissent les trous.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from loguru import logger

from crush.capabilities.tools.base import ToolResult
from crush.capabilities.tools.filesystem import _OutilFichiers, _resoudre
from crush.kernel.contracts import Transcripteur
from crush.kernel.permissions import permissions as _perms

# Plafond de l'API Whisper d'OpenAI. On refuse AVANT l'envoi : au-delà, l'appel
# part, consomme du temps et de la bande passante sur une liaison domestique,
# et revient en erreur.
_MAX_OCTETS = 25 * 1024 * 1024

# Formats que Whisper accepte, dans les deux variantes (API et local). `.ogg`
# figure en tête parce que c'est celui des messages vocaux Telegram — le cas
# d'usage qui a motivé la compétence installée.
_EXTENSIONS = {
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".mp4": "audio/mp4",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
    ".flac": "audio/flac",
    ".mpeg": "audio/mpeg",
    ".mpga": "audio/mpeg",
}


class TranscribeAudioTool(_OutilFichiers):
    """Transcrit un fichier audio présent sur la machine hôte."""

    name = "transcribe_audio"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Chemin du fichier audio sur la machine qui héberge l'assistant.",
            },
        },
        "required": ["path"],
    }

    def __init__(self, allowed_roots: list[Path], transcripteur: Transcripteur) -> None:
        super().__init__(allowed_roots)
        self._stt = transcripteur
        # Description construite à l'instance : annoncer le périmètre réel évite
        # de promettre une lecture qu'on n'obtiendra pas. Même raison que
        # `ReadFileTool`, dont on reprend la formulation pour que le modèle
        # n'ait pas deux modèles mentaux du système de fichiers.
        formats = ", ".join(sorted(_EXTENSIONS))
        self.description = (
            "Transcrit un fichier audio en texte (message vocal, enregistrement, dictée). "
            "Le fichier doit être sur la machine qui héberge l'assistant — un serveur Linux, "
            "PAS l'ordinateur de l'utilisateur. "
            f"Formats acceptés : {formats}. 25 Mo maximum. "
            f"Répertoires lisibles : {self._racines_lisibles}. "
            "Pour un fichier de l'ordinateur de l'utilisateur, le faire d'abord déposer, "
            "ou passer par remote_pc."
        )

    async def execute(self, path: str = "", **_: object) -> ToolResult:
        if not _perms.get("files"):
            return self._refus_permission()

        p, erreur = _resoudre(path)
        if p is None:
            return ToolResult(content=erreur or "Chemin invalide.", is_error=True)

        refus = self._hors_perimetre(p)
        if refus is not None:
            return ToolResult(content=refus, is_error=True)

        mime = _EXTENSIONS.get(p.suffix.lower())
        if mime is None:
            # Deviné en dernier recours : un fichier sans extension connue peut
            # tout de même être de l'audio. On ne refuse que si le type deviné
            # n'est pas audio du tout — sinon on empêcherait la transcription
            # d'un fichier parfaitement valide pour une question de nom.
            devine, _ = mimetypes.guess_type(p.name)
            if devine is None or not devine.startswith(("audio/", "video/")):
                return ToolResult(
                    content=(
                        f"Format non reconnu pour {p.name}. Extensions acceptées : "
                        f"{', '.join(sorted(_EXTENSIONS))}. Si le fichier est bien de l'audio, "
                        f"le renommer avec la bonne extension."
                    ),
                    is_error=True,
                )
            mime = devine

        try:
            infos = p.stat()
        except FileNotFoundError:
            return ToolResult(
                content=(
                    f"Fichier introuvable : {p}. Vérifier le chemin, ou le localiser "
                    f"avec find_files."
                ),
                is_error=True,
            )
        except OSError as exc:
            return ToolResult(content=f"Chemin illisible ({p}) : {exc}", is_error=True)

        if not p.is_file():
            return ToolResult(
                content=f"{p} n'est pas un fichier régulier : transcription impossible.",
                is_error=True,
            )
        if infos.st_size == 0:
            return ToolResult(content=f"{p.name} est vide : rien à transcrire.", is_error=True)
        if infos.st_size > _MAX_OCTETS:
            return ToolResult(
                content=(
                    f"Fichier trop lourd : {infos.st_size // (1024 * 1024)} Mo pour un plafond "
                    f"de {_MAX_OCTETS // (1024 * 1024)} Mo. Découper l'enregistrement, ou le "
                    f"réencoder dans un format plus compact (.ogg, .mp3)."
                ),
                is_error=True,
            )

        try:
            audio = p.read_bytes()
        except OSError as exc:
            return ToolResult(content=f"Lecture impossible ({p}) : {exc}", is_error=True)

        try:
            texte = await self._stt.transcribe(audio, mime)
        except Exception as exc:  # noqa: BLE001 — le motif compte plus que le type
            # Journalisé ET rendu au modèle : la cascade peut échouer des deux
            # côtés (pas de clé, pas de réseau, modèle local absent), et sans le
            # motif l'assistant répondrait « je n'ai pas réussi » sans savoir
            # quoi proposer ensuite.
            logger.warning(
                "Transcription échouée", fichier=p.name, mime=mime, error=str(exc)
            )
            return ToolResult(
                content=(
                    f"Transcription impossible pour {p.name} : {exc}. "
                    f"Vérifier la clé de transcription et la connexion du serveur."
                ),
                is_error=True,
            )

        propre = texte.strip()
        if not propre:
            # Un silence transcrit rend une chaîne vide. Le dire, plutôt que de
            # renvoyer un résultat vide que le modèle présenterait comme un
            # succès sans contenu.
            return ToolResult(
                content=(
                    f"{p.name} n'a produit aucun texte : l'enregistrement est probablement "
                    f"silencieux ou inaudible."
                )
            )

        logger.info("Fichier transcrit", fichier=p.name, caracteres=len(propre))
        return ToolResult(content=f"Transcription de {p.name} :\n\n{propre}")
