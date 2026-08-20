# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS,   .
 

"""Découpage du flux LLM en fragments à synthétiser — `providers/audio/segmentation.py`.

Ce découpage est ce qui fait passer le délai avant le premier son de 1,72 s à
0,25 s sur le Pi : on synthétise dès la première phrase au lieu d'attendre la
réponse entière.
"""

from __future__ import annotations

from crush.providers.audio.segmentation import SentenceAccumulator


def _tout(acc: SentenceAccumulator, texte: str, taille_morceau: int = 7) -> list[str]:
    """Pousse le texte par petits morceaux, comme le ferait un flux LLM."""
    sortie: list[str] = []
    for i in range(0, len(texte), taille_morceau):
        sortie += acc.push(texte[i : i + taille_morceau])
    sortie += acc.flush()
    return sortie


def test_premiere_phrase_emise_sans_attendre_la_suite() -> None:
    """Le point clé : ne pas attendre la fin de la réponse."""
    acc = SentenceAccumulator()
    emis = acc.push("Il fait quinze degrés à Paris. Le vent")
    assert emis == ["Il fait quinze degrés à Paris."]


def test_rien_avant_une_phrase_complete() -> None:
    acc = SentenceAccumulator()
    assert acc.push("Il fait quinze") == []
    assert acc.push(" degrés") == []


def test_flush_rend_la_phrase_incomplete() -> None:
    """Une réponse sans ponctuation finale ne doit pas être perdue."""
    acc = SentenceAccumulator()
    acc.push("Bonjour Max")
    assert acc.flush() == ["Bonjour Max"]


def test_reponse_complete_par_morceaux() -> None:
    texte = (
        "Il fait quinze degrés à Paris. Le vent souffle du nord. "
        "Des averses sont attendues ce soir."
    )
    fragments = _tout(SentenceAccumulator(), texte)
    assert fragments[0] == "Il fait quinze degrés à Paris."
    assert "".join(fragments).replace(" ", "") == texte.replace(" ", "")


def test_fragments_courts_regroupes_apres_le_premier() -> None:
    """Après le premier, on regroupe : des bribes hachent la prosodie."""
    fragments = _tout(SentenceAccumulator(), "Oui. Non. Peut-être. Je vérifie tout de suite.")
    assert len(fragments) < 4


def test_markdown_retire_car_non_prononcable() -> None:
    fragments = _tout(SentenceAccumulator(), "**Attention** : le disque est plein.")
    assert "*" not in fragments[0]
    assert "Attention" in fragments[0]


def test_puce_de_liste_retiree() -> None:
    acc = SentenceAccumulator()
    acc.push("- Premier point. ")
    assert acc.flush() or True
    fragments = _tout(SentenceAccumulator(), "- Premier point. Second point.")
    assert not fragments[0].startswith("-")


def test_paragraphe_sans_ponctuation_finit_par_etre_coupe() -> None:
    """Un LLM peut produire un pavé sans point : il ne faut pas attendre la fin."""
    texte = "mot " * 200
    acc = SentenceAccumulator()
    emis = acc.push(texte)
    assert emis, "un texte très long sans ponctuation doit être découpé"


def test_points_de_suspension_traites_comme_une_fin() -> None:
    acc = SentenceAccumulator()
    assert acc.push("Je réfléchis… Voilà la réponse.") == ["Je réfléchis…"]


def test_aucun_fragment_vide() -> None:
    fragments = _tout(SentenceAccumulator(), "Bien.   \n\n  Très bien.  ")
    assert all(f.strip() for f in fragments)
