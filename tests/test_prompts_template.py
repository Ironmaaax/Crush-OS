# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS,   .
 

"""Rendu des gabarits de prompt — `kernel/prompts.py`.

Remplace la personnalisation par `str.replace("Max", …)`, qui frappait
n'importe quelle sous-chaîne et ne couvrait qu'un fichier.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from crush.kernel.paths import PROMPTS_DIR
from crush.kernel.prompts import (
    UnknownPlaceholderError,
    placeholders,
    render,
    render_file,
)


def test_substitution_simple() -> None:
    assert render("Salut {{user}}.", user="Max") == "Salut Max."


def test_espaces_dans_le_placeholder_tolerés() -> None:
    assert render("Salut {{ user }}.", user="Max") == "Salut Max."


def test_placeholder_inconnu_leve() -> None:
    """Mieux vaut un démarrage bruyant qu'un assistant qui dit « {{user}} »."""
    with pytest.raises(UnknownPlaceholderError, match="assistant"):
        render("Je suis {{assistant}}.", user="Max")


def test_accolades_simples_preservees() -> None:
    """Les prompts contiennent du JSON d'exemple — `str.format` le casserait."""
    gabarit = 'Réponds en JSON : {"nom": "valeur"} pour {{user}}.'
    assert render(gabarit, user="Max") == 'Réponds en JSON : {"nom": "valeur"} pour Max.'


def test_valeur_contenant_un_placeholder_non_reinterpretee() -> None:
    """Une substitution ne doit pas relancer la substitution sur son résultat."""
    assert render("{{user}}", user="{{assistant}}") == "{{assistant}}"


def test_pas_de_substitution_partielle_dans_un_mot() -> None:
    """C'était le défaut de `str.replace` : « Max » devenait « Maxélemy »."""
    assert render("Max et {{user}}", user="Max") == "Max et Max"


def test_placeholders_listes() -> None:
    assert placeholders("{{user}} parle à {{assistant}}") == {"user", "assistant"}


# ── Le prompt système réel ───────────────────────────────────────────────────


def test_prompt_systeme_ne_contient_plus_de_nom_en_dur() -> None:
    """Garde-fou : aucun nom propre ne doit revenir dans le gabarit."""
    texte = (PROMPTS_DIR / "system_static.md").read_text(encoding="utf-8")
    assert not re.search(r"\bBarth\b", texte), "« Max » est revenu en dur"
    assert not re.search(r"\bCrush\b", texte), "« Crush » est revenu en dur"


def _rendre(nom: str) -> str:
    """Rend un prompt de canal, persona incluse — comme le fait `Agent`."""
    persona = render_file(PROMPTS_DIR / "persona.md", user="Max", assistant="Vendredi")
    return render_file(
        PROMPTS_DIR / nom, user="Max", assistant="Vendredi", persona=persona
    )


@pytest.mark.parametrize("nom", ["system_static.md", "system_voice.md"])
def test_prompt_se_rend_entierement(nom: str) -> None:
    rendu = _rendre(nom)
    assert "Max" in rendu
    assert "Vendredi" in rendu
    assert "{{" not in rendu, "un placeholder n'a pas été résolu"


@pytest.mark.parametrize("nom", ["system_static.md", "system_voice.md"])
def test_prompt_n_a_que_les_variables_cablees(nom: str) -> None:
    """Un nouveau placeholder dans un prompt doit être câblé dans agent.py."""
    texte = (PROMPTS_DIR / nom).read_text(encoding="utf-8")
    assert placeholders(texte) <= {"user", "assistant", "persona"}


def test_persona_ne_cite_que_user_et_assistant() -> None:
    """La persona est rendue AVANT injection : elle ne peut pas citer {{persona}}."""
    texte = (PROMPTS_DIR / "persona.md").read_text(encoding="utf-8")
    assert placeholders(texte) <= {"user", "assistant"}


def test_persona_presente_dans_les_deux_canaux() -> None:
    """Le caractère doit être identique à l'oral et à l'écrit."""
    for nom in ("system_static.md", "system_voice.md"):
        texte = (PROMPTS_DIR / nom).read_text(encoding="utf-8")
        assert "persona" in placeholders(texte), f"{nom} n'inclut pas la persona"


def test_render_file_lit_en_utf8(tmp_path: Path) -> None:
    """Encodage explicite : sans lui, Windows lit en cp1252 et casse les accents."""
    f = tmp_path / "g.md"
    f.write_text("Préférences de {{user}} — café", encoding="utf-8")
    assert render_file(f, user="Max") == "Préférences de Max — café"
