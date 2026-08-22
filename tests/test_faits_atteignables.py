# Copyright (C) 2026 Maxime Song

"""Les faits hors du bloc sont-ils ATTEIGNABLES en conversation ?

LE TROU QUE CE CHANTIER FERME

Mesuré le 22/08/2026 : 37 faits actifs, 22 places dans le bloc injecté au
prompt. Les 15 autres n'étaient pas mal classés, ils étaient introuvables —
vérifié chemin par chemin : `memory_search` interroge l'index vectoriel,
alimenté par les topics et les transcriptions et jamais par les faits ;
`session_recall` lit ce même index ; `memory_journal` exige une fenêtre de
dates ; `MemoryRetrieval` n'est instancié nulle part.

CE QUE CES TESTS VÉRIFIENT, ET CE QU'ILS NE PEUVENT PAS VÉRIFIER

Ils vérifient le CÂBLAGE et la forme du texte indexé, sans charger fastembed :
un modèle d'embedding de 470 Mo n'a pas sa place dans une suite unitaire, et son
classement n'est pas déterministe d'une version à l'autre. Le double d'index
enregistre ce qu'on lui donne — donc ce que le modèle aurait à encoder.

La qualité de la recherche elle-même se mesure sur la vraie base, avec le vrai
modèle. C'est fait à la main au déploiement, et rapporté.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from crush.providers.memory.fact_index import SOURCE, FactIndex, texte_indexable
from crush.providers.memory.kernel import MemoryKernel
from crush.providers.memory.schemas import DecayPolicy, Fact, FactStatus


class _IndexEspion:
    """Double de `VectorIndex` : retient ce qu'on lui donne à encoder."""

    def __init__(self) -> None:
        self.documents: dict[str, dict] = {}
        self.retraits: list[str] = []
        self.persiste = 0

    async def add(self, doc_id: str, text: str, metadata: dict | None = None) -> None:
        self.documents[doc_id] = {"text": text, "metadata": metadata or {}}

    async def search(self, query: str, k: int = 5, source: str | None = None) -> list[dict]:
        return []

    async def remove_source(self, source: str) -> int:
        self.retraits.append(source)
        avant = len(self.documents)
        self.documents = {
            d: v
            for d, v in self.documents.items()
            if (v["metadata"] or {}).get("source") != source
        }
        return avant - len(self.documents)

    async def persist(self) -> None:
        self.persiste += 1

    def is_empty(self) -> bool:
        return not self.documents


@pytest.fixture
def kernel(tmp_path: Path) -> MemoryKernel:
    return MemoryKernel(tmp_path / "k.db")


def _fait(
    fid: str,
    predicate: str,
    obj: str,
    category: str,
    status: FactStatus = FactStatus.ACTIVE,
) -> Fact:
    quand = datetime.now()
    return Fact(
        id=fid,
        subject="max",
        predicate=predicate,
        object=obj,
        category=category,
        status=status,
        confidence=0.75,
        support_count=1,
        decay_policy=DecayPolicy.MEDIUM,
        importance=0.5,
        created_at=quand,
        last_seen_at=quand,
        updated_at=quand,
    )


# ── 1. Le texte encodé ────────────────────────────────────────────────────────


def test_un_fait_devient_une_phrase_francaise() -> None:
    """LE point du chantier. Un embedding encode du langage, pas un schéma :
    `max communicates_as monsieur` est une suite de jetons dont deux ne sont pas
    des mots. La question « comment tu me parles » ne peut se rapprocher que
    d'une phrase."""
    texte = texte_indexable(_fait("f", "communicates_as", "monsieur", "persona"))
    assert "monsieur" in texte
    assert "communicates_as" not in texte
    assert texte[0].isupper() and texte.rstrip().endswith(")")


def test_le_genre_de_linformation_est_dans_le_texte() -> None:
    """Une question porte souvent sur le GENRE (« quelles sont mes contraintes »)
    autant que sur le contenu. Sans le mot « contrainte » dans le texte encodé,
    aucun rapprochement n'est possible."""
    texte = texte_indexable(_fait("f", "struggles_with", "perception du temps", "constraint"))
    assert "contrainte" in texte


def test_un_predicat_inconnu_est_indexe_quand_meme() -> None:
    """Le vocabulaire est fermé, mais la base contient des vestiges d'avant sa
    fermeture. Un fait sans phrase toute faite doit rester TROUVABLE plutôt que
    de disparaître silencieusement de l'index."""
    texte = texte_indexable(_fait("f", "predicat_inedit", "quelque chose", "preference"))
    assert "quelque chose" in texte
    assert texte.strip() != ""


def test_le_prenom_configure_est_employe() -> None:
    texte = texte_indexable(_fait("f", "prefers", "café", "preference"), prenom="Camille")
    assert texte.startswith("Camille")


# ── 2. La synchronisation ─────────────────────────────────────────────────────


async def test_tous_les_faits_actifs_sont_indexes(kernel: MemoryKernel) -> None:
    kernel.insert_fact(_fait("a", "prefers", "concision", "preference"))
    kernel.insert_fact(_fait("b", "uses", "spotify", "tool"))
    espion = _IndexEspion()

    n = await FactIndex(kernel, espion).synchroniser()  # type: ignore[arg-type]

    assert n == 2
    assert len(espion.documents) == 2
    assert espion.persiste == 1


async def test_un_fait_archive_nest_pas_indexe(kernel: MemoryKernel) -> None:
    """Sinon la recherche ramènerait une croyance corrigée ou un doublon absorbé
    la nuit précédente, sans rien qui le distingue d'un fait courant."""
    kernel.insert_fact(_fait("actif", "prefers", "thé", "preference"))
    kernel.insert_fact(
        _fait("vieux", "prefers", "café", "preference", status=FactStatus.SUPERSEDED)
    )
    espion = _IndexEspion()

    await FactIndex(kernel, espion).synchroniser()  # type: ignore[arg-type]

    textes = " ".join(d["text"] for d in espion.documents.values())
    assert "thé" in textes
    assert "café" not in textes


async def test_le_retrait_precede_lajout(kernel: MemoryKernel) -> None:
    """L'ORDRE est le point : indexer avant de retirer laisserait trouvables les
    faits qui viennent d'être archivés."""
    kernel.insert_fact(_fait("a", "prefers", "concision", "preference"))
    espion = _IndexEspion()

    await FactIndex(kernel, espion).synchroniser()  # type: ignore[arg-type]

    assert espion.retraits == [SOURCE]


async def test_la_synchronisation_est_idempotente(kernel: MemoryKernel) -> None:
    """Elle tourne chaque nuit ET à chaque démarrage. Deux passes ne doivent pas
    laisser deux exemplaires de chaque fait."""
    kernel.insert_fact(_fait("a", "prefers", "concision", "preference"))
    espion = _IndexEspion()
    index = FactIndex(kernel, espion)  # type: ignore[arg-type]

    await index.synchroniser()
    await index.synchroniser()

    assert len(espion.documents) == 1


async def test_un_fait_disparu_de_la_base_disparait_de_lindex(kernel: MemoryKernel) -> None:
    """Le cas que le suivi incrémental ratait : un fait absorbé par une fusion
    n'est plus ACTIVE, donc la resynchronisation en bloc le retire."""
    kernel.insert_fact(_fait("a", "prefers", "concision", "preference"))
    kernel.insert_fact(_fait("b", "uses", "globe", "tool"))
    espion = _IndexEspion()
    index = FactIndex(kernel, espion)  # type: ignore[arg-type]
    await index.synchroniser()
    assert len(espion.documents) == 2

    archive = kernel.get_fact("b")
    assert archive is not None
    archive.status = FactStatus.SUPERSEDED
    kernel.update_fact(archive)
    await index.synchroniser()

    assert len(espion.documents) == 1
    assert "globe" not in " ".join(d["text"] for d in espion.documents.values())


async def test_les_metadonnees_marquent_la_provenance(kernel: MemoryKernel) -> None:
    """C'est la marque qui permet de retirer en bloc les faits sans toucher aux
    topics ni aux transcriptions qui partagent l'index."""
    kernel.insert_fact(_fait("a", "prefers", "concision", "preference"))
    espion = _IndexEspion()

    await FactIndex(kernel, espion).synchroniser()  # type: ignore[arg-type]

    meta = next(iter(espion.documents.values()))["metadata"]
    assert meta["source"] == SOURCE
    assert meta["fact_id"] == "a"
    assert meta["category"] == "preference"


async def test_une_base_vide_ne_casse_rien(kernel: MemoryKernel) -> None:
    espion = _IndexEspion()
    assert await FactIndex(kernel, espion).synchroniser() == 0  # type: ignore[arg-type]
