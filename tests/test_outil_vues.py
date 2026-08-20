# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS,   .
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tests de l'outil show_view — découverte des vues et honnêteté du retour.

Le fil directeur : côté navigateur, `Crush.views.activate()` ignore
silencieusement un view_id inconnu, et un broadcast vers zéro abonné ne
lève rien. Ces tests vérifient que l'outil refuse au lieu d'annoncer un
affichage qui n'a pas eu lieu.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crush.capabilities.tools import show_view as mod
from crush.capabilities.tools.show_view import ACTIONS, ShowViewTool


class FauxFile:
    """Imite ProactiveQueue : c'est `_subscribers` que l'outil inspecte."""

    def __init__(self, nb_clients: int = 1) -> None:
        self._subscribers = [object()] * nb_clients
        self.events: list[dict] = []

    def broadcast_event(self, event: dict) -> None:
        self.events.append(event)


@pytest.fixture(autouse=True)
def _arbre_vide(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isole la découverte du dépôt réel (et vide le cache inter-tests)."""
    monkeypatch.setattr(mod, "UI_STATIC_DIR", tmp_path / "static")
    monkeypatch.setattr(mod, "SKILLS_INSTALLED_DIR", tmp_path / "installed")
    mod._JS_IDS_CACHE.clear()
    return tmp_path


def _installer_vue(
    racine: Path,
    dossier: str,
    view_id: str,
    *,
    avec_yaml: bool = True,
    description: str = "Vue de test",
) -> None:
    statique = racine / "static" / "skills" / dossier
    statique.mkdir(parents=True, exist_ok=True)
    (statique / "view.js").write_text(
        f"(function(){{\n  const VIEW_ID = '{view_id}';\n"
        f"  Crush.views.register(VIEW_ID, {{}});\n}})();\n",
        encoding="utf-8",
    )
    if avec_yaml:
        meta = racine / "installed" / dossier
        meta.mkdir(parents=True, exist_ok=True)
        (meta / "skill.yaml").write_text(
            f'name: {dossier}\ntype: view\ndescription: "{description}"\n', encoding="utf-8"
        )


# ── Découverte ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_est_une_action_valide() -> None:
    """Le défaut d'origine : action=list répondait « Action inconnue »."""
    assert "list" in ACTIONS
    assert "list" in ShowViewTool.input_schema["properties"]["action"]["enum"]

    outil = ShowViewTool(broadcast_event=FauxFile().broadcast_event)
    res = await outil.execute(action="list")
    assert "Action inconnue" not in res.content


@pytest.mark.asyncio
async def test_list_donne_les_view_id_installes(_arbre_vide: Path) -> None:
    _installer_vue(_arbre_vide, "globe-view", "globe", description="Globe terrestre")
    outil = ShowViewTool(broadcast_event=FauxFile().broadcast_event)

    res = await outil.execute(action="list")

    assert not res.is_error
    assert "globe" in res.content
    assert "globe-view" in res.content
    assert "Globe terrestre" in res.content


@pytest.mark.asyncio
async def test_list_sans_vue_explique_comment_en_installer() -> None:
    outil = ShowViewTool(broadcast_event=FauxFile().broadcast_event)

    res = await outil.execute(action="list")

    assert "Aucune vue" in res.content
    # Le message doit être une instruction : quoi créer, et par où.
    assert "skill.yaml" in res.content
    assert "/api/skills/install/" in res.content


@pytest.mark.asyncio
async def test_list_signale_les_assets_sans_skill_installee(_arbre_vide: Path) -> None:
    """Cas réel en production : le JS est sur le disque, le skill.yaml manque."""
    _installer_vue(_arbre_vide, "astronomy", "astronomy", avec_yaml=False)
    outil = ShowViewTool(broadcast_event=FauxFile().broadcast_event)

    res = await outil.execute(action="list")

    assert "astronomy" in res.content
    assert "skill.yaml" in res.content


@pytest.mark.asyncio
async def test_list_previent_quand_aucun_navigateur(_arbre_vide: Path) -> None:
    _installer_vue(_arbre_vide, "clock", "clock")
    outil = ShowViewTool(broadcast_event=FauxFile(nb_clients=0).broadcast_event)

    res = await outil.execute(action="list")

    assert "clock" in res.content
    assert "navigateur" in res.content


# ── Actions inconnues / paramètres manquants ──────────────────────────────


@pytest.mark.asyncio
async def test_action_inconnue_liste_les_actions_valides() -> None:
    outil = ShowViewTool(broadcast_event=FauxFile().broadcast_event)

    res = await outil.execute(action="teleporter")

    assert res.is_error
    for attendue in ACTIONS:
        assert attendue in res.content


@pytest.mark.asyncio
async def test_view_id_manquant_renvoie_vers_list() -> None:
    outil = ShowViewTool(broadcast_event=FauxFile().broadcast_event)

    res = await outil.execute(action="show")

    assert res.is_error
    assert 'action="list"' in res.content


# ── Refus d'un view_id que le navigateur n'a pas chargé ───────────────────


@pytest.mark.asyncio
async def test_show_refuse_une_vue_inconnue_et_liste_les_vues(_arbre_vide: Path) -> None:
    _installer_vue(_arbre_vide, "clock", "clock")
    file = FauxFile()
    outil = ShowViewTool(broadcast_event=file.broadcast_event)

    res = await outil.execute(action="show", view_id="hologramme")

    assert res.is_error
    assert "clock" in res.content
    assert file.events == []  # rien n'est parti : pas de faux succès


@pytest.mark.asyncio
async def test_show_sans_aucune_vue_installee_explique_l_installation() -> None:
    file = FauxFile()
    outil = ShowViewTool(broadcast_event=file.broadcast_event)

    res = await outil.execute(action="show", view_id="globe")

    assert res.is_error
    assert "/api/skills/install/" in res.content
    assert file.events == []


@pytest.mark.asyncio
async def test_fly_to_refuse_quand_le_globe_manque(_arbre_vide: Path) -> None:
    _installer_vue(_arbre_vide, "clock", "clock")
    file = FauxFile()
    outil = ShowViewTool(broadcast_event=file.broadcast_event)

    res = await outil.execute(action="fly_to", location="Lyon")

    assert res.is_error
    assert "globe" in res.content
    assert file.events == []


# ── Absence de navigateur ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_show_sans_navigateur_ne_pretend_pas_avoir_affiche(_arbre_vide: Path) -> None:
    _installer_vue(_arbre_vide, "clock", "clock")
    file = FauxFile(nb_clients=0)
    outil = ShowViewTool(broadcast_event=file.broadcast_event)

    res = await outil.execute(action="show", view_id="clock")

    assert res.is_error
    assert "navigateur" in res.content
    assert file.events == []


@pytest.mark.asyncio
async def test_home_sans_navigateur_echoue_aussi() -> None:
    file = FauxFile(nb_clients=0)
    outil = ShowViewTool(broadcast_event=file.broadcast_event)

    res = await outil.execute(action="home")

    assert res.is_error
    assert file.events == []


@pytest.mark.asyncio
async def test_comptage_indetermine_ne_bloque_pas(_arbre_vide: Path) -> None:
    """Fonction nue (process voice_agent) : on ne sait pas, donc on n'invente pas."""
    _installer_vue(_arbre_vide, "clock", "clock")
    recus: list[dict] = []
    outil = ShowViewTool(broadcast_event=recus.append)

    res = await outil.execute(action="show", view_id="clock")

    assert not res.is_error
    assert recus == [{"type": "show_view", "view_id": "clock"}]


@pytest.mark.asyncio
async def test_count_clients_injecte_est_prioritaire(_arbre_vide: Path) -> None:
    _installer_vue(_arbre_vide, "clock", "clock")
    file = FauxFile(nb_clients=3)
    outil = ShowViewTool(broadcast_event=file.broadcast_event, count_clients=lambda: 0)

    res = await outil.execute(action="show", view_id="clock")

    assert res.is_error
    assert file.events == []


# ── Chemins nominaux préservés ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_show_nominal_emet_l_evenement(_arbre_vide: Path) -> None:
    _installer_vue(_arbre_vide, "clock", "clock")
    file = FauxFile()
    outil = ShowViewTool(broadcast_event=file.broadcast_event)

    res = await outil.execute(action="show", view_id="clock")

    assert not res.is_error
    assert file.events == [{"type": "show_view", "view_id": "clock"}]
    assert "interface web" in res.content


@pytest.mark.asyncio
async def test_fly_to_nominal_affiche_le_globe_puis_navigue(_arbre_vide: Path) -> None:
    _installer_vue(_arbre_vide, "globe-view", "globe")
    file = FauxFile()
    outil = ShowViewTool(broadcast_event=file.broadcast_event)

    # « lyon » est dans CITY_COORDS : aucun appel réseau.
    res = await outil.execute(action="fly_to", location="Lyon", zoom=42)

    assert not res.is_error
    assert file.events[0] == {"type": "show_view", "view_id": "globe"}
    assert file.events[1]["command"] == "fly_to"
    assert file.events[1]["params"]["zoom"] == 18  # borné à 18
    assert file.events[1]["params"]["location_name"] == "Lyon"


@pytest.mark.asyncio
async def test_globe_view_nominal(_arbre_vide: Path) -> None:
    _installer_vue(_arbre_vide, "globe-view", "globe")
    file = FauxFile()
    outil = ShowViewTool(broadcast_event=file.broadcast_event)

    res = await outil.execute(action="globe_view")

    assert not res.is_error
    assert [e["type"] for e in file.events] == ["show_view", "view_command"]


@pytest.mark.asyncio
async def test_view_command_nominal(_arbre_vide: Path) -> None:
    _installer_vue(_arbre_vide, "astronomy", "astronomy")
    file = FauxFile()
    outil = ShowViewTool(broadcast_event=file.broadcast_event)

    res = await outil.execute(
        action="view_command",
        view_id="astronomy",
        command="focus_constellation",
        params={"name": "Orion"},
    )

    assert not res.is_error
    assert file.events == [
        {
            "type": "view_command",
            "view_id": "astronomy",
            "command": "focus_constellation",
            "params": {"name": "Orion"},
        }
    ]


# ── Description ───────────────────────────────────────────────────────────


def test_description_ne_promet_pas_un_ecran_physique() -> None:
    """La Pi n'a pas d'écran : la description doit désigner le navigateur."""
    desc = ShowViewTool.description
    assert "écran principal" not in desc
    assert "interface web" in desc.lower()
    assert "navigateur" in desc.lower()


def test_schema_action_enumere_les_actions() -> None:
    prop = ShowViewTool.input_schema["properties"]["action"]
    assert prop["enum"] == list(ACTIONS)
    for attendue in ACTIONS:
        assert attendue in prop["description"]


# ── Découverte : identifiant déclaré par un littéral ──────────────────────


@pytest.mark.asyncio
async def test_id_lu_dans_un_register_litteral(_arbre_vide: Path) -> None:
    """globe.js écrit `Crush.views.register('globe', …)` sans constante."""
    statique = _arbre_vide / "static" / "skills" / "globe-view"
    statique.mkdir(parents=True)
    (statique / "globe.js").write_text(
        "Crush.views.register('globe', { meta: {} });\n", encoding="utf-8"
    )
    meta = _arbre_vide / "installed" / "globe-view"
    meta.mkdir(parents=True)
    (meta / "skill.yaml").write_text("type: view\n", encoding="utf-8")

    outil = ShowViewTool(broadcast_event=FauxFile().broadcast_event)
    res = await outil.execute(action="list")

    assert "globe" in res.content


@pytest.mark.asyncio
async def test_fallback_sur_le_nom_du_dossier_sans_id_dans_le_js(_arbre_vide: Path) -> None:
    statique = _arbre_vide / "static" / "skills" / "memory-map"
    statique.mkdir(parents=True)
    (statique / "view.js").write_text("/* pas d'identifiant lisible */\n", encoding="utf-8")
    meta = _arbre_vide / "installed" / "memory-map"
    meta.mkdir(parents=True)
    (meta / "skill.yaml").write_text("type: view\n", encoding="utf-8")

    outil = ShowViewTool(broadcast_event=FauxFile().broadcast_event)
    res = await outil.execute(action="list")

    assert "memory-map" in res.content
