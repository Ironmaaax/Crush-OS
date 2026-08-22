# Copyright (C) 2026 Maxime Song

"""La QUALITÉ de ce qui entre en mémoire, et de ce qui y reste.

Deux corrections, tirées d'un relevé de la base réelle (41 faits actifs, Pi,
2026-08-22) et non d'une intuition :

1. À l'écriture — la forme canonique. `prefers iris (goo goo dolls)` et `prefers
   iris goo goo dolls` coexistaient : deux faits, une idée, deux places dans un
   bloc plafonné, pour deux parenthèses.

2. À l'entretien — les variantes. `uses format audio .ogg` / `uses format ogg`,
   `uses globe terrestre interactif` / `uses globe`, `prefers titanium david
   guetta` / `prefers titanium`. Ni la fusion exacte ni `find_active_exact` ne
   les voient.

CE QUI A ÉTÉ ÉCARTÉ, ET POURQUOI C'EST TESTÉ AUSSI

Le rapprochement TRANS-catégories a été simulé sur cette même base avant d'être
rejeté : il libérait quatre places de plus, mais faisait survivre la version
`preference` de chaque paire, ce qui VIDAIT la catégorie `persona` — le bloc
perdait sa section « Comment lui parler ». `test_pas_de_glissement_entre_categories`
verrouille ce refus, sinon quelqu'un le « corrigera » un jour de bonne foi.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from crush.providers.memory.ingest import _canonise_objet, _parse_extract_response
from crush.providers.memory.kernel import MemoryKernel
from crush.providers.memory.schemas import DecayPolicy, Fact, FactStatus


@pytest.fixture
def kernel(tmp_path: Path) -> MemoryKernel:
    return MemoryKernel(tmp_path / "k.db")


def _fait(
    fid: str,
    obj: str,
    predicate: str = "uses",
    category: str = "tool",
    support: int = 1,
    confidence: float = 0.75,
    importance: float = 0.5,
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
        support_count=support,
        decay_policy=DecayPolicy.MEDIUM,
        importance=importance,
        created_at=quand,
        last_seen_at=quand,
        updated_at=quand,
    )


# ── 1. Forme canonique à l'écriture ───────────────────────────────────────────


def test_les_parentheses_disparaissent_le_contenu_reste() -> None:
    """C'est la ponctuation qui est du bruit, pas l'artiste entre parenthèses."""
    assert _canonise_objet("iris (goo goo dolls)") == "iris goo goo dolls"
    assert _canonise_objet("titanium (david guetta)") == "titanium david guetta"


def test_deux_formulations_deviennent_le_meme_objet() -> None:
    """LE cas mesuré : après canonisation, les deux libellés sont identiques,
    donc `find_active_exact` reconnaît le fait et le CONFIRME au lieu d'en créer
    un jumeau — le support monte au lieu de se diviser."""
    assert _canonise_objet("iris (goo goo dolls)") == _canonise_objet("iris goo goo dolls")


def test_un_objet_vide_apres_canonisation_est_rejete() -> None:
    """Un objet réduit à « () » n'est pas un fait. Sans ce filtre, il entrait en
    base avec un libellé vide et occupait une place invisible."""
    brut = (
        '{"facts": [{"subject": "max", "predicate": "uses", "object": "()", '
        '"category": "tool", "confidence_source": "explicit", "importance": 0.5}]}'
    )
    assert _parse_extract_response(brut) == []


def test_la_canonisation_sapplique_a_lextraction() -> None:
    """Le prompt DEMANDE une forme canonique, mais un prompt est une consigne,
    pas une garantie. Le filtre doit tenir même si le modèle l'ignore."""
    brut = (
        '{"facts": [{"subject": "max", "predicate": "prefers", '
        '"object": "iris (goo goo dolls)", "category": "preference", '
        '"confidence_source": "explicit", "importance": 0.3}]}'
    )
    cands = _parse_extract_response(brut)
    assert len(cands) == 1
    assert cands[0].object == "iris goo goo dolls"


# ── 2. Absorption des variantes ───────────────────────────────────────────────


def test_une_reformulation_est_absorbee(kernel: MemoryKernel) -> None:
    """`format audio .ogg` et `format ogg` : le point et le mot en trop ne font
    pas deux faits."""
    kernel.insert_fact(_fait("f1", "format audio .ogg"))
    kernel.insert_fact(_fait("f2", "format ogg"))

    assert kernel.fusionner_variantes() == 1
    actifs = kernel.list_facts_by_status(FactStatus.ACTIVE)
    assert len(actifs) == 1
    # Le libellé le plus long survit : « globe » seul ne se comprend plus dans
    # six mois. La brièveté est demandée à l'extraction, pas payée en information.
    assert actifs[0].object == "format audio .ogg"
    assert actifs[0].support_count == 2


def test_le_plus_observe_gagne_meme_sil_est_plus_court(kernel: MemoryKernel) -> None:
    """La longueur ne départage QU'À égalité d'observations. Ce qui a été vu
    quatre fois est la formulation que Max emploie vraiment."""
    kernel.insert_fact(_fait("court", "globe", support=4))
    kernel.insert_fact(_fait("long", "globe terrestre interactif", support=1))

    assert kernel.fusionner_variantes() == 1
    actifs = kernel.list_facts_by_status(FactStatus.ACTIVE)
    assert actifs[0].object == "globe"
    assert actifs[0].support_count == 5


def test_un_ecart_trop_grand_nest_pas_une_variante(kernel: MemoryKernel) -> None:
    """LE garde-fou. Sans plafond d'écart, « jazz » absorberait tout fait
    commençant par jazz — l'heuristique cesserait de rapprocher des
    reformulations pour rapprocher des notions voisines."""
    kernel.insert_fact(_fait("a", "jazz", predicate="prefers", category="preference"))
    kernel.insert_fact(
        _fait(
            "b",
            "jazz manouche des annees trente",
            predicate="prefers",
            category="preference",
        )
    )

    assert kernel.fusionner_variantes() == 0
    assert len(kernel.list_facts_by_status(FactStatus.ACTIVE)) == 2


def test_pas_de_glissement_entre_categories(kernel: MemoryKernel) -> None:
    """REFUS VERROUILLÉ. Rapprocher `uses iris` [tool] de `prefers iris goo goo
    dolls` [preference] libère une place, mais la version `preference` survit et
    la catégorie d'origine se vide. Simulé sur la base réelle : `persona`
    disparaissait entièrement du bloc, qui perdait « Comment lui parler ».

    Le savoir n'était pas perdu — la LISIBILITÉ l'était."""
    kernel.insert_fact(_fait("t", "iris", predicate="uses", category="tool"))
    kernel.insert_fact(
        _fait("p", "iris goo goo dolls", predicate="prefers", category="preference")
    )

    assert kernel.fusionner_variantes() == 0
    assert len(kernel.list_facts_by_status(FactStatus.ACTIVE)) == 2


def test_pas_de_glissement_entre_predicats(kernel: MemoryKernel) -> None:
    """Même refus pour deux prédicats d'une même catégorie : `prefers concision`
    et `communicates_as concision` disent la même chose, mais l'un est un goût
    et l'autre une façon de parler. Les confondre est un choix éditorial, pas un
    nettoyage."""
    kernel.insert_fact(_fait("a", "concision", predicate="prefers", category="persona"))
    kernel.insert_fact(
        _fait("b", "concision totale", predicate="communicates_as", category="persona")
    )

    assert kernel.fusionner_variantes() == 0


def test_labsorption_est_idempotente(kernel: MemoryKernel) -> None:
    """La passe tourne chaque nuit. Une base déjà propre ne doit rien changer —
    sinon le support enflerait à chaque nuit, et c'est la clé qui désigne le
    survivant."""
    kernel.insert_fact(_fait("f1", "format audio .ogg"))
    kernel.insert_fact(_fait("f2", "format ogg"))

    assert kernel.fusionner_variantes() == 1
    total = kernel.list_facts_by_status(FactStatus.ACTIVE)[0].support_count

    assert kernel.fusionner_variantes() == 0
    assert kernel.list_facts_by_status(FactStatus.ACTIVE)[0].support_count == total


def test_trois_variantes_se_rangent_sous_une_seule(kernel: MemoryKernel) -> None:
    kernel.insert_fact(_fait("a", "globe terrestre interactif"))
    kernel.insert_fact(_fait("b", "globe terrestre"))
    kernel.insert_fact(_fait("c", "globe"))

    assert kernel.fusionner_variantes() == 2
    actifs = kernel.list_facts_by_status(FactStatus.ACTIVE)
    assert len(actifs) == 1
    assert actifs[0].support_count == 3


def test_une_variante_absorbee_reste_verifiable(kernel: MemoryKernel) -> None:
    """On n'efface rien : la variante passe en SUPERSEDED et reste reliée au
    survivant. C'est ce qui rend l'heuristique acceptable — une erreur de
    rapprochement est réversible et visible."""
    kernel.insert_fact(_fait("f1", "format audio .ogg"))
    kernel.insert_fact(_fait("f2", "format ogg"))
    kernel.fusionner_variantes()

    survivant = kernel.list_facts_by_status(FactStatus.ACTIVE)[0]
    archives = kernel.list_facts_by_status(FactStatus.SUPERSEDED)
    assert len(archives) == 1
    assert [r.to_fact_id for r in kernel.list_relations(survivant.id)] == [archives[0].id]
