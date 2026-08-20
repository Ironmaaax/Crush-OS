# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS,   .
 

"""Isolation des états globaux que les tests manipulent.

Sans ce garde-fou, lancer la suite sur la machine de production révoquerait les
permissions de l'utilisateur : `tests/test_tools.py` basculait le singleton
`permissions` pour ses besoins, et depuis que celui-ci persiste sur disque, la
bascule s'écrit dans `memory_data/permissions.json` et y reste. Un `pytest` sur
la Pi suffisait donc à couper l'accès aux fichiers, sans que rien ne le signale.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import crush.kernel.permissions as module_permissions


@pytest.fixture(autouse=True)
def _permissions_isolees(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redirige l'état persisté du singleton vers un fichier jetable.

    `autouse` volontairement : la protection ne doit pas dépendre du fait qu'un
    auteur de test pense à la demander. C'est précisément l'oubli qui a produit
    le défaut.
    """
    # Un test verifie justement OU pointe le singleton reel : le rediriger
    # lui oterait son objet. Exemption explicite, jamais implicite.
    if request.node.get_closest_marker("permissions_reelles") is not None:
        return
    monkeypatch.setattr(
        module_permissions.permissions,
        "_path",
        tmp_path / "permissions.json",
        raising=False,
    )

@pytest.fixture(autouse=True)
def _sandbox_hote_autorise_en_test(monkeypatch: pytest.MonkeyPatch) -> None:
    """Laisse le banc d'essai du Skill Lab s'executer pendant les tests.

    En production, `skill_sandbox_allow_host_exec` vaut False et le Lab REFUSE
    de lancer une candidate hors conteneur : son skill.py est ecrit par un LLM,
    et l'executer sur l'hote lui donnerait le .env, le reseau et l'ecriture dans
    skills_data/installed/.

    Ici le risque n'existe pas : la « candidate » est ecrite par le test
    lui-meme. Sans cette autorisation, toute la logique du banc deviendrait
    intestable — y compris le test qui verifie qu'aucune porte derobee ne mene
    vers installed/.

    Le refus par defaut, lui, est couvert a part : voir
    tests/test_skill_lab_refus_hote.py, qui neutralise cette fixture.
    """
    from crush.kernel.settings import settings

    monkeypatch.setattr(settings, "skill_sandbox_allow_host_exec", True, raising=False)
