# Copyright (C) 2026 Maxime Song

"""L'outil de transcription — et surtout ses refus.

POURQUOI IL EXISTE

La reconnaissance vocale était présente et fonctionnelle (`CascadingSTT` :
OpenAI Whisper puis Whisper local en repli), mais branchée UNIQUEMENT au
WebSocket vocal — `interfaces/api/voice_ws.py`, en L3, seule couche autorisée à
importer `providers`. Aucun outil n'y accédait, faute d'un Protocol côté
`kernel`.

Conséquence mesurée sur le Pi : une compétence `audio-to-text-transcription`
installée, en échec de chargement à chaque démarrage, dont le contenu n'était
que de la prose (« utiliser un modèle ASR comme Whisper », « convertir avec
ffmpeg »). En réparer l'import aurait fait promettre à l'assistant une
transcription qu'il ne pouvait pas exécuter.

CE QUI EST TESTÉ EN PRIORITÉ

Les refus, pas le succès. Un outil qui lit des fichiers sur la machine hôte et
envoie leur contenu à une API tierce doit refuser proprement AVANT d'agir : hors
périmètre, trop lourd, mauvais format, permission absente. Le chemin heureux est
une ligne ; les refus sont la sécurité.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crush.capabilities.tools.transcription import _MAX_OCTETS, TranscribeAudioTool
from crush.kernel.permissions import permissions as _perms


class _STTDouble:
    """Transcripteur de test. Retient ce qu'on lui a envoyé."""

    def __init__(self, texte: str = "Bonjour monsieur.", explose: Exception | None = None) -> None:
        self._texte = texte
        self._explose = explose
        self.appels: list[tuple[int, str]] = []

    async def transcribe(self, audio: bytes, mime_type: str) -> str:
        self.appels.append((len(audio), mime_type))
        if self._explose is not None:
            raise self._explose
        return self._texte


@pytest.fixture(autouse=True)
def permission_fichiers() -> object:
    """La permission « Fichiers » conditionne l'outil. On l'active pour les
    tests, et on restaure l'état d'origine — sinon un test laisserait la
    permission ouverte pour les suivants."""
    avant = _perms.get("files")
    _perms.set("files", True)
    yield
    _perms.set("files", avant)


def _outil(tmp_path: Path, stt: _STTDouble | None = None) -> TranscribeAudioTool:
    return TranscribeAudioTool(
        allowed_roots=[tmp_path], transcripteur=stt or _STTDouble()  # type: ignore[arg-type]
    )


# ── Le chemin heureux ─────────────────────────────────────────────────────────


async def test_un_ogg_est_transcrit(tmp_path: Path) -> None:
    """`.ogg` d'abord : c'est le format des messages vocaux Telegram, le cas
    d'usage qui a motivé la compétence installée."""
    f = tmp_path / "vocal.ogg"
    f.write_bytes(b"OggS" + b"\x00" * 200)
    stt = _STTDouble("Lance Liberta sur mon PC.")

    r = await _outil(tmp_path, stt).execute(path=str(f))

    assert not r.is_error
    assert "Lance Liberta" in r.content
    assert stt.appels == [(204, "audio/ogg")]


async def test_le_type_mime_suit_lextension(tmp_path: Path) -> None:
    """Whisper choisit son décodeur d'après le type déclaré : un `.wav` annoncé
    en `audio/ogg` échouerait côté API."""
    stt = _STTDouble()
    for nom in ("a.wav", "b.mp3", "c.m4a"):
        f = tmp_path / nom
        f.write_bytes(b"\x00" * 50)
        await _outil(tmp_path, stt).execute(path=str(f))
    assert [m for _, m in stt.appels] == ["audio/wav", "audio/mpeg", "audio/mp4"]


async def test_un_silence_est_annonce_et_non_rendu_vide(tmp_path: Path) -> None:
    """Un enregistrement muet rend une chaîne vide. La renvoyer telle quelle
    aurait produit un succès sans contenu, que le modèle aurait présenté comme
    une transcription réussie."""
    f = tmp_path / "muet.ogg"
    f.write_bytes(b"\x00" * 100)

    r = await _outil(tmp_path, _STTDouble("   ")).execute(path=str(f))

    assert not r.is_error
    assert "silencieux" in r.content


# ── Les refus ─────────────────────────────────────────────────────────────────


async def test_hors_perimetre_refuse(tmp_path: Path) -> None:
    """LE refus qui compte. L'outil lit un fichier de la machine hôte et l'envoie
    à une API tierce : le périmètre autorisé est ce qui empêche d'exfiltrer
    n'importe quel fichier en le renommant `.ogg`."""
    dehors = tmp_path.parent / "dehors.ogg"
    dehors.write_bytes(b"\x00" * 10)
    autorise = tmp_path / "ok"
    autorise.mkdir()

    r = await TranscribeAudioTool(
        allowed_roots=[autorise], transcripteur=_STTDouble()  # type: ignore[arg-type]
    ).execute(path=str(dehors))

    assert r.is_error
    assert "hors des répertoires autorisés" in r.content


async def test_sans_permission_refuse(tmp_path: Path) -> None:
    f = tmp_path / "a.ogg"
    f.write_bytes(b"\x00" * 10)
    _perms.set("files", False)

    r = await _outil(tmp_path).execute(path=str(f))

    assert r.is_error
    assert "permission" in r.content.lower()


async def test_un_format_non_audio_est_refuse(tmp_path: Path) -> None:
    """Sinon l'outil devient un moyen d'envoyer n'importe quel fichier — une
    base SQLite, un `.env` — à une API tierce."""
    f = tmp_path / "secrets.txt"
    f.write_text("mot de passe")
    stt = _STTDouble()

    r = await _outil(tmp_path, stt).execute(path=str(f))

    assert r.is_error
    assert "Format non reconnu" in r.content
    assert stt.appels == [], "le fichier a été envoyé malgré le refus"


async def test_un_fichier_trop_lourd_est_refuse_avant_lenvoi(tmp_path: Path) -> None:
    """Refusé AVANT l'appel : au-delà du plafond de Whisper, l'envoi consomme du
    temps et de la bande passante sur une liaison domestique pour revenir en
    erreur."""
    f = tmp_path / "long.ogg"
    f.write_bytes(b"\x00" * (_MAX_OCTETS + 1))
    stt = _STTDouble()

    r = await _outil(tmp_path, stt).execute(path=str(f))

    assert r.is_error
    assert "trop lourd" in r.content
    assert stt.appels == []


async def test_un_fichier_vide_est_refuse(tmp_path: Path) -> None:
    f = tmp_path / "vide.ogg"
    f.write_bytes(b"")
    stt = _STTDouble()

    r = await _outil(tmp_path, stt).execute(path=str(f))

    assert r.is_error
    assert "vide" in r.content
    assert stt.appels == []


async def test_un_fichier_absent_oriente_vers_find_files(tmp_path: Path) -> None:
    r = await _outil(tmp_path).execute(path=str(tmp_path / "fantome.ogg"))
    assert r.is_error
    assert "introuvable" in r.content
    assert "find_files" in r.content


async def test_un_repertoire_nest_pas_transcrit(tmp_path: Path) -> None:
    d = tmp_path / "dossier.ogg"
    d.mkdir()
    r = await _outil(tmp_path).execute(path=str(d))
    assert r.is_error


async def test_un_chemin_vide_est_refuse(tmp_path: Path) -> None:
    r = await _outil(tmp_path).execute(path="")
    assert r.is_error


async def test_un_echec_du_transcripteur_donne_le_motif(tmp_path: Path) -> None:
    """La cascade peut échouer des deux côtés : pas de clé, pas de réseau, modèle
    local absent. Sans le motif, l'assistant répond « je n'ai pas réussi » sans
    savoir quoi proposer."""
    f = tmp_path / "a.ogg"
    f.write_bytes(b"\x00" * 10)
    stt = _STTDouble(explose=RuntimeError("clé OpenAI absente"))

    r = await _outil(tmp_path, stt).execute(path=str(f))

    assert r.is_error
    assert "clé OpenAI absente" in r.content


# ── La description annoncée au modèle ─────────────────────────────────────────


def test_la_description_annonce_le_perimetre_reel(tmp_path: Path) -> None:
    """Le modèle doit savoir que le fichier est sur le SERVEUR, pas sur le PC de
    Max — sinon il promet une transcription d'un fichier qu'il ne verra jamais."""
    outil = _outil(tmp_path)
    assert str(tmp_path) in outil.description
    assert "remote_pc" in outil.description
    assert ".ogg" in outil.description
