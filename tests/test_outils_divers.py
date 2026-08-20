# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS,   .
 

"""Trois outils qui ne peuvent pas fonctionner sans un geste de l'utilisateur.

notion_tasks, remote_pc et execute_preset échouaient tous les trois par un
constat sans suite : « non configuré », « aucun canal actif », « introuvable ».
Ce qui est vérifié ici n'est donc pas seulement l'échec, mais le fait que le
message porte le nom exact de ce qui manque et le geste qui le répare — sans
quoi le modèle invente une explication ou boucle sur des noms au hasard.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic import SecretStr

from crush.capabilities.tools import notion as notion_module
from crush.capabilities.tools import preset as preset_module
from crush.capabilities.tools.base import ToolResult
from crush.capabilities.tools.notion import (
    NotionTasksTool,
    _diagnostic_http,
    _extraire_taches,
    _normaliser_page_id,
)
from crush.capabilities.tools.preset import ExecutePresetTool
from crush.capabilities.tools.remote_pc import RemotePCTool
from crush.kernel.remote_agents import RemoteAgent, registry

# ══════════════════════════════════════════════════════════════════════════════
# notion_tasks
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def notion_configure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Jeton et page présents : les tests de lecture n'ont plus à s'en soucier."""
    monkeypatch.setattr(notion_module.settings, "notion_token", SecretStr("ntn_faux"))
    monkeypatch.setattr(notion_module.settings, "notion_page_id", "0" * 32)


def _bloc_titre(texte: str) -> dict:
    return {"type": "heading_2", "heading_2": {"rich_text": [{"plain_text": texte}]}}


def _bloc_tache(texte: str, *, cochee: bool = False) -> dict:
    return {
        "type": "to_do",
        "to_do": {"checked": cochee, "rich_text": [{"plain_text": texte}]},
    }


async def test_notion_sans_configuration_nomme_les_deux_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notion_module.settings, "notion_token", SecretStr(""))
    monkeypatch.setattr(notion_module.settings, "notion_page_id", "")

    resultat = await NotionTasksTool().execute()

    assert resultat.is_error
    assert "NOTION_TOKEN" in resultat.content
    assert "NOTION_PAGE_ID" in resultat.content
    # Le jeton ne s'obtient que dans l'interface Notion : sans l'adresse, le
    # message reste une devinette.
    assert "notion.so/my-integrations" in resultat.content
    assert "Connexions" in resultat.content


async def test_notion_ne_reclame_que_la_variable_reellement_absente(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notion_module.settings, "notion_token", SecretStr("ntn_faux"))
    monkeypatch.setattr(notion_module.settings, "notion_page_id", "")

    resultat = await NotionTasksTool().execute()

    assert "NOTION_PAGE_ID" in resultat.content
    assert "NOTION_TOKEN" not in resultat.content


def test_notion_expose_la_section_dans_son_schema() -> None:
    """La description parlait d'une section ; le schéma n'en disait rien."""
    schema = NotionTasksTool().to_claude_schema()["input_schema"]

    assert "section" in schema["properties"]
    assert schema["required"] == []
    assert "Tâches du jour" in schema["properties"]["section"]["description"]


def test_notion_extrait_les_taches_de_la_bonne_section() -> None:
    blocs = [
        _bloc_titre("Notes"),
        _bloc_tache("ne pas prendre celle-ci"),
        _bloc_titre("Tâches du jour"),
        _bloc_tache("appeler le garage"),
        _bloc_tache("déjà fait", cochee=True),
        _bloc_titre("Plus tard"),
        _bloc_tache("hors section"),
    ]

    trouvee, taches, titres = _extraire_taches(blocs, "Tâches du jour")

    assert trouvee is True
    assert taches == ["appeler le garage"]
    assert titres == ["Notes", "Tâches du jour", "Plus tard"]


async def test_notion_section_absente_liste_les_titres_reels(
    monkeypatch: pytest.MonkeyPatch,
    notion_configure: None,
) -> None:
    async def faux_blocs(*_: object, **__: object) -> list[dict]:
        return [_bloc_titre("Courses"), _bloc_titre("Idées")]

    monkeypatch.setattr(NotionTasksTool, "_lire_blocs", faux_blocs)

    resultat = await NotionTasksTool().execute()

    assert resultat.is_error
    assert "Courses" in resultat.content
    assert "Idées" in resultat.content


async def test_notion_lit_une_section_choisie(
    monkeypatch: pytest.MonkeyPatch,
    notion_configure: None,
) -> None:
    async def faux_blocs(*_: object, **__: object) -> list[dict]:
        return [_bloc_titre("Courses"), _bloc_tache("pain")]

    monkeypatch.setattr(NotionTasksTool, "_lire_blocs", faux_blocs)

    resultat = await NotionTasksTool().execute(section="courses")

    assert not resultat.is_error
    assert resultat.content == "- pain"


@pytest.mark.parametrize(
    ("statut", "attendu"),
    [
        (401, "NOTION_TOKEN"),
        (404, "Connexions"),
        (403, "Connexions"),
        (429, "réessayer"),
    ],
)
def test_notion_traduit_les_codes_http_en_geste(statut: int, attendu: str) -> None:
    assert attendu in _diagnostic_http(statut)


def test_notion_accepte_une_url_collee_dans_page_id() -> None:
    """Le .env dit « depuis l'URL » : l'URL entière doit donc marcher."""
    url = "https://www.notion.so/moi/Taches-8f3c1d2e4b5a6789abcdef0123456789"

    assert _normaliser_page_id(url) == "8f3c1d2e4b5a6789abcdef0123456789"


# ══════════════════════════════════════════════════════════════════════════════
# remote_pc
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def registre_vide() -> Iterator[None]:
    """Le registre est un singleton de process : le remettre à zéro à chaque test."""
    registry._agents.clear()
    registry.set_dispatcher(None)
    yield
    registry._agents.clear()
    registry.set_dispatcher(None)


def _brancher_agent(
    nom: str = "pc-bureau",
    actions: list[str] | None = None,
) -> list[tuple[str, str, dict]]:
    """Simule un agent connecté ; retourne le journal des actions expédiées."""
    journal: list[tuple[str, str, dict]] = []

    async def dispatcher(machine: str, action: str, params: dict) -> dict:
        journal.append((machine, action, params))
        return {"ok": True, "detail": f"{action} fait"}

    registry.add(
        RemoteAgent(
            name=nom,
            platform="windows",
            actions=actions if actions is not None else ["status", "volume_set"],
        )
    )
    registry.set_dispatcher(dispatcher)
    return journal


async def test_remote_pc_sans_agent_dit_quoi_lancer() -> None:
    resultat = await RemotePCTool().execute(action="status")

    assert resultat.is_error
    assert "scripts/agent_pc.py" in resultat.content
    assert "--configurer" in resultat.content


async def test_remote_pc_inventaire_vide_donne_aussi_le_remede() -> None:
    resultat = await RemotePCTool().execute()

    assert "scripts/agent_pc.py" in resultat.content


async def test_remote_pc_action_sensible_explique_le_drapeau() -> None:
    """shutdown absent = agent lancé sans --autoriser-sensibles, pas action inexistante."""
    _brancher_agent(actions=["status"])

    resultat = await RemotePCTool().execute(action="shutdown")

    assert resultat.is_error
    assert "--autoriser-sensibles" in resultat.content


async def test_remote_pc_action_inconnue_liste_les_actions_du_poste() -> None:
    _brancher_agent(actions=["status", "lock"])

    resultat = await RemotePCTool().execute(action="danser")

    assert resultat.is_error
    assert "status" in resultat.content
    assert "lock" in resultat.content


async def test_remote_pc_execute_et_cible_le_poste_resolu() -> None:
    journal = _brancher_agent(actions=["volume_set"])

    resultat = await RemotePCTool().execute(action="volume_set", params={"level": 0.4})

    assert not resultat.is_error
    assert journal == [("pc-bureau", "volume_set", {"level": 0.4})]


async def test_remote_pc_machine_inconnue_nomme_les_postes_connectes() -> None:
    _brancher_agent(nom="pc-bureau")
    _brancher_agent(nom="portable")

    resultat = await RemotePCTool().execute(action="status", machine="pc-salon")

    assert resultat.is_error
    assert "pc-bureau" in resultat.content
    assert "portable" in resultat.content


def test_remote_pc_documente_les_actions_et_leur_sensibilite() -> None:
    schema = RemotePCTool().to_claude_schema()["input_schema"]
    description = schema["properties"]["action"]["description"]

    for action in ("status", "volume_set", "app_launch", "lock", "shutdown"):
        assert action in description
    assert "[sensible]" in description
    assert "--autoriser-sensibles" in description


# ══════════════════════════════════════════════════════════════════════════════
# execute_preset
# ══════════════════════════════════════════════════════════════════════════════


class _FauxPreset:
    def __init__(self, nom: str, description: str = "") -> None:
        self.name = nom
        self.label = nom
        self.description = description


class _FauxRegistreSkills:
    def __init__(self, presets: dict[str, _FauxPreset]) -> None:
        self._presets = presets

    def get_presets(self) -> dict[str, _FauxPreset]:
        return self._presets

    def get_preset(self, nom: str) -> _FauxPreset | None:
        return self._presets.get(nom)


def _outil_preset(
    monkeypatch: pytest.MonkeyPatch,
    presets: dict[str, _FauxPreset],
) -> ExecutePresetTool:
    monkeypatch.setattr(preset_module, "skill_registry", _FauxRegistreSkills(presets))
    return ExecutePresetTool(tool_registry=None, tts_engine=None)  # type: ignore[arg-type]


def _brancher_executeur(monkeypatch: pytest.MonkeyPatch, resultats: dict) -> None:
    class FauxExecuteur:
        def __init__(self, **_: object) -> None: ...

        async def execute(self, *_: object, **__: object) -> dict:
            return resultats

    monkeypatch.setattr(preset_module, "PresetExecutor", FauxExecuteur)


async def test_preset_sans_argument_ne_plante_pas_et_dit_le_vide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`preset_name` obligatoire faisait planter l'appel exploratoire du modèle."""
    resultat = await _outil_preset(monkeypatch, {}).execute()

    assert not resultat.is_error
    assert "Aucun preset" in resultat.content
    assert "skill.yaml" in resultat.content
    assert "installed" in resultat.content


async def test_preset_sans_argument_liste_les_presets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    presets = {"mode-nuit": _FauxPreset("mode-nuit", "éteint tout")}
    resultat = await _outil_preset(monkeypatch, presets).execute()

    assert "mode-nuit" in resultat.content
    assert "éteint tout" in resultat.content


async def test_preset_absent_sans_aucun_preset_ecarte_la_faute_de_frappe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resultat = await _outil_preset(monkeypatch, {}).execute(preset_name="mode-nuit")

    assert resultat.is_error
    assert "faute de frappe" in resultat.content
    assert "/api/skills/reload" in resultat.content


async def test_preset_absent_parmi_des_presets_suggere_le_plus_proche(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    presets = {"mode-nuit": _FauxPreset("mode-nuit"), "mode-travail": _FauxPreset("mode-travail")}

    resultat = await _outil_preset(monkeypatch, presets).execute(preset_name="mode-nuits")

    assert resultat.is_error
    assert "mode-nuit" in resultat.content
    assert "Vouliez-vous dire" in resultat.content


async def test_preset_remonte_le_blocage_applications_requises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un preset non lancé annonçait « 0 étapes réalisées », ce qui passait pour un succès."""
    _brancher_executeur(
        monkeypatch,
        {
            "success": False,
            "error": "Applications requises non installées : OBS",
            "steps_done": 0,
            "steps_skipped": 0,
            "steps_failed": 0,
            "logs": [],
        },
    )
    outil = _outil_preset(monkeypatch, {"mode-stream": _FauxPreset("mode-stream")})

    resultat = await outil.execute(preset_name="mode-stream")

    assert resultat.is_error
    assert "OBS" in resultat.content


async def test_preset_detaille_les_etapes_en_erreur(monkeypatch: pytest.MonkeyPatch) -> None:
    _brancher_executeur(
        monkeypatch,
        {
            "success": True,
            "steps_done": 0,
            "steps_skipped": 0,
            "steps_failed": 1,
            "logs": [{"step": "lancer obs", "status": "failed", "message": "commande absente"}],
        },
    )
    outil = _outil_preset(monkeypatch, {"mode-stream": _FauxPreset("mode-stream")})

    resultat = await outil.execute(preset_name="mode-stream")

    assert resultat.is_error
    assert "lancer obs" in resultat.content
    assert "commande absente" in resultat.content


async def test_preset_reussi_reste_un_succes(monkeypatch: pytest.MonkeyPatch) -> None:
    _brancher_executeur(
        monkeypatch,
        {
            "success": True,
            "steps_done": 3,
            "steps_skipped": 1,
            "steps_failed": 0,
            "logs": [],
        },
    )
    outil = _outil_preset(monkeypatch, {"mode-nuit": _FauxPreset("mode-nuit")})

    resultat: ToolResult = await outil.execute(preset_name="mode-nuit")

    assert not resultat.is_error
    assert "3 étapes réalisées" in resultat.content
