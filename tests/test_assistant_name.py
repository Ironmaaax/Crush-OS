# Copyright (C) 2026 Barthélemy Houot
# This file is part of CRUSH-OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Garde-fou : ASSISTANT_NAME non configuré doit valoir « Crush », jamais «  ».

Le champ brut `assistant_name` a pour défaut la chaîne vide. Un `.env` déjà en
place ne contient pas la clé — donc lire le champ brut dans un prompt produit
« Tu es . » chez tous les utilisateurs existants, silencieusement. Le repli est
centralisé dans la propriété `display_assistant_name`, miroir de `display_name`.
"""

from __future__ import annotations

import pytest

from crush.kernel.settings import Settings


@pytest.mark.parametrize(
    ("configured", "attendu"),
    [
        ("", "Crush"),  # .env existant, clé absente
        ("   ", "Crush"),  # champ vidé depuis l'UI Réglages
        ("Vendredi", "Vendredi"),
        ("  Vendredi  ", "Vendredi"),
    ],
)
def test_display_assistant_name_repli(configured: str, attendu: str) -> None:
    assert Settings(assistant_name=configured).display_assistant_name == attendu


def test_display_assistant_name_defaut_sans_champ(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings construit sans mentionner assistant_name nulle part.

    `_env_file=None` et la purge de la variable sont indispensables : sans
    elles, le test lit le .env du poste de dev et passe ou echoue selon ce
    que CE poste a configure, au lieu de verifier le repli du code.
    """
    monkeypatch.delenv("ASSISTANT_NAME", raising=False)
    assert Settings(_env_file=None).display_assistant_name == "Crush"


def test_prompts_ne_contiennent_jamais_de_nom_vide() -> None:
    """Les prompts système construits sans ASSISTANT_NAME nomment bien l'assistant."""
    cfg = Settings(assistant_name="")
    nom = cfg.display_assistant_name
    for gabarit in (
        f"Tu es {nom}. Exécute la tâche demandée.",
        f"[{nom} proactif] titre — action",
    ):
        assert "Tu es ." not in gabarit
        assert "[ proactif]" not in gabarit
        assert nom in gabarit
