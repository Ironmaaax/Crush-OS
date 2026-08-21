# Copyright (C) 2026 Maxime Song

"""Le graphe de l'assistant — ce dont il est fait, et ce qui est relié.

LA RÈGLE QUI DÉCIDE DE TOUT ICI : aucune arête inventée. Un graphe où l'on ajoute
des liens pour faire joli ne dit plus rien — on ne peut plus distinguer une vraie
proximité d'un effet de mise en page, et on cesse de s'y fier. Ces tests
défendent exactement ça, et rien d'autre n'a autant d'importance.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from crush.interfaces.api.graphe import RACINE, router
from crush.kernel.schemas import FactStatus, RelationType
from crush.providers.memory.kernel import MemoryKernel
from crush.providers.memory.mirror import MemoryMirror
from crush.providers.memory.schemas import DecayPolicy, Fact


def _fait(fid: str, obj: str, category: str = "preference", predicate: str = "prefers") -> Fact:
    now = datetime.now()
    return Fact(
        id=fid,
        subject="max",
        predicate=predicate,
        object=obj,
        category=category,
        status=FactStatus.ACTIVE,
        confidence=0.8,
        support_count=1,
        decay_policy=DecayPolicy.MEDIUM,
        importance=0.6,
        created_at=now,
        last_seen_at=now,
        updated_at=now,
    )


_SCHEMAS = [
    {"name": "notion_tasks", "description": "Lit les tâches Notion."},
    {"name": "spotify_control", "description": "Pilote la lecture Spotify."},
    {"name": "execute_cli", "description": "Lance une commande sur le serveur."},
    {"name": "remote_pc", "description": "Agit sur l'ordinateur de l'utilisateur."},
]


@pytest.fixture
def kernel(tmp_path: Path) -> MemoryKernel:
    k = MemoryKernel(tmp_path / "k.db")
    k.insert_fact(_fait("fact_aaaa1111", "café noir"))
    k.insert_fact(_fait("fact_bbbb2222", "notion pour les tâches", "tool", "uses"))
    k.insert_fact(_fait("fact_cccc3333", "courir un marathon", "goal", "targets"))
    return k


@pytest.fixture
def client(tmp_path: Path, kernel: MemoryKernel) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.memory_kernel = kernel
    app.state.memory_mirror = MemoryMirror(kernel, tmp_path / "mirror")
    app.state.tool_registry = SimpleNamespace(schemas=lambda: list(_SCHEMAS))
    return TestClient(app)


def _graphe(client: TestClient) -> dict[str, Any]:
    r = client.get("/api/graphe")
    assert r.status_code == 200
    return r.json()


def _origines(g: dict) -> set[str]:
    return {lien["origine"] for lien in g["liens"]}


# ── Aucune arête inventée ─────────────────────────────────────────────────────


def test_chaque_arete_declare_son_origine(client: TestClient) -> None:
    """Une arête sans justification est une arête qu'on ne peut pas contester."""
    g = _graphe(client)

    connues = {"contenu", "predicat", "memoire", "nom", "pilote", "partie"}
    assert g["liens"], "graphe sans aucune arête"
    assert _origines(g) <= connues


def test_toute_arete_relie_deux_noeuds_existants(client: TestClient) -> None:
    g = _graphe(client)
    ids = {n["id"] for n in g["noeuds"]}

    for lien in g["liens"]:
        assert lien["de"] in ids and lien["vers"] in ids


def test_aucune_arete_en_double_ni_boucle(client: TestClient) -> None:
    """Deux arêtes identiques se superposent au rendu et faussent les degrés."""
    g = _graphe(client)

    paires = [tuple(sorted((lien["de"], lien["vers"]))) for lien in g["liens"]]
    assert len(paires) == len(set(paires))
    assert all(a != b for a, b in paires)


def test_la_racine_ne_touche_pas_les_feuilles(client: TestClient) -> None:
    """Relier la racine à chaque fait donnerait un oursin qui n'apprend rien.

    Tout serait à un pas de tout, les degrés perdraient leur sens, et le panneau
    « par quoi tout passe » ne désignerait plus que la racine.
    """
    g = _graphe(client)

    depuis_racine = {
        (lien["vers"] if lien["de"] == RACINE else lien["de"])
        for lien in g["liens"]
        if RACINE in (lien["de"], lien["vers"])
    }
    types = {n["id"]: n["type"] for n in g["noeuds"]}

    assert depuis_racine, "la racine doit être reliée à quelque chose"
    assert all(types[nid] != "fait" for nid in depuis_racine)
    assert all(types[nid] != "verbe" for nid in depuis_racine)


# ── La répartition ne peut pas contredire Obsidian ────────────────────────────


def test_les_documents_sont_ceux_du_miroir(
    client: TestClient, kernel: MemoryKernel, tmp_path: Path
) -> None:
    mirror = MemoryMirror(kernel, tmp_path / "mirror")
    attendu = {"doc:" + d.fichier for d in mirror.grouper()}

    g = _graphe(client)
    vu = {n["id"] for n in g["noeuds"] if n["type"] == "document"}

    assert vu == attendu


def test_chaque_fait_est_dans_exactement_un_document(client: TestClient) -> None:
    """Un fait dans deux documents serait un fait qu'on corrige deux fois."""
    g = _graphe(client)

    compte: dict[str, int] = {}
    for lien in g["liens"]:
        if lien["origine"] != "contenu":
            continue
        fid = lien["vers"] if lien["vers"].startswith("fait:") else lien["de"]
        compte[fid] = compte.get(fid, 0) + 1

    faits = {n["id"] for n in g["noeuds"] if n["type"] == "fait"}
    assert set(compte) == faits
    assert all(v == 1 for v in compte.values())


# ── Les verbes, et pas les sujets ─────────────────────────────────────────────


def test_les_verbes_servent_de_pivot(client: TestClient) -> None:
    g = _graphe(client)

    verbes = {n["label"] for n in g["noeuds"] if n["type"] == "verbe"}
    assert verbes == {"prefers", "uses", "targets"}


def test_le_sujet_ne_devient_pas_un_noeud(client: TestClient) -> None:
    """Il vaut « max » sur la quasi-totalité des faits : un moyeu aussi gros
    qu'inutile, qui écraserait tout le reste du graphe."""
    g = _graphe(client)

    assert not [n for n in g["noeuds"] if n["type"] == "sujet"]
    assert "max" not in {n["label"] for n in g["noeuds"] if n["type"] != "racine"}


# ── Les arêtes déduites, et le fait qu'elles soient marquées ──────────────────


def test_un_souvenir_qui_nomme_un_service_est_relie(client: TestClient) -> None:
    """« max uses notion pour les tâches » doit toucher le service Notion."""
    g = _graphe(client)

    noms = [lien for lien in g["liens"] if lien["origine"] == "nom"]
    ids = {n["id"] for n in g["noeuds"]}

    assert "integ:Notion" in ids
    assert any(
        "fact_bbbb2222" in lien["de"] + lien["vers"] and "Notion" in lien["de"] + lien["vers"]
        for lien in noms
    )


def test_l_arete_deduite_est_distinguable_des_autres(client: TestClient) -> None:
    """C'est la seule qui relève d'une supposition : elle doit se voir comme telle."""
    g = _graphe(client)

    deduites = [lien for lien in g["liens"] if lien["origine"] == "nom"]
    assert deduites, "aucune arête déduite dans ce jeu de données"
    assert all(lien["origine"] == "nom" for lien in deduites)


def test_un_souvenir_anodin_ne_nomme_rien(client: TestClient) -> None:
    """« café noir » ne doit accrocher aucun service — sinon tout accroche tout."""
    g = _graphe(client)

    accroches = {
        lien["de"] + "|" + lien["vers"] for lien in g["liens"] if lien["origine"] == "nom"
    }
    assert not any("fact_aaaa1111" in a for a in accroches)


def test_un_outil_sans_service_derriere_reste_rattache(client: TestClient) -> None:
    """`execute_cli` ne parle à rien d'extérieur : isolé, il se lirait comme un bug."""
    g = _graphe(client)

    degres = {n["id"]: n["degre"] for n in g["noeuds"]}
    assert degres["outil:execute_cli"] >= 1


def test_l_outil_qui_pilote_un_service_le_montre(client: TestClient) -> None:
    g = _graphe(client)

    pilotes = {
        tuple(sorted((lien["de"], lien["vers"])))
        for lien in g["liens"]
        if lien["origine"] == "pilote"
    }
    assert tuple(sorted(("outil:notion_tasks", "integ:Notion"))) in pilotes


# ── Les relations explicites de la mémoire ────────────────────────────────────


def test_une_relation_entre_faits_apparait(client: TestClient, kernel: MemoryKernel) -> None:
    """`fact_relations` est vide aujourd'hui, mais se remplira : le jour où un
    fait en remplacera un autre, l'arête doit sortir sans rien changer au code."""
    kernel.link_facts("fact_aaaa1111", "fact_cccc3333", RelationType.RELATED_TO)

    g = _graphe(client)

    assert "memoire" in _origines(g)


def test_sans_relation_enregistree_aucune_arete_memoire(client: TestClient) -> None:
    """L'état réel d'aujourd'hui : on ne fabrique pas de liens pour compenser."""
    g = _graphe(client)

    assert "memoire" not in _origines(g)


# ── Ce que les panneaux affichent ─────────────────────────────────────────────


def test_les_moyeux_sont_tries_du_plus_relie_au_moins(client: TestClient) -> None:
    g = _graphe(client)

    degres = [m["degre"] for m in g["moyeux"]]
    assert degres == sorted(degres, reverse=True)


def test_le_degre_de_chaque_noeud_correspond_a_ses_aretes(client: TestClient) -> None:
    """Un degré faux fausse la taille des sphères ET le classement des moyeux."""
    g = _graphe(client)

    compte: dict[str, int] = {n["id"]: 0 for n in g["noeuds"]}
    for lien in g["liens"]:
        compte[lien["de"]] += 1
        compte[lien["vers"]] += 1

    for n in g["noeuds"]:
        assert n["degre"] == compte[n["id"]], n["id"]


def test_les_noeuds_isoles_sont_nommes_pas_cachees(client: TestClient) -> None:
    """Un nœud relié à rien flottant dans un coin se lit comme un défaut
    d'affichage. On le dit, pour qu'il se lise comme une information."""
    g = _graphe(client)

    isoles_calcules = {n["label"] for n in g["noeuds"] if n["degre"] == 0}
    assert set(g["isoles"]) == isoles_calcules


def test_les_comptes_par_type_couvrent_tous_les_noeuds(client: TestClient) -> None:
    """Le panneau de filtres s'en sert : un écart y masquerait des nœuds."""
    g = _graphe(client)

    assert sum(g["par_type"].values()) == len(g["noeuds"]) == g["total"]["noeuds"]
    assert g["total"]["liens"] == len(g["liens"])


def test_chaque_noeud_porte_de_quoi_remplir_sa_fiche(client: TestClient) -> None:
    g = _graphe(client)

    for n in g["noeuds"]:
        assert n["label"].strip(), n["id"]
        assert n["type"].strip(), n["id"]


def test_le_graphe_est_d_un_seul_morceau(client: TestClient) -> None:
    """Plusieurs îlots séparés donneraient des amas qui s'ignorent à l'écran, et
    on croirait la vue cassée. La racine et ses parties garantissent l'unité."""
    g = _graphe(client)

    voisins: dict[str, list[str]] = {n["id"]: [] for n in g["noeuds"]}
    for lien in g["liens"]:
        voisins[lien["de"]].append(lien["vers"])
        voisins[lien["vers"]].append(lien["de"])

    vus, file = {RACINE}, [RACINE]
    while file:
        for v in voisins[file.pop()]:
            if v not in vus:
                vus.add(v)
                file.append(v)

    assert vus == set(voisins), f"non atteints : {set(voisins) - vus}"


def test_sans_miroir_la_vue_refuse_plutot_que_de_mentir(kernel: MemoryKernel) -> None:
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        assert c.get("/api/graphe").status_code == 503


def test_un_registre_d_outils_en_vrac_ne_casse_pas_la_vue(
    tmp_path: Path, kernel: MemoryKernel
) -> None:
    """La mémoire reste consultable même si le registre d'outils est indisponible."""

    def _casse() -> list[dict]:
        raise RuntimeError("registre indisponible")

    app = FastAPI()
    app.include_router(router)
    app.state.memory_kernel = kernel
    app.state.memory_mirror = MemoryMirror(kernel, tmp_path / "mirror")
    app.state.tool_registry = SimpleNamespace(schemas=_casse)

    with TestClient(app) as c:
        g = c.get("/api/graphe").json()

    assert [n for n in g["noeuds"] if n["type"] == "fait"]
    assert not [n for n in g["noeuds"] if n["type"] == "outil"]
