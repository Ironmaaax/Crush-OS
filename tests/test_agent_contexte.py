# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Ce que le prompt système dit à l'assistant sur son utilisateur et sur lui-même.

Deux irritants constatés à l'usage, verrouillés ici :
  - « je ne sais pas encore où tu habites », alors que HOME_CITY était
    renseignée depuis le premier jour ;
  - `report_missing_capability` jamais appelé, donc Skill Lab jamais alimenté.
"""

from __future__ import annotations

from crush.engine.agent import Agent
from crush.kernel.schemas import ToolCapture  # noqa: F401 — garde l'import stable
from crush.kernel.settings import Settings


class _LLMMuet:
    @property
    def supports_tools(self) -> bool:
        return True

    async def complete(self, **_: object) -> str:
        return ""

    async def health_check(self) -> bool:
        return True


class _RegistreOutils:
    def __init__(self, noms: list[str]) -> None:
        self._noms = noms

    def has_tools(self) -> bool:
        return bool(self._noms)

    def schemas(self) -> list[dict]:
        return [{"name": n, "description": f"outil {n}", "input_schema": {}} for n in self._noms]

    async def call_str(self, name: str, inputs: dict) -> str:
        return ""


def _agent(**surcharges: object) -> Agent:
    reglages = Settings(**surcharges)  # type: ignore[arg-type]
    return Agent(settings=reglages, llm=_LLMMuet())  # type: ignore[arg-type]


# ── Irritant 1 — la ville ───────────────────────────────────────────────────


def test_la_ville_de_residence_est_dans_le_prompt() -> None:
    system = _agent(home_city="Cergy")._build_system()
    assert "Cergy" in system


def test_la_ville_est_presentee_comme_le_defaut_implicite() -> None:
    """Injecter le nom ne suffit pas : le modèle doit savoir quoi en faire.

    Sans la consigne, « quel temps il fait ? » partait chercher la météo d'une
    ville par défaut du code plutôt que celle de l'utilisateur.
    """
    system = _agent(home_city="Cergy")._build_system()
    assert "météo" in system.lower()
    assert "chez moi" in system.lower()


def test_ville_de_veille_distincte_mentionnee_a_part() -> None:
    system = _agent(home_city="Cergy", proactive_city="Paris")._build_system()
    assert "Cergy" in system
    assert "Paris" in system


def test_ville_de_veille_identique_non_repetee() -> None:
    system = _agent(home_city="Lyon", proactive_city="lyon")._build_system()
    assert system.lower().count("lyon") == 1


def test_sans_ville_aucun_bloc_reperes() -> None:
    system = _agent(home_city="", proactive_city="")._build_system()
    assert "Repères sur" not in system


# ── Irritant 2 — le signalement de capacité manquante ───────────────────────


def test_la_regle_de_signalement_est_enoncee() -> None:
    agent = _agent()
    agent._tool_registry = _RegistreOutils(["weather", "report_missing_capability"])  # type: ignore[assignment]
    system = agent._build_system()

    # Présent dans la liste des outils ET énoncé comme une règle : c'est la
    # seconde qui manquait, et sans elle le tool n'a jamais été appelé.
    assert system.count("report_missing_capability") >= 2
    assert "AVANT de répondre que tu ne sais pas faire" in system


def test_pas_de_regle_si_le_tool_est_absent() -> None:
    agent = _agent()
    agent._tool_registry = _RegistreOutils(["weather"])  # type: ignore[assignment]
    system = agent._build_system()
    assert "report_missing_capability" not in system


# ── Le moteur annoncé ───────────────────────────────────────────────────────


def test_le_backend_gemini_annonce_son_vrai_modele() -> None:
    """Absent de la table, gemini se voyait annoncer le modèle Anthropic."""
    system = _agent(
        api_backend="gemini", gemini_model="gemini-3-flash-preview"
    )._build_system()
    assert "gemini-3-flash-preview" in system
