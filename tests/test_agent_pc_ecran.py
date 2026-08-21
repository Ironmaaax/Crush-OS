# Copyright (C) 2026 Maxime Song

"""L'agent PC peut dire sur quoi on travaille — et seulement si on l'a permis.

L'agent n'annonçait que six actions : volume, lancer, fermer, verrouiller, veille,
extinction. Il ne savait pas dire sur quoi on travaille, donc l'assistant ne
pouvait rien proposer de contextuel.

CE QUI EST DÉFENDU ICI

Le filtrage, avant tout le reste. Sans le drapeau `--autoriser-ecran`, l'action
n'est même pas ANNONCÉE au serveur : il ne sait pas qu'elle existe, ne peut donc
pas la demander, et une injection de prompt dans une page web lue par l'assistant
n'a rien à quoi s'accrocher. Un filtrage à l'exécution seulement laisserait
l'action visible dans le catalogue — donc tentante pour le modèle, et refusée
après coup, ce qui est la pire des deux situations.

Et son drapeau est SÉPARÉ de `--autoriser-sensibles`, parce que le risque n'est
pas du même ordre : les actions sensibles cassent quelque chose, celle-ci raconte
quelque chose. Les mélanger obligerait à accepter d'être observé pour obtenir
l'extinction, ou l'inverse.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_RACINE = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def agent() -> ModuleType:
    """Charge `scripts/agent_pc.py`, qui n'est pas un module du paquet.

    Il tourne sur la MACHINE de l'utilisateur, jamais sur le serveur : il n'a
    donc rien à faire dans `src/`. On le charge par son chemin.
    """
    chemin = _RACINE / "scripts" / "agent_pc.py"
    spec = importlib.util.spec_from_file_location("agent_pc_sous_test", chemin)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    argv = sys.argv
    sys.argv = ["agent_pc.py"]  # le module ne doit pas lire nos arguments pytest
    try:
        spec.loader.exec_module(module)
    finally:
        sys.argv = argv
    return module


# ── Le filtrage ───────────────────────────────────────────────────────────────


def test_sans_drapeau_l_ecran_n_est_pas_annonce(agent: ModuleType) -> None:
    """LE test. Non annoncée, l'action ne peut pas être demandée."""
    assert "screen" not in agent.actions_disponibles(False, False)
    assert "screen" not in agent.actions_disponibles(True, False)


def test_avec_le_drapeau_l_ecran_est_annonce(agent: ModuleType) -> None:
    assert "screen" in agent.actions_disponibles(False, True)


def test_l_ecran_ne_depend_pas_du_drapeau_des_sensibles(agent: ModuleType) -> None:
    """Deux permissions distinctes : accepter d'être observé n'est pas accepter
    qu'on éteigne la machine."""
    ecran_seul = set(agent.actions_disponibles(False, True))

    assert "screen" in ecran_seul
    assert not (ecran_seul & agent.SENSIBLES), "aucune action sensible ne doit passer"


def test_les_sensibles_ne_donnent_pas_l_ecran(agent: ModuleType) -> None:
    sensibles_seules = set(agent.actions_disponibles(True, False))

    assert agent.SENSIBLES <= sensibles_seules
    assert "screen" not in sensibles_seules


def test_l_ecran_est_dans_le_registre_des_actions(agent: ModuleType) -> None:
    """Filtrée à l'annonce, l'action doit rester exécutable quand elle est permise."""
    assert "screen" in agent.ACTIONS
    assert callable(agent.ACTIONS["screen"])


# ── Ce que l'action rend ──────────────────────────────────────────────────────


def test_l_action_rend_des_titres_et_jamais_une_image(agent: ModuleType) -> None:
    """Une capture d'écran répondrait à la même question en transmettant tout ce
    qui traîne à l'écran : une conversation privée, un mot de passe affiché. Les
    titres suffisent, et ce qui n'est pas transmis ne peut pas fuiter.
    """
    ok, texte = agent.ACTIONS["screen"]()

    assert isinstance(texte, str)
    # Pas de base64, pas d'octets : du texte lisible, et rien d'autre.
    assert "data:image" not in texte
    assert "base64" not in texte
    if ok:
        assert "Au premier plan" in texte


def test_la_liste_des_autres_fenetres_est_plafonnee(agent: ModuleType) -> None:
    """Trente onglets de navigateur ne doivent pas remplir le contexte du modèle."""
    agent_titres = ["Fenêtre " + str(i) for i in range(60)]

    # On éprouve le plafonnement par la fonction publique, en simulant la source.
    original = agent.SYSTEME
    try:
        agent.SYSTEME = "windows"
        agent._fenetres_windows = lambda: ("Au travail", agent_titres)  # type: ignore[attr-defined]
        _ok, texte = agent.screen()
    finally:
        agent.SYSTEME = original

    assert texte.count(" · ") <= 11, "au plus douze titres, donc onze séparateurs"


def test_une_session_verrouillee_est_dite_pas_devinee(agent: ModuleType) -> None:
    original = agent.SYSTEME
    try:
        agent.SYSTEME = "windows"
        agent._fenetres_windows = lambda: ("", [])  # type: ignore[attr-defined]
        ok, texte = agent.screen()
    finally:
        agent.SYSTEME = original

    assert not ok
    assert "verrouillée" in texte
