# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS,   .


"""Vue d'ensemble de l'état de l'assistant.

La page « Écosystème » affichait « À venir… ». La question « dans quel état est
mon assistant ? » n'avait donc aucune réponse accessible : il fallait ouvrir une
session SSH et lire des journaux, sur une machine sans écran consultée depuis un
téléphone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crush.interfaces.api import ecosysteme as eco
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


def test_un_maillon_sain_ne_propose_pas_de_remede(vue: dict) -> None:
    """La réciproque de l'invariant, et elle compte autant.

    Un remède affiché sous un voyant vert envoie agir là où rien ne cloche, et
    décrédibilise les remèdes qui, eux, sont utiles. C'est aussi le genre d'erreur
    qu'un copier-coller de chaîne introduit sans qu'aucun autre test ne s'en émeuve.
    """
    bavards = [m["nom"] for m in vue["maillons"] if m["etat"] == "ok" and m["remede"].strip()]
    assert not bavards, f"maillon sain porteur d'un remède : {bavards}"


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


def test_invariant_du_remede_tient_sur_une_installation_neuve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rejoue l'invariant sur une machine SANS base mémoire.

    La fixture `vue` lit le vrai dossier de données : sur un poste de dev la base
    existe depuis le premier échange, donc la branche « aucune base » n'y est
    jamais prise. C'est ce trou qui a laissé passer un maillon `absent` au remède
    vide — visible seulement en CI, où le checkout est neuf. Le test échouait donc
    là où personne ne pouvait le reproduire.
    """
    import asyncio

    monkeypatch.setattr(eco, "MEMORY_DATA_DIR", tmp_path)
    vue = asyncio.run(eco.ecosysteme(None))  # type: ignore[arg-type]

    muets = [
        m["nom"]
        for m in vue["maillons"]
        if m["etat"] in {"degrade", "absent"} and not m["remede"].strip()
    ]
    assert not muets, f"état dégradé sans remède : {muets}"


def test_la_boite_de_reception_apparait(vue: dict) -> None:
    noms = {m["nom"] for m in vue["maillons"]}
    assert "Boîte de réception" in noms


def test_la_boite_absente_est_signalee_pas_verte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le fichier est créé au démarrage : son absence est une anomalie réelle.

    Un voyant vert sur « fichier introuvable » laisserait croire qu'on peut
    corriger un souvenir depuis Obsidian, alors qu'il n'y a rien à ouvrir.
    """
    monkeypatch.setattr(eco, "MEMORY_DATA_DIR", tmp_path)
    monkeypatch.setattr(eco.settings, "obsidian_inbox_enabled", True, raising=False)

    maillon = eco._boite_obsidian()

    assert maillon["etat"] == "degrade"
    assert maillon["remede"]


def test_la_boite_desactivee_ne_passe_pas_pour_une_panne(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un réglage volontairement coupé n'est pas une avarie — il est « absent »."""
    monkeypatch.setattr(eco, "MEMORY_DATA_DIR", tmp_path)
    monkeypatch.setattr(eco.settings, "obsidian_inbox_enabled", False, raising=False)

    maillon = eco._boite_obsidian()

    assert maillon["etat"] == "absent"
    assert "OBSIDIAN_INBOX_ENABLED" in maillon["remede"]


def test_les_consignes_en_attente_sont_comptees_sans_le_mode_d_emploi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le mode d'emploi et l'historique ne doivent pas gonfler le compteur."""
    from crush.providers.memory.boite_reception import NOM_FICHIER, _gabarit

    miroir = tmp_path / "mirror"
    miroir.mkdir()
    fichier = miroir / NOM_FICHIER
    fichier.write_text(
        _gabarit() + "\noublie ^fact-00c2b7c5e5\n\n## Traité le 01/01/2026 à 10:00\n\n- fait\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(eco, "MEMORY_DATA_DIR", tmp_path)
    monkeypatch.setattr(eco.settings, "obsidian_inbox_enabled", True, raising=False)

    maillon = eco._boite_obsidian()

    assert maillon["etat"] == "ok"
    assert "1 consigne(s) en attente" in maillon["detail"]
