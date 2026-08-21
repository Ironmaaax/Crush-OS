# Copyright (C) 2026 Maxime Song

"""Le coffre exposé par l'API — la mémoire lisible et corrigeable depuis un navigateur.

La page Atelier/Mémoire répondait déjà à « qu'est-ce que Crush sait ? » sous forme
de tableau. Elle ne répondait pas à « où ce souvenir vit-il, et à côté de quoi ? »,
qui est la question qu'on se pose en relisant sa propre mémoire.

Ce qui est vérifié ici, dans l'ordre de ce qui coûterait le plus cher :

1. la page web et Obsidian montrent la MÊME répartition — sinon on cherche dans
   l'une ce qu'on a vu dans l'autre, sans savoir laquelle a tort ;
2. les faits arrivent avec leur ancre, sans quoi rien n'est corrigeable ;
3. l'état vient du Kernel et non des fichiers .md, qui datent de la nuit ;
4. « retenir » passe par l'extraction et jamais par une écriture directe.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from crush.interfaces.api.memory import router
from crush.kernel.schemas import FactStatus, ResultatBoiteReception
from crush.providers.memory.boite_reception import NOM_FICHIER, BoiteReception
from crush.providers.memory.kernel import MemoryKernel
from crush.providers.memory.mirror import MemoryMirror
from crush.providers.memory.schemas import DecayPolicy, Fact


def _fait(
    fid: str,
    obj: str = "café",
    category: str = "preference",
    importance: float = 0.6,
    confidence: float = 0.8,
) -> Fact:
    now = datetime.now()
    return Fact(
        id=fid,
        subject="max",
        predicate="prefers",
        object=obj,
        category=category,
        status=FactStatus.ACTIVE,
        confidence=confidence,
        support_count=1,
        decay_policy=DecayPolicy.MEDIUM,
        importance=importance,
        created_at=now,
        last_seen_at=now,
        updated_at=now,
    )


class _IngestFactice:
    def __init__(self, nouveaux: int = 1) -> None:
        self.appels: list[str] = []
        self._nouveaux = nouveaux

    async def ingest(self, content: str, **_: str) -> object:
        self.appels.append(content)
        faits = [_fait(f"fact_n{i}", obj=f"objet {i}") for i in range(self._nouveaux)]
        return type("Rendu", (), {"new_facts": faits, "confirmed": []})()


@pytest.fixture
def kernel(tmp_path: Path) -> MemoryKernel:
    k = MemoryKernel(tmp_path / "k.db")
    k.insert_fact(_fait("fact_00c2b7c5e5", obj="café", category="preference", importance=0.9))
    k.insert_fact(_fait("fact_11e964da9c", obj="thé", category="preference", importance=0.2))
    k.insert_fact(_fait("fact_beefbeef", obj="marathon", category="goal"))
    return k


@pytest.fixture
def app_et_client(tmp_path: Path, kernel: MemoryKernel) -> tuple[FastAPI, TestClient, Any]:
    mirror = MemoryMirror(kernel, tmp_path / "mirror")
    ingest = _IngestFactice()
    app = FastAPI()
    app.include_router(router)
    app.state.memory_kernel = kernel
    app.state.memory_mirror = mirror
    app.state.memory_ingest = ingest
    app.state.boite_memoire = BoiteReception(kernel, mirror, ingest=ingest)
    return app, TestClient(app), ingest


@pytest.fixture
def client(app_et_client: tuple[FastAPI, TestClient, Any]) -> TestClient:
    return app_et_client[1]


# ── 1. La même répartition que le miroir ──────────────────────────────────────


def test_le_coffre_montre_exactement_les_documents_du_miroir(
    client: TestClient, kernel: MemoryKernel, tmp_path: Path
) -> None:
    """LE test qui empêche les deux vues de se contredire.

    Si l'API recalculait la répartition de son côté, une catégorie ajoutée un
    jour dans `_CATEGORY_TO_FILE` déplacerait un fait dans Obsidian sans le
    déplacer ici, et rien ne dirait laquelle des deux vues a raison.
    """
    mirror = MemoryMirror(kernel, tmp_path / "mirror")
    attendu = {d.fichier for d in mirror.grouper()}

    vu = {d["fichier"] for d in client.get("/api/memory/coffre").json()["documents"]}

    assert vu == attendu
    assert "user/preferences.md" in vu


def test_les_faits_sont_ordonnes_comme_dans_le_document_markdown(client: TestClient) -> None:
    """Même ordre des deux côtés, sinon on cherche à la ligne 3 ce qui est ligne 12."""
    docs = client.get("/api/memory/coffre").json()["documents"]
    prefs = next(d for d in docs if d["fichier"] == "user/preferences.md")

    # importance × confidence décroissant : le café (0.9) avant le thé (0.2).
    assert [f["object"] for f in prefs["faits"]] == ["café", "thé"]


def test_chaque_document_porte_son_dossier(client: TestClient) -> None:
    """L'explorateur les groupe par dossier, comme le volet latéral d'Obsidian."""
    docs = client.get("/api/memory/coffre").json()["documents"]

    assert {d["dossier"] for d in docs} == {"user"}


# ── 2. Sans ancre, rien n'est corrigeable ─────────────────────────────────────


def test_chaque_fait_arrive_avec_son_ancre_obsidian(client: TestClient) -> None:
    docs = client.get("/api/memory/coffre").json()["documents"]
    faits = [f for d in docs for f in d["faits"]]

    assert faits, "aucun fait exposé"
    for f in faits:
        assert f["ancre"] == f["id"].replace("_", "-")
        assert "_" not in f["ancre"], "un `_` rendrait l'ancre inerte dans Obsidian"


def test_le_total_compte_les_faits_de_tous_les_documents(client: TestClient) -> None:
    corps = client.get("/api/memory/coffre").json()

    assert corps["total_faits"] == sum(len(d["faits"]) for d in corps["documents"]) == 3


# ── 3. L'état vient du Kernel, pas des fichiers de la nuit ────────────────────


def test_une_correction_est_visible_immediatement(
    client: TestClient, kernel: MemoryKernel
) -> None:
    """Le cas qui décide de la source de lecture.

    Les .md ne sont réécrits qu'à la passe nocturne. Les lire ici afficherait
    l'état d'hier matin : on corrigerait, rien ne changerait à l'écran, et on
    recorrigerait — en croyant que la fonction est cassée.
    """
    r = client.post(
        "/api/memory/correct",
        json={"target_fact_id": "fact_00c2b7c5e5", "new_object": "thé vert"},
    )
    assert r.status_code == 200 and r.json()["fact_found"]

    docs = client.get("/api/memory/coffre").json()["documents"]
    objets = [f["object"] for d in docs for f in d["faits"]]

    assert "thé vert" in objets
    assert "café" not in objets


def test_un_fait_oublie_disparait_du_coffre_sans_quitter_la_base(
    client: TestClient, kernel: MemoryKernel
) -> None:
    client.post(
        "/api/memory/correct",
        json={"target_fact_id": "fact_beefbeef", "new_status": "archived"},
    )

    docs = client.get("/api/memory/coffre").json()["documents"]
    assert "marathon" not in [f["object"] for d in docs for f in d["faits"]]
    assert kernel.get_fact("fact_beefbeef") is not None, "l'historique doit rester"


# ── 4. Retenir passe par l'extraction ─────────────────────────────────────────


def test_retenir_appelle_la_chaine_d_extraction(
    app_et_client: tuple[FastAPI, TestClient, Any],
) -> None:
    """Jamais d'écriture directe : un fait inséré à la main échapperait à la
    réconciliation et doublerait le souvenir qu'il devait préciser."""
    _app, client, ingest = app_et_client

    r = client.post("/api/memory/retenir", json={"texte": "je passe au thé vert le matin"})

    assert r.status_code == 200
    assert ingest.appels == ["je passe au thé vert le matin"]
    assert r.json()["retenus"] == 1


def test_retenir_refuse_le_vide(client: TestClient) -> None:
    assert client.post("/api/memory/retenir", json={"texte": "   "}).status_code == 400


def test_retenir_sans_extraction_branchee_repond_503(kernel: MemoryKernel) -> None:
    app = FastAPI()
    app.include_router(router)
    app.state.memory_kernel = kernel
    assert app.state  # l'ingest n'est volontairement pas branché

    with TestClient(app) as c:
        assert c.post("/api/memory/retenir", json={"texte": "x"}).status_code == 503


# ── 5. La boîte, vue et déclenchée depuis la page ─────────────────────────────


def test_le_coffre_expose_le_contenu_de_la_boite(client: TestClient) -> None:
    corps = client.get("/api/memory/coffre").json()

    assert corps["boite"]["existe"] is False, "rien ne l'a créée dans ce test"
    assert corps["boite"]["contenu"] == ""


def test_la_boite_creee_est_exposee(
    app_et_client: tuple[FastAPI, TestClient, Any], tmp_path: Path
) -> None:
    app, client, _ = app_et_client
    app.state.boite_memoire.creer_si_absente()

    boite = client.get("/api/memory/coffre").json()["boite"]

    assert boite["existe"] is True
    assert "faux" in boite["contenu"], "le mode d'emploi doit être visible dans la page"
    assert (tmp_path / "mirror" / NOM_FICHIER).exists()


def test_appliquer_maintenant_traite_une_consigne_ecrite_dans_obsidian(
    app_et_client: tuple[FastAPI, TestClient, Any],
) -> None:
    """Sans ce bouton, une correction écrite depuis le téléphone attend dix
    minutes — dix minutes pendant lesquelles on doute et on la réécrit."""
    app, client, _ = app_et_client
    boite = app.state.boite_memoire
    boite.creer_si_absente()
    boite.chemin.write_text(
        boite.chemin.read_text(encoding="utf-8") + "\nfaux ^fact-00c2b7c5e5 : thé vert\n",
        encoding="utf-8",
    )

    r = client.post("/api/memory/coffre/traiter")

    assert r.status_code == 200
    assert r.json()["appliquees"] == 1
    assert app.state.memory_kernel.get_fact("fact_00c2b7c5e5").object == "thé vert"


def test_traiter_sans_boite_branchee_repond_503(kernel: MemoryKernel) -> None:
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        assert c.post("/api/memory/coffre/traiter").status_code == 503


def test_le_rendu_de_traiter_a_la_forme_attendue_par_la_page(
    app_et_client: tuple[FastAPI, TestClient, Any],
) -> None:
    """La page lit ces quatre compteurs : un renommage silencieux la rendrait muette."""
    _app, client, _ = app_et_client

    corps = client.post("/api/memory/coffre/traiter").json()

    assert set(corps) == set(ResultatBoiteReception().__dict__)


def test_coffre_sans_miroir_repond_503() -> None:
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        assert c.get("/api/memory/coffre").status_code == 503
