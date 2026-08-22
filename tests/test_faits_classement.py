# Copyright (C) 2026 Maxime Song

"""Le CLASSEMENT du bloc mémoire : qui obtient une des 22 places.

CE QUI A ÉTÉ MESURÉ SUR LA BASE RÉELLE (41 faits actifs, Pi, 2026-08-22)

Le tri était plat : `importance × confiance`, puis coupe au plafond. Résultat
observé, en tête des faits ÉCARTÉS :

    0.33 [persona] communicates_as monsieur

Autrement dit, la façon dont Max veut qu'on s'adresse à lui — qui pèse sur
CHAQUE réponse — perdait sa place au profit de faits d'outil mieux notés. La
cause est une distribution déséquilibrée : 15 `preference` et 13 `tool` pour 2
`identity` et 3 `persona`. Une catégorie nombreuse monopolise les places, quelle
que soit son utilité.

POURQUOI UN QUOTA ET NON UN PALIER PAR CATÉGORIE

Première tentative, simulée avant d'être écrite : deux paliers, les catégories
« qui pèsent sur chaque réponse » d'abord. Le bloc obtenu était PIRE — il faisait
entrer quinze goûts musicaux individuels (« prefers creep radiohead », « prefers
titanium ») en évinçant `requires_validation_for déploiement fonctionnalité`,
une règle de fonctionnement. Un palier ne règle pas le déséquilibre, il le
déplace.

Le quota borne chaque catégorie à un premier tour, puis remplit au mérite. Les
petites catégories décisives sont garanties, les grosses restent représentées
sans écraser le reste.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from crush.engine.faits import bloc_memoire
from crush.providers.memory.kernel import MemoryKernel
from crush.providers.memory.schemas import DecayPolicy, Fact, FactStatus


@pytest.fixture
def kernel(tmp_path: Path) -> MemoryKernel:
    return MemoryKernel(tmp_path / "k.db")


def _fait(
    fid: str,
    predicate: str,
    obj: str,
    category: str,
    confidence: float = 0.8,
    importance: float = 0.6,
) -> Fact:
    quand = datetime.now()
    return Fact(
        id=fid,
        subject="max",
        predicate=predicate,
        object=obj,
        category=category,
        status=FactStatus.ACTIVE,
        confidence=confidence,
        support_count=1,
        decay_policy=DecayPolicy.MEDIUM,
        importance=importance,
        created_at=quand,
        last_seen_at=quand,
        updated_at=quand,
    )


def _base_desequilibree(kernel: MemoryKernel) -> None:
    """Reproduit la forme de la base réelle : une catégorie nombreuse et bien
    notée, une catégorie rare et mal notée mais décisive."""
    for i in range(15):
        kernel.insert_fact(
            _fait(f"pref{i}", "prefers", f"chanson-{i}", "preference", 0.9, 0.7)
        )
    # Mal noté — c'est exactement le cas réel : l'extraction donne une importance
    # faible à un fait de persona, alors qu'il s'applique à toutes les réponses.
    kernel.insert_fact(_fait("p1", "communicates_as", "monsieur", "persona", 0.6, 0.55))
    kernel.insert_fact(
        _fait("d1", "requires_validation_for", "déploiement", "decision", 0.7, 0.5)
    )


def test_un_fait_de_persona_mal_note_garde_sa_place(kernel: MemoryKernel) -> None:
    """LE cas mesuré. Avec un tri plat, « communicates_as monsieur » (0,33) sort
    derrière quinze préférences à 0,63 et n'atteint jamais le prompt."""
    _base_desequilibree(kernel)
    bloc = bloc_memoire(kernel, plafond=10)
    assert "monsieur" in bloc


def test_une_regle_de_fonctionnement_garde_sa_place(kernel: MemoryKernel) -> None:
    """Une décision qui conditionne les actions de l'assistant vaut mieux qu'une
    quinzième chanson, même moins bien notée."""
    _base_desequilibree(kernel)
    bloc = bloc_memoire(kernel, plafond=10)
    assert "déploiement" in bloc


def test_une_categorie_nombreuse_ne_monopolise_pas(kernel: MemoryKernel) -> None:
    """Sans quota, les 15 préférences prenaient 15 des places disponibles."""
    _base_desequilibree(kernel)
    bloc = bloc_memoire(kernel, plafond=10, quota=3)
    assert bloc.count("chanson-") <= 8, bloc


def test_les_places_restantes_vont_au_merite(kernel: MemoryKernel) -> None:
    """Le quota est un plancher garanti, pas un plafond : quand il reste des
    places après le premier tour, elles vont aux mieux notés. Sinon un bloc
    plafonné à 22 se contenterait de 3 faits par catégorie et gâcherait le reste."""
    _base_desequilibree(kernel)
    bloc = bloc_memoire(kernel, plafond=17, quota=3)
    # 17 places, 17 faits en base : tout doit entrer.
    assert bloc.count("chanson-") == 15


def test_le_quota_ne_gonfle_pas_le_bloc(kernel: MemoryKernel) -> None:
    """Le plafond reste le plafond. Le quota redistribue les places, il n'en
    ajoute aucune — sinon le coût en jetons dériverait silencieusement."""
    _base_desequilibree(kernel)
    bloc = bloc_memoire(kernel, plafond=8, quota=4)
    lignes = [ligne for ligne in bloc.splitlines() if ligne.startswith("- ")]
    assert len(lignes) == 8


def test_base_homogene_inchangee(kernel: MemoryKernel) -> None:
    """Garde-fou : quand une seule catégorie existe, le quota ne doit rien
    changer au classement par mérite."""
    for i in range(6):
        kernel.insert_fact(
            _fait(f"t{i}", "uses", f"outil-{i}", "tool", 0.9, 0.9 - i * 0.1)
        )
    bloc = bloc_memoire(kernel, plafond=3, quota=2)
    assert "outil-0" in bloc and "outil-1" in bloc and "outil-2" in bloc
    assert "outil-3" not in bloc
