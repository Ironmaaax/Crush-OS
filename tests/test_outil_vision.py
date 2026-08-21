# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS,   .


"""Tests de l'outil `vision` sur une machine sans caméra ni écran.

Le décor par défaut (fixture `pi`) reproduit la machine réelle : Linux/aarch64,
aucun /dev/video*, aucune variable DISPLAY. C'est là que l'outil doit être le
plus honnête, et c'est là qu'il mentait le plus.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from crush.capabilities.tools import vision as mod

# ── Doublures ────────────────────────────────────────────────────────────────


class _Perms:
    def __init__(self, **etat: bool) -> None:
        self._etat = etat

    def get(self, cle: str) -> bool:
        return self._etat.get(cle, True)


class _Plateforme:
    def __init__(self, systeme: str, machine: str = "aarch64") -> None:
        self._systeme = systeme
        self._machine = machine

    def system(self) -> str:
        return self._systeme

    def machine(self) -> str:
        return self._machine


class _Agent:
    def __init__(self, name: str, actions: list[str]) -> None:
        self.name = name
        self.actions = actions


class _Registre:
    def __init__(self, agents: list[_Agent] | None = None) -> None:
        self._agents = agents or []

    def list_agents(self) -> list[_Agent]:
        return self._agents


class _Memoire:
    def __init__(self, resultats: list[str] | None = None) -> None:
        self.resultats = resultats or []
        self.stockes: list[tuple[str, str, str]] = []

    async def search(self, query: str) -> list[str]:
        return self.resultats

    async def store(
        self, description: str, source: str, context: str = "", tags: list[str] | None = None
    ) -> None:
        self.stockes.append((description, source, context))


class _Completions:
    def __init__(self, texte: str) -> None:
        self.texte = texte
        self.appels: list[dict] = []

    async def create(self, **kwargs: object) -> SimpleNamespace:
        self.appels.append(dict(kwargs))
        message = SimpleNamespace(content=self.texte)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _Client:
    def __init__(self, texte: str = "Je vois un chat.") -> None:
        self.completions = _Completions(texte)
        self.chat = SimpleNamespace(completions=self.completions)


def _explose(*_: object, **__: object) -> bytes:
    raise AssertionError("la capture ne devait pas être tentée")


# ── Décor ────────────────────────────────────────────────────────────────────


@pytest.fixture
def pi(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """Raspberry Pi headless : ni caméra, ni serveur d'affichage, permissions ouvertes."""
    dev = tmp_path / "dev"
    dev.mkdir()
    sysfs = tmp_path / "video4linux"
    sysfs.mkdir()
    monkeypatch.setattr(mod, "_DEV", dev)
    monkeypatch.setattr(mod, "_SYS_V4L2", sysfs)
    monkeypatch.setattr(mod, "platform", _Plateforme("Linux"))
    monkeypatch.setattr(mod, "_perms", _Perms(camera=True, screen=True))
    monkeypatch.setattr(mod, "_agents_distants", _Registre())
    monkeypatch.setattr(mod, "_opencv_disponible", lambda: True)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    return SimpleNamespace(dev=dev, sysfs=sysfs)


def _brancher_camera(pi, noeud: str = "video0", nom: str = "UVC Camera") -> None:  # noqa: ANN001
    (pi.dev / noeud).write_text("", encoding="utf-8")
    dossier = pi.sysfs / noeud
    dossier.mkdir()
    (dossier / "name").write_text(nom, encoding="utf-8")


def _outil(memoire: _Memoire | None = None, client: object = "defaut") -> mod.VisionTool:
    outil = mod.VisionTool(visual_memory=memoire or _Memoire())
    if client == "defaut":
        client = _Client()
    outil._get_openai_client = lambda: client  # type: ignore[method-assign]
    return outil


# ── recall : la seule action qui ne dépend d'aucun matériel ──────────────────


async def test_recall_fonctionne_sans_materiel_ni_cle(pi: SimpleNamespace) -> None:  # noqa: ANN001
    outil = _outil(_Memoire(["un schéma de PCB montré en mai"]), client=None)
    outil._capture_webcam = _explose  # type: ignore[method-assign]

    res = await outil.execute(question="le schéma", action="recall")

    assert not res.is_error
    assert "schéma de PCB" in res.content


# ── Normalisation de la source ───────────────────────────────────────────────


async def test_alias_camera_nest_pas_une_source_inconnue(pi: SimpleNamespace) -> None:  # noqa: ANN001
    """Le modèle écrit spontanément 'camera' ; le refuser masquait le vrai problème."""
    res = await _outil().execute(question="tu vois quoi ?", source="camera")

    assert res.is_error
    assert "Source inconnue" not in res.content
    assert "Aucune caméra" in res.content


async def test_source_reellement_inconnue_est_nommee(pi: SimpleNamespace) -> None:  # noqa: ANN001
    res = await _outil().execute(question="?", source="satellite")

    assert res.is_error
    assert "satellite" in res.content
    assert "'webcam'" in res.content


# ── Caméra : matériel absent ≠ permission refusée ────────────────────────────


async def test_sans_camera_le_message_exclut_la_piste_permission(pi: SimpleNamespace) -> None:  # noqa: ANN001
    outil = _outil()
    outil._capture_webcam = _explose  # type: ignore[method-assign]

    res = await outil.execute(question="tu vois quoi ?", source="webcam")

    assert res.is_error
    assert "Aucune caméra" in res.content
    assert "pas une question de permission" in res.content
    # Le remède actionnable, et l'aveu que l'ordinateur de l'utilisateur est hors d'atteinte.
    assert "webcam USB" in res.content
    assert "n'est pas\njoignable" in res.content or "joignable depuis ici" in res.content


async def test_sans_camera_aucune_reference_a_macos(pi: SimpleNamespace) -> None:  # noqa: ANN001
    """L'ancien message renvoyait aux Préférences Système sur un serveur Debian."""
    res = await _outil().execute(question="?", source="webcam")

    assert "Préférences Système" not in res.content
    assert "Mac" not in res.content


async def test_codec_du_raspberry_nest_pas_pris_pour_une_camera(pi: SimpleNamespace) -> None:  # noqa: ANN001
    """/dev/video10 = codec matériel bcm2835, pas un capteur."""
    _brancher_camera(pi, noeud="video10", nom="bcm2835-codec-decode")

    res = await _outil().execute(question="?", source="webcam")

    assert "Aucune caméra" in res.content


async def test_noeud_au_nom_illisible_est_conserve(pi: SimpleNamespace) -> None:  # noqa: ANN001
    """Sans /sys lisible on ne peut rien prouver : mieux vaut tenter que nier."""
    (pi.dev / "video0").write_text("", encoding="utf-8")

    assert mod._cameras_detectees() == [str(pi.dev / "video0")]


async def test_camera_presente_mais_permission_refusee(
    pi: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    _brancher_camera(pi)
    monkeypatch.setattr(mod, "_perms", _Perms(camera=False, screen=True))
    outil = _outil()
    outil._capture_webcam = _explose  # type: ignore[method-assign]

    res = await outil.execute(question="?", source="webcam")

    assert res.is_error
    assert "Permission « caméra » désactivée" in res.content
    assert "UVC Camera" in res.content  # le matériel est là, on le dit
    assert "/api/permissions/camera" in res.content


async def test_camera_presente_mais_opencv_absent(
    pi: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    _brancher_camera(pi)
    monkeypatch.setattr(mod, "_opencv_disponible", lambda: False)

    res = await _outil().execute(question="?", source="webcam")

    assert res.is_error
    assert "cv2" in res.content
    assert "opencv-python" in res.content
    assert "Traceback" not in res.content


async def test_import_cv2_manquant_devient_un_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repli si opencv est absent au moment même de la capture (course d'installation)."""
    import builtins

    vrai_import = builtins.__import__

    def _import(nom: str, *args: object, **kwargs: object) -> object:
        if nom == "cv2":
            raise ImportError("No module named 'cv2'")
        return vrai_import(nom, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _import)

    with pytest.raises(mod.CaptureImpossible) as exc:
        mod.VisionTool(visual_memory=_Memoire())._capture_webcam()

    assert "opencv-python" in str(exc.value)


async def test_cameras_indecidables_hors_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sur macOS on ne sait pas énumérer : ne jamais affirmer une absence."""
    monkeypatch.setattr(mod, "platform", _Plateforme("Darwin", "arm64"))

    assert mod._cameras_detectees() is None


# ── Écran : headless ≠ permission refusée ────────────────────────────────────


async def test_sans_session_graphique_le_message_est_definitif(pi: SimpleNamespace) -> None:  # noqa: ANN001
    outil = _outil()
    outil._capture_screen = _explose  # type: ignore[method-assign]

    res = await outil.execute(question="?", source="screen")

    assert res.is_error
    assert "DISPLAY" in res.content and "WAYLAND_DISPLAY" in res.content
    assert "pas une question de permission" in res.content


async def test_sans_ecran_remote_pc_nest_pas_promis_a_tort(pi: SimpleNamespace) -> None:  # noqa: ANN001
    """L'agent distant n'annonce aucune capture : le dire, pas suggérer l'inverse."""
    res = await _outil().execute(question="?", source="screen")

    assert "remote_pc" in res.content
    assert "aucune action de capture" in res.content


async def test_agent_distant_capable_est_cite_nommement(
    pi: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        mod,
        "_agents_distants",
        _Registre([_Agent("mac-maxime", ["volume_set", "screenshot"])]),
    )

    res = await _outil().execute(question="?", source="screen")

    assert "mac-maxime" in res.content
    assert "screenshot" in res.content
    assert "aucune action de capture" not in res.content


async def test_ecran_present_mais_permission_refusee(
    pi: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(mod, "_perms", _Perms(camera=True, screen=False))
    outil = _outil()
    outil._capture_screen = _explose  # type: ignore[method-assign]

    res = await outil.execute(question="?", source="screen")

    assert res.is_error
    assert "Permission « capture d'écran » désactivée" in res.content
    assert "/api/permissions/screen" in res.content
    assert "DISPLAY" not in res.content  # ce n'est pas le sujet ici


async def test_pillow_sans_xcb_donne_un_message_pas_une_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Grab:
        @staticmethod
        def grab() -> object:
            raise OSError("Pillow was built without XCB support")

    monkeypatch.setattr(mod, "ImageGrab", _Grab)

    with pytest.raises(mod.CaptureImpossible) as exc:
        mod.VisionTool(visual_memory=_Memoire())._capture_screen_pil()

    assert "XCB" in str(exc.value)
    assert "grim" in str(exc.value)


async def test_capture_impossible_remonte_en_erreur_outil(
    pi: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    monkeypatch.setenv("DISPLAY", ":0")

    def _echec() -> bytes:
        raise mod.CaptureImpossible("Le serveur X refuse la connexion.")

    outil = _outil()
    outil._capture_screen = _echec  # type: ignore[method-assign]

    res = await outil.execute(question="?", source="screen")

    assert res.is_error
    assert res.content == "Le serveur X refuse la connexion."


# ── Clé d'API ────────────────────────────────────────────────────────────────


async def test_sans_cle_openai_aucune_capture_nest_tentee(
    pi: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    monkeypatch.setenv("DISPLAY", ":0")
    outil = _outil(client=None)
    outil._capture_screen = _explose  # type: ignore[method-assign]

    res = await outil.execute(question="?", source="screen")

    assert res.is_error
    assert "OPENAI_API_KEY" in res.content
    assert "La capture n'a pas été tentée." in res.content


# ── Chemin nominal ───────────────────────────────────────────────────────────


async def test_capture_ecran_analysee_et_memorisee(
    pi: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    monkeypatch.setenv("DISPLAY", ":0")
    memoire = _Memoire()
    client = _Client("Un terminal ouvert sur htop.")
    outil = _outil(memoire, client=client)
    outil._capture_screen = lambda: b"\xff\xd8jpeg"  # type: ignore[method-assign]

    res = await outil.execute(question="que vois-tu ?", source="screen", action="snapshot")
    await asyncio.sleep(0)  # laisse tourner la tâche « fire and forget » de mémorisation

    assert not res.is_error
    assert res.content == "Un terminal ouvert sur htop."
    assert memoire.stockes and memoire.stockes[0][1] == "screen"
    assert client.completions.appels[0]["max_tokens"] == 1024


async def test_read_document_force_le_detail_haut(
    pi: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    monkeypatch.setenv("DISPLAY", ":0")
    client = _Client("Facture n°42.")
    outil = _outil(client=client)
    outil._capture_screen = lambda: b"\xff\xd8jpeg"  # type: ignore[method-assign]

    await outil.execute(question="lis", source="screen", action="read_document")
    await asyncio.sleep(0)

    envoye = client.completions.appels[0]
    image = envoye["messages"][0]["content"][0]["image_url"]
    assert image["detail"] == "high"
    assert envoye["max_tokens"] == 2000


# ── Description exposée au modèle ────────────────────────────────────────────


def test_description_situe_la_capture_sur_le_serveur() -> None:
    """La description induisait le modèle en erreur sur la machine visée."""
    description = mod.VisionTool.description

    assert "HÉBERGE l'assistant" in description
    assert "jamais sur l'ordinateur de l'utilisateur" in description
