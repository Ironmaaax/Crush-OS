# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS,   .
 

"""Arbitrage des suggestions proactives depuis la conversation.

Le moteur proactif diffusait une initiative `validate` UNE SEULE FOIS par
WebSocket. La Pi tourne 24 h/24 et personne n'est connecté la plupart du temps :
quinze suggestions attendaient un arbitrage impossible à rendre depuis la voix,
qui est l'usage principal. Ces tests couvrent la lecture et la décision.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from crush.capabilities.tools.initiatives import InitiativesTool


class _MagasinFactice:
    def __init__(self, initiatives: list[object] | None = None) -> None:
        self.items = list(initiatives or [])
        self.decisions: list[tuple[str, str]] = []

    def load_pending_all(self, days: int = 7) -> list[object]:
        return list(self.items)

    def update_status(self, initiative_id: str, status: str) -> None:
        self.decisions.append((initiative_id, status))


def _init(id_: str, titre: str, priorite: str = "medium") -> SimpleNamespace:
    return SimpleNamespace(
        id=id_,
        title=titre,
        action=f"faire {titre}",
        priority=priorite,
        # Seul ce mode attend un arbitrage humain — cf. _en_attente.
        execution_mode="validate",
    )


async def test_aucune_suggestion_le_dit_sans_erreur() -> None:
    outil = InitiativesTool(store=_MagasinFactice())

    res = await outil.execute(action="list")

    assert not res.is_error
    assert "Aucune suggestion" in res.content


async def test_les_plus_urgentes_en_premier() -> None:
    """L'ordre importe : l'assistant en propose une seule à la fois."""
    magasin = _MagasinFactice(
        [_init("a", "basse", "low"), _init("b", "haute", "high"), _init("c", "moyenne", "medium")]
    )
    outil = InitiativesTool(store=magasin)

    contenu = (await outil.execute(action="list")).content

    assert contenu.index("haute") < contenu.index("moyenne") < contenu.index("basse")


async def test_la_liste_est_bornee_mais_annonce_le_reste() -> None:
    """Douze suggestions lues à voix haute seraient inécoutables."""
    magasin = _MagasinFactice([_init(f"i{n}", f"sujet{n}") for n in range(12)])
    outil = InitiativesTool(store=magasin)

    contenu = (await outil.execute(action="list")).content

    assert "12 suggestion(s)" in contenu
    assert "et 6 autre(s)" in contenu


async def test_accepter_enregistre_la_decision() -> None:
    magasin = _MagasinFactice([_init("i1", "deep work"), _init("i2", "sortie")])
    outil = InitiativesTool(store=magasin)

    res = await outil.execute(action="approve", initiative_id="i1")

    assert not res.is_error
    assert magasin.decisions == [("i1", "approved")]
    assert "deep work" in res.content
    assert "reste 1" in res.content


async def test_ecarter_enregistre_le_refus() -> None:
    magasin = _MagasinFactice([_init("i1", "deep work")])
    outil = InitiativesTool(store=magasin)

    await outil.execute(action="reject", initiative_id="i1")

    assert magasin.decisions == [("i1", "rejected")]


async def test_identifiant_inconnu_refuse_au_lieu_d_ecrire_dans_le_vide() -> None:
    """`update_status` ne lève pas sur un id absent : il ne fait rien.

    Sans cette vérification, l'assistant annonçait « c'est accepté » alors
    qu'aucune décision n'avait été enregistrée.
    """
    magasin = _MagasinFactice([_init("i1", "deep work")])
    outil = InitiativesTool(store=magasin)

    res = await outil.execute(action="approve", initiative_id="inexistant")

    assert res.is_error
    assert magasin.decisions == [], "aucune écriture ne doit avoir eu lieu"
    # Le refus liste desormais les TITRES et non les identifiants : a l'oral,
    # « deep work » est exploitable, « i1 » ne l'est pas.
    assert "deep work" in res.content, "le refus doit rappeler ce qui est en attente"


async def test_identifiant_manquant_est_signale() -> None:
    outil = InitiativesTool(store=_MagasinFactice([_init("i1", "x")]))

    res = await outil.execute(action="approve")

    assert res.is_error
    assert "list" in res.content


async def test_action_inconnue_liste_les_valides() -> None:
    outil = InitiativesTool(store=_MagasinFactice())

    res = await outil.execute(action="supprime-tout")

    assert res.is_error
    assert "list" in res.content and "approve" in res.content


async def test_magasin_illisible_ne_casse_pas_la_conversation() -> None:
    class _Casse:
        def load_pending_all(self, days: int = 7) -> list[object]:
            raise OSError("disque absent")

        def update_status(self, initiative_id: str, status: str) -> None: ...

    res = await InitiativesTool(store=_Casse()).execute(action="list")

    assert res.is_error
    assert "disque absent" in res.content


def test_le_schema_correspond_a_la_signature() -> None:
    """Le défaut qui rendait run_script structurellement inappelable."""
    import inspect

    props = set(InitiativesTool.input_schema["properties"])
    params = set(inspect.signature(InitiativesTool.execute).parameters) - {"self"}

    assert props <= params, f"annoncés mais absents : {props - params}"
    assert "action" in InitiativesTool.input_schema["required"]


@pytest.mark.parametrize("priorite", ["high", "medium", "low", "inconnue"])
async def test_priorite_inattendue_ne_fait_pas_tomber_le_tri(priorite: str) -> None:
    outil = InitiativesTool(store=_MagasinFactice([_init("i1", "x", priorite)]))

    assert not (await outil.execute(action="list")).is_error


async def test_seules_les_suggestions_a_arbitrer_remontent() -> None:
    """Les `notify` ont déjà été délivrées : les proposer serait une redite.

    Leur statut ne repasse jamais de « pending », si bien que le magasin en
    annonçait 36 là où 15 attendaient réellement une décision. C'est ce
    compteur trompeur qui avait fait conclure à tort que rien n'était consommé.
    """
    magasin = _MagasinFactice(
        [
            SimpleNamespace(
                id="v1", title="a arbitrer", action="x",
                priority="high", execution_mode="validate",
            ),
            SimpleNamespace(
                id="n1", title="deja delivree", action="x",
                priority="high", execution_mode="notify",
            ),
            SimpleNamespace(
                id="a1", title="sans effet", action="x",
                priority="high", execution_mode="auto",
            ),
        ]
    )
    outil = InitiativesTool(store=magasin)

    contenu = (await outil.execute(action="list")).content

    assert "1 suggestion(s)" in contenu
    assert "a arbitrer" in contenu
    assert "deja delivree" not in contenu
    assert "sans effet" not in contenu


# ── Désignation par le titre ─────────────────────────────────────────────────
#
# Le Gateway n'enchaîne QU'UN tour d'outils : le modèle ne peut pas appeler
# 'list' pour trouver l'identifiant puis 'reject' dans le même échange. Observé
# en production — « écarte celle sur le brain dump » relistait en boucle sans
# jamais trancher. Et à l'oral, personne ne dicte un identifiant.


async def test_le_titre_suffit_a_designer_une_suggestion() -> None:
    magasin = _MagasinFactice([_init("init_ab12", "Session de capture (Brain Dump)")])
    outil = InitiativesTool(store=magasin)

    res = await outil.execute(action="reject", initiative_id="brain dump")

    assert not res.is_error
    assert magasin.decisions == [("init_ab12", "rejected")], (
        "c'est le VRAI identifiant qui doit être écrit, pas les mots du titre"
    )


async def test_titre_ambigu_demande_de_preciser() -> None:
    """Trancher au hasard entre deux correspondances serait pire que refuser."""
    magasin = _MagasinFactice(
        [_init("i1", "Audit du flux de tâches"), _init("i2", "Audit de l'agenda")]
    )
    outil = InitiativesTool(store=magasin)

    res = await outil.execute(action="approve", initiative_id="audit")

    assert res.is_error
    assert magasin.decisions == []
    assert "2 suggestions" in res.content


async def test_l_identifiant_reste_prioritaire_sur_le_titre() -> None:
    magasin = _MagasinFactice([_init("i1", "alpha"), _init("i2", "beta")])
    outil = InitiativesTool(store=magasin)

    await outil.execute(action="approve", initiative_id="i2")

    assert magasin.decisions == [("i2", "approved")]
