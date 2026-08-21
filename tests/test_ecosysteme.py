# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS,   .


"""Vue d'ensemble de l'état de l'assistant.

La page « Écosystème » affichait « À venir… ». La question « dans quel état est
mon assistant ? » n'avait donc aucune réponse accessible : il fallait ouvrir une
session SSH et lire des journaux, sur une machine sans écran consultée depuis un
téléphone.
"""

from __future__ import annotations

import pytest

from crush.interfaces.api.ecosysteme import ecosysteme


@pytest.fixture
def vue() -> dict:
    import asyncio

    return asyncio.run(ecosysteme(None))  # type: ignore[arg-type]


def test_chaque_probleme_porte_son_remede(vue: dict) -> None:
    """L'invariant qui fait toute la valeur de la page.

    Un voyant rouge sans marche à suivre ne vaut guère mieux qu'un silence :
    il informe qu'il y a un problème sans permettre d'agir. C'est exactement
    ce que faisaient les outils avant leur reprise — « Credentials Google
    manquants », sans dire lesquels ni où.
    """
    muets = [
        m["nom"]
        for m in vue["maillons"]
        if m["etat"] in {"degrade", "absent"} and not m["remede"].strip()
    ]
    assert not muets, f"état dégradé sans remède : {muets}"


def test_les_familles_sont_toutes_renseignees(vue: dict) -> None:
    """Un maillon sans famille n'apparaîtrait dans aucune section de la page."""
    orphelins = [m["nom"] for m in vue["maillons"] if not m["famille"]]
    assert not orphelins


def test_le_resume_compte_tous_les_maillons(vue: dict) -> None:
    r = vue["resume"]
    assert r["ok"] + r["degrade"] + r["absent"] == len(vue["maillons"])


def test_aucun_etat_hors_vocabulaire(vue: dict) -> None:
    """Un état inconnu retomberait silencieusement sur « non configuré »."""
    connus = {"ok", "degrade", "absent"}
    assert {m["etat"] for m in vue["maillons"]} <= connus


def test_l_execution_hors_conteneur_est_surveillee(vue: dict) -> None:
    """Ce réglage donne au code écrit par un LLM les droits du service.

    Il doit rester visible sur cette page : c'est le genre de garde-fou qu'on
    désactive « le temps d'un test » et qu'on oublie de remettre.
    """
    noms = {m["nom"] for m in vue["maillons"]}
    assert "Exécution hors conteneur" in noms
