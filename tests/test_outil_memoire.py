# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS,   .
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Outil `memory_write` : création d'un topic, confinement, chaînage.

L'audit de production a montré un outil d'écriture qui refusait de créer un
fichier : inutilisable pour son cas d'usage principal, noter quelque chose de
nouveau. Ces tests verrouillent la création ET le confinement au répertoire
des topics, qui est la raison d'être de la validation du nom.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crush.capabilities.tools.base import ToolResult
from crush.capabilities.tools.memory import (
    MemoryLoadTopicTool,
    MemorySearchTool,
    MemoryTopicWriteTool,
)
from crush.providers.memory.topics import TopicStore

# ── Doubles ───────────────────────────────────────────────────


class _FakeVectorIndex:
    """Index vectoriel en mémoire : recherche par sous-chaîne, sans embeddings."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.persist_calls = 0

    async def add(self, doc_id: str, text: str, metadata: dict | None = None) -> None:
        self.docs[doc_id] = {"text": text, "metadata": metadata or {}}

    async def search(self, query: str, k: int = 5) -> list[dict]:
        hits = [
            {"doc_id": d, "text": v["text"], "metadata": v["metadata"], "score": 1.0}
            for d, v in self.docs.items()
            if query.lower() in v["text"].lower()
        ]
        return hits[:k]

    async def persist(self) -> None:
        self.persist_calls += 1

    def is_empty(self) -> bool:
        return not self.docs


class _IndexEnPanne(_FakeVectorIndex):
    """Reproduit une indexation qui casse (modèle d'embedding indisponible)."""

    async def add(self, doc_id: str, text: str, metadata: dict | None = None) -> None:
        raise RuntimeError("fastembed indisponible")


@pytest.fixture
def topics_dir(tmp_path: Path) -> Path:
    return tmp_path / "memory" / "topics"


@pytest.fixture
def index() -> _FakeVectorIndex:
    return _FakeVectorIndex()


def _outil(topics_dir: Path, index: object | None = None) -> MemoryTopicWriteTool:
    return MemoryTopicWriteTool(topics_dir=topics_dir, vector_index=index)  # type: ignore[arg-type]


# ── Création ──────────────────────────────────────────────────


async def test_ecrit_un_fichier_inexistant(topics_dir: Path, index: _FakeVectorIndex) -> None:
    """Le défaut d'origine : l'appel de l'audit doit maintenant réussir."""
    result = await _outil(topics_dir, index).execute(
        filename="sonde_audit.md", content="# Sonde\nOK."
    )

    assert not result.is_error, result.content
    assert (topics_dir / "sonde_audit.md").read_text(encoding="utf-8") == "# Sonde\nOK."
    assert "créée" in result.content


async def test_cree_le_repertoire_des_topics_absent(topics_dir: Path) -> None:
    """Premier déploiement : aucun répertoire mémoire n'existe encore."""
    assert not topics_dir.exists()

    result = await _outil(topics_dir).execute(filename="premier.md", content="x")

    assert not result.is_error, result.content
    assert (topics_dir / "premier.md").exists()


async def test_distingue_creation_et_mise_a_jour(topics_dir: Path) -> None:
    outil = _outil(topics_dir)
    creation = await outil.execute(filename="notes.md", content="v1")
    maj = await outil.execute(filename="notes.md", content="v2")

    assert "créée" in creation.content
    assert "mise à jour" in maj.content
    assert (topics_dir / "notes.md").read_text(encoding="utf-8") == "v2"


async def test_mode_append_conserve_l_existant(topics_dir: Path) -> None:
    outil = _outil(topics_dir)
    await outil.execute(filename="notes.md", content="Ligne 1")
    result = await outil.execute(filename="notes.md", content="Ligne 2", mode="append")

    contenu = (topics_dir / "notes.md").read_text(encoding="utf-8")
    assert not result.is_error, result.content
    assert "Ligne 1" in contenu
    assert "Ligne 2" in contenu


async def test_mode_append_sur_fichier_neuf(topics_dir: Path) -> None:
    result = await _outil(topics_dir).execute(
        filename="neuf.md", content="Première note", mode="append"
    )

    assert not result.is_error, result.content
    assert (topics_dir / "neuf.md").read_text(encoding="utf-8") == "Première note"


async def test_mode_inconnu_indique_les_valeurs_acceptees(topics_dir: Path) -> None:
    result = await _outil(topics_dir).execute(filename="x.md", content="a", mode="ajouter")

    assert result.is_error
    assert "replace" in result.content and "append" in result.content
    assert not (topics_dir / "x.md").exists()


# ── Confinement ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "nom",
    [
        "../evasion.md",
        "..\\evasion.md",
        "sous/dossier.md",
        "sous\\dossier.md",
        "/etc/passwd.md",
        "C:/Windows/evasion.md",
        "C:evasion.md",  # relatif au lecteur : anodin sous Linux, chemin sous Windows
        "~/evasion.md",
        ".cache.md",
        "sans_extension",
        "evasion.MD",  # glob('*.md') est sensible à la casse sous Linux
        "",
        "   ",
    ],
)
async def test_refuse_les_noms_dangereux(topics_dir: Path, nom: str) -> None:
    topics_dir.mkdir(parents=True)
    result = await _outil(topics_dir).execute(filename=nom, content="charge utile")

    assert result.is_error, f"Nom accepté à tort : {nom!r}"
    assert list(topics_dir.rglob("*")) == [], f"Fichier écrit hors topics via {nom!r}"


async def test_aucune_ecriture_hors_du_repertoire(tmp_path: Path) -> None:
    """Le contenu ne doit jamais atterrir chez le voisin, quel que soit le nom."""
    topics = tmp_path / "topics"
    topics.mkdir()
    cible = tmp_path / "voisin.md"
    cible.write_text("intact", encoding="utf-8")

    result = await _outil(topics).execute(filename="../voisin.md", content="écrasé")

    assert result.is_error
    assert cible.read_text(encoding="utf-8") == "intact"


async def test_refuse_d_ecraser_un_repertoire(topics_dir: Path) -> None:
    topics_dir.mkdir(parents=True)
    (topics_dir / "piege.md").mkdir()

    result = await _outil(topics_dir).execute(filename="piege.md", content="x")

    assert result.is_error
    assert (topics_dir / "piege.md").is_dir()


# ── Chaînage avec les autres outils mémoire ───────────────────


async def test_memory_load_topic_lit_un_fichier_fraichement_cree(topics_dir: Path) -> None:
    await _outil(topics_dir).execute(filename="sonde_audit.md", content="Contenu de sonde")

    lecture = MemoryLoadTopicTool(topic_store=TopicStore(topics_dir))
    result = await lecture.execute(filename="sonde_audit.md")

    assert not result.is_error, result.content
    assert "Contenu de sonde" in result.content


async def test_memory_search_trouve_un_fichier_fraichement_cree(
    topics_dir: Path, index: _FakeVectorIndex
) -> None:
    await _outil(topics_dir, index).execute(
        filename="sonde_audit.md", content="Le DAC PCM5102A est branché sur la Pi"
    )

    recherche = MemorySearchTool(vector_index=index)  # type: ignore[arg-type]
    result = await recherche.execute(query="PCM5102A", k=3)

    assert not result.is_error, result.content
    assert "sonde_audit.md" in result.content
    assert index.persist_calls == 1


async def test_memory_load_topic_oriente_vers_la_creation(topics_dir: Path) -> None:
    topics_dir.mkdir(parents=True)
    lecture = MemoryLoadTopicTool(topic_store=TopicStore(topics_dir))

    result = await lecture.execute(filename="inconnu.md")

    assert result.is_error
    assert "memory_write" in result.content


# ── Robustesse de l'indexation ────────────────────────────────


async def test_echec_d_indexation_ne_perd_pas_l_ecriture(topics_dir: Path) -> None:
    """Le fichier est sur le disque : le dire, sinon le modèle réécrit en boucle."""
    result: ToolResult = await _outil(topics_dir, _IndexEnPanne()).execute(
        filename="sonde_audit.md", content="Contenu précieux"
    )

    assert not result.is_error, result.content
    assert (topics_dir / "sonde_audit.md").read_text(encoding="utf-8") == "Contenu précieux"
    assert "memory_load_topic" in result.content
