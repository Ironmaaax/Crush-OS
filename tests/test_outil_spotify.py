# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Outil `spotify_control` : introspection, contrat d'actions, choix d'appareil.

Ces tests remplacent l'API Spotify par un faux client HTTP. Ils gardent trois
propriétés que l'audit en production a trouvées absentes :

  1. le modèle peut savoir ce qui joue AVANT d'affirmer qu'il a lancé quelque
     chose (action `status`) ;
  2. les actions annoncées dans le schéma sont exactement celles que le code
     sert, et un refus liste les actions valides ;
  3. la lecture évite l'onglet du navigateur — correction antérieure qu'aucun
     test ne protégeait, alors que la refaire tomber coupe la musique dès que
     l'assistant parle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from crush.capabilities.tools.spotify import SpotifyTool

_NOM_ONGLET = "Crush"


class _FauxReponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        # Un 204 (ou un 200 sans corps) n'a pas de contenu : le code de l'outil
        # s'appuie sur `content` pour ne pas décoder du vide.
        self.content = b"{}" if payload is not None else b""
        self.headers: dict[str, str] = {}

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("corps vide")
        return self._payload

    def raise_for_status(self) -> None:
        if not self.is_success:
            raise httpx.HTTPStatusError(
                "erreur", request=httpx.Request("GET", "https://x"), response=self  # type: ignore[arg-type]
            )


class _FauxClient:
    """Client httpx minimal : router (méthode, chemin) -> réponses successives.

    Les valeurs peuvent être une réponse unique ou une liste consommée appel
    après appel, pour rejouer les séquences de l'API (204 puis 200, etc.).
    """

    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self._routes = routes
        self.appels: list[tuple[str, str, dict]] = []

    async def __aenter__(self) -> _FauxClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def _repondre(self, methode: str, url: str, **kwargs: object) -> _FauxReponse:
        chemin = url.replace("https://api.spotify.com/v1", "")
        self.appels.append((methode, chemin, kwargs))
        valeur = self._routes.get((methode, chemin))
        if valeur is None:
            return _FauxReponse(404, {})
        if isinstance(valeur, list):
            return valeur.pop(0) if len(valeur) > 1 else valeur[0]
        return valeur

    async def get(self, url: str, **kwargs: object) -> _FauxReponse:
        return self._repondre("GET", url, **kwargs)

    async def put(self, url: str, **kwargs: object) -> _FauxReponse:
        return self._repondre("PUT", url, **kwargs)

    async def post(self, url: str, **kwargs: object) -> _FauxReponse:
        return self._repondre("POST", url, **kwargs)


def _monter(routes: dict[tuple[str, str], Any]) -> tuple[Any, _FauxClient]:
    client = _FauxClient(routes)
    return patch(
        "crush.capabilities.tools.spotify.httpx.AsyncClient", return_value=client
    ), client


@pytest.fixture(autouse=True)
def _spotify_connecte(monkeypatch: pytest.MonkeyPatch) -> None:
    """Court-circuite OAuth et fige le nom de l'onglet du Web Playback SDK.

    Le nom d'affichage de l'assistant est ce qui permet de distinguer l'onglet
    des vrais appareils : le figer rend les tests indépendants du réglage.
    """
    import crush.capabilities.tools.spotify as module

    async def _token() -> str:
        return "jeton-test"

    monkeypatch.setattr(module, "_get_access_token", _token)
    monkeypatch.setattr(
        type(module.settings), "display_assistant_name", property(lambda _: _NOM_ONGLET)
    )


def _piste(nom: str = "Bad Guy", artiste: str = "Billie Eilish") -> dict:
    return {
        "name": nom,
        "artists": [{"name": artiste}],
        "duration_ms": 194_000,
        "uri": "spotify:track:1",
    }


# ── Contrat d'actions : schéma, description, refus ────────────────────────────


def test_le_schema_annonce_exactement_les_actions_servies() -> None:
    """Le schéma est la seule chose que le modèle voit : il doit être exact.

    L'audit a trouvé une description annonçant six actions pour huit servies,
    ce qui poussait le modèle à en inventer une neuvième (`status`).
    """
    outil = SpotifyTool()
    annoncees = set(outil.input_schema["properties"]["action"]["enum"])
    assert annoncees == {
        "status",
        "play",
        "pause",
        "toggle",
        "next",
        "previous",
        "search_track",
        "search_playlist",
        "volume_delta",
    }


def test_chaque_action_est_decrite_dans_le_parametre_action() -> None:
    outil = SpotifyTool()
    texte = outil.input_schema["properties"]["action"]["description"]
    for nom in outil.input_schema["properties"]["action"]["enum"]:
        assert f"'{nom}'" in texte


def test_delta_est_declare_dans_le_schema() -> None:
    """`volume_delta` lit `delta` : sans déclaration, le modèle ne peut l'envoyer."""
    outil = SpotifyTool()
    assert outil.input_schema["properties"]["delta"]["type"] == "integer"


async def test_action_inconnue_liste_les_actions_valides() -> None:
    patcheur, _ = _monter({})
    with patcheur:
        resultat = await SpotifyTool().execute(action="jouer_du_jazz")

    assert resultat.is_error
    assert "jouer_du_jazz" in resultat.content
    for nom in ("status", "play", "search_track", "volume_delta"):
        assert nom in resultat.content


async def test_action_vide_reste_explicite() -> None:
    patcheur, _ = _monter({})
    with patcheur:
        resultat = await SpotifyTool().execute()

    assert resultat.is_error
    assert "status" in resultat.content


async def test_la_casse_de_l_action_est_toleree() -> None:
    routes = {("GET", "/me/player"): _FauxReponse(204)}
    patcheur, _ = _monter(routes)
    with patcheur:
        resultat = await SpotifyTool().execute(action="  Status ")

    assert not resultat.is_error
    assert "Rien ne joue" in resultat.content


# ── Action status ─────────────────────────────────────────────────────────────


async def test_status_rapporte_piste_artiste_et_appareil() -> None:
    routes = {
        ("GET", "/me/player"): _FauxReponse(
            200,
            {
                "is_playing": True,
                "progress_ms": 60_000,
                "item": _piste(),
                "device": {
                    "id": "mac",
                    "name": "MacBook de Max",
                    "type": "Computer",
                    "volume_percent": 55,
                },
            },
        )
    }
    patcheur, _ = _monter(routes)
    with patcheur:
        resultat = await SpotifyTool().execute(action="status")

    assert not resultat.is_error
    assert "Lecture en cours" in resultat.content
    assert "Bad Guy" in resultat.content
    assert "Billie Eilish" in resultat.content
    assert "MacBook de Max" in resultat.content
    assert "55 %" in resultat.content
    assert "1:00 / 3:14" in resultat.content


async def test_status_distingue_la_pause_de_la_lecture() -> None:
    routes = {
        ("GET", "/me/player"): _FauxReponse(
            200,
            {
                "is_playing": False,
                "item": _piste(),
                "device": {"id": "mac", "name": "MacBook de Max", "type": "Computer"},
            },
        )
    }
    patcheur, _ = _monter(routes)
    with patcheur:
        resultat = await SpotifyTool().execute(action="status")

    assert not resultat.is_error
    assert resultat.content.startswith("En pause")


async def test_status_sans_lecture_nest_pas_une_erreur_et_liste_les_appareils() -> None:
    """« Rien ne joue » est une réponse, pas une panne.

    La marquer `is_error` ferait retenter le modèle au lieu de le laisser
    répondre honnêtement.
    """
    routes = {
        ("GET", "/me/player"): _FauxReponse(204),
        ("GET", "/me/player/devices"): _FauxReponse(
            200, {"devices": [{"id": "tel", "name": "iPhone de Max", "type": "Smartphone"}]}
        ),
    }
    patcheur, _ = _monter(routes)
    with patcheur:
        resultat = await SpotifyTool().execute(action="status")

    assert not resultat.is_error
    assert "Rien ne joue" in resultat.content
    assert "iPhone de Max" in resultat.content


async def test_status_sans_appareil_donne_la_marche_a_suivre() -> None:
    routes = {
        ("GET", "/me/player"): _FauxReponse(204),
        ("GET", "/me/player/devices"): _FauxReponse(200, {"devices": []}),
    }
    patcheur, _ = _monter(routes)
    with patcheur:
        resultat = await SpotifyTool().execute(action="status")

    assert "aucun appareil Spotify" in resultat.content
    assert "remote_pc" in resultat.content


async def test_status_signale_que_seul_l_onglet_est_disponible() -> None:
    """Le seul appareil est la page web : la voix de l'assistant coupera la musique."""
    routes = {
        ("GET", "/me/player"): _FauxReponse(204),
        ("GET", "/me/player/devices"): _FauxReponse(
            200, {"devices": [{"id": "web", "name": _NOM_ONGLET, "type": "Computer"}]}
        ),
    }
    patcheur, _ = _monter(routes)
    with patcheur:
        resultat = await SpotifyTool().execute(action="status")

    assert "page web" in resultat.content
    assert "synthèse vocale" in resultat.content


async def test_status_avertit_quand_la_lecture_tourne_dans_l_onglet() -> None:
    routes = {
        ("GET", "/me/player"): _FauxReponse(
            200,
            {
                "is_playing": True,
                "item": _piste(),
                "device": {"id": "web", "name": _NOM_ONGLET, "type": "Computer"},
            },
        )
    }
    patcheur, _ = _monter(routes)
    with patcheur:
        resultat = await SpotifyTool().execute(action="status")

    assert "page web" in resultat.content


async def test_status_supporte_une_lecture_sans_piste() -> None:
    """Publicité, podcast ou session privée : `item` est nul, ce n'est pas un plantage."""
    routes = {
        ("GET", "/me/player"): _FauxReponse(
            200,
            {"is_playing": True, "item": None, "device": {"id": "mac", "name": "MacBook"}},
        )
    }
    patcheur, _ = _monter(routes)
    with patcheur:
        resultat = await SpotifyTool().execute(action="status")

    assert not resultat.is_error
    assert "aucune piste" in resultat.content


async def test_status_jeton_revoque_indique_la_reautorisation() -> None:
    routes = {("GET", "/me/player"): _FauxReponse(401, {})}
    patcheur, _ = _monter(routes)
    with patcheur:
        resultat = await SpotifyTool().execute(action="status")

    assert resultat.is_error
    assert "/api/spotify/auth" in resultat.content


async def test_status_ne_declenche_aucune_commande_de_lecture() -> None:
    """L'introspection doit être sans effet de bord : elle sert à décider."""
    routes = {
        ("GET", "/me/player"): _FauxReponse(
            200, {"is_playing": False, "item": _piste(), "device": {"id": "mac", "name": "Mac"}}
        )
    }
    patcheur, client = _monter(routes)
    with patcheur:
        await SpotifyTool().execute(action="status")

    assert all(methode == "GET" for methode, _, _ in client.appels)


# ── Choix d'appareil : l'onglet reste le dernier recours ──────────────────────


async def test_la_lecture_prefere_un_appareil_externe_a_l_onglet() -> None:
    """Garde-fou : rejouer la musique dans l'onglet la fait couper par la voix."""
    routes = {
        ("GET", "/me/player/devices"): _FauxReponse(
            200,
            {
                "devices": [
                    {"id": "web", "name": _NOM_ONGLET, "type": "Computer", "is_active": True},
                    {"id": "mac", "name": "MacBook de Max", "type": "Computer"},
                ]
            },
        ),
        ("PUT", "/me/player/play"): _FauxReponse(204),
        ("GET", "/me/player"): _FauxReponse(200, {"is_playing": True}),
    }
    patcheur, client = _monter(routes)
    with patcheur:
        resultat = await SpotifyTool().execute(action="play")

    assert not resultat.is_error
    lectures = [k for m, c, k in client.appels if (m, c) == ("PUT", "/me/player/play")]
    assert lectures[0]["params"] == {"device_id": "mac"}


async def test_l_onglet_sert_de_dernier_recours() -> None:
    routes = {
        ("GET", "/me/player/devices"): _FauxReponse(
            200, {"devices": [{"id": "web", "name": _NOM_ONGLET, "type": "Computer"}]}
        ),
        ("PUT", "/me/player/play"): _FauxReponse(204),
        ("GET", "/me/player"): _FauxReponse(200, {"is_playing": True}),
    }
    patcheur, client = _monter(routes)
    with patcheur:
        await SpotifyTool().execute(action="play")

    lectures = [k for m, c, k in client.appels if (m, c) == ("PUT", "/me/player/play")]
    assert lectures[0]["params"] == {"device_id": "web"}


# ── Volume ────────────────────────────────────────────────────────────────────


async def test_volume_delta_accepte_un_entier_en_chaine() -> None:
    """Le schéma déclare un entier, mais les modèles envoient parfois « -10 »."""
    routes = {
        ("GET", "/me/player"): _FauxReponse(200, {"device": {"volume_percent": 40}}),
        ("PUT", "/me/player/volume"): _FauxReponse(204),
    }
    patcheur, _ = _monter(routes)
    with patcheur:
        resultat = await SpotifyTool().execute(action="volume_delta", delta="-10")

    assert not resultat.is_error
    assert "30" in resultat.content


async def test_volume_delta_refuse_une_valeur_ininterpretable() -> None:
    routes = {("GET", "/me/player"): _FauxReponse(200, {"device": {"volume_percent": 40}})}
    patcheur, _ = _monter(routes)
    with patcheur:
        resultat = await SpotifyTool().execute(action="volume_delta", delta="fort")

    assert resultat.is_error
    assert "delta" in resultat.content


# ── Erreurs HTTP des recherches ───────────────────────────────────────────────


async def test_une_recherche_refusee_rend_un_message_et_non_une_exception() -> None:
    """`raise_for_status()` levait hors des `except` : l'outil plantait sans message."""
    routes = {("GET", "/search"): _FauxReponse(429, {})}
    patcheur, _ = _monter(routes)
    with patcheur:
        resultat = await SpotifyTool().execute(action="search_track", query="jazz")

    assert resultat.is_error
    assert "Réessaie" in resultat.content


# ── Mémoire de l'appareil ────────────────────────────────────────────────────
#
# Sans elle, il fallait nommer l'enceinte à chaque demande sous peine de
# retomber sur le premier appareil venu — souvent l'onglet du navigateur, dont
# la lecture meurt avec la page.


def _fichier_temporaire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isole la préférence : jamais d'écriture dans le vrai memory_data."""
    from crush.capabilities.tools import spotify as mod

    cible = tmp_path / "spotify_appareil.json"
    monkeypatch.setattr(mod, "_FICHIER_APPAREIL", cible)
    return cible


def test_appareil_externe_retenu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from crush.capabilities.tools import spotify as mod

    _fichier_temporaire(tmp_path, monkeypatch)
    mod._memoriser_appareil({"name": "Enceinte salon", "type": "speaker"})

    assert mod._appareil_prefere() == "Enceinte salon"


def test_l_onglet_n_est_jamais_retenu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Le retenir ramènerait la lecture dans la page à chaque fois.

    C'est l'inverse du but : l'onglet est le dernier recours, pas une
    préférence.
    """
    from crush.capabilities.tools import spotify as mod
    from crush.kernel.settings import settings

    _fichier_temporaire(tmp_path, monkeypatch)
    mod._memoriser_appareil({"name": settings.display_assistant_name, "type": "computer"})

    assert mod._appareil_prefere() == ""


def test_preference_absente_ou_abimee_ne_leve_pas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Une préférence illisible ne doit pas empêcher de lancer la musique."""
    from crush.capabilities.tools import spotify as mod

    cible = _fichier_temporaire(tmp_path, monkeypatch)
    assert mod._appareil_prefere() == ""

    cible.write_text("{ pas du JSON", encoding="utf-8")
    assert mod._appareil_prefere() == ""

    cible.write_text('["une liste"]', encoding="utf-8")
    assert mod._appareil_prefere() == ""


def test_disque_en_lecture_seule_ne_casse_pas_la_lecture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ne pas pouvoir retenir la préférence n'est pas un échec de la commande."""
    from crush.capabilities.tools import spotify as mod

    _fichier_temporaire(tmp_path, monkeypatch)

    def _refuse(*_a: object, **_k: object) -> None:
        raise OSError("disque en lecture seule")

    monkeypatch.setattr(mod, "ecrire_atomique", _refuse)
    mod._memoriser_appareil({"name": "Enceinte salon", "type": "speaker"})  # ne lève pas
