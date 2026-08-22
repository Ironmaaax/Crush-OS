# Copyright (C) 2026 Maxime Song

"""La file d'initiatives : ce qui la remplissait pour rien.

MESURE DE DÉPART, sur sept jours de données réelles : 76 propositions, dont 44
rejetées et 5 écartées — 64 % de déchet. 25 en attente, et parmi elles deux
alertes pour la même pluie, trois initiatives pour le même projet Mapbox, et une
majorité d'items de veille sans action demandée.

CAUSE RACINE : le générateur repartait de l'état du monde SEUL, toutes les trois
heures, sans savoir ce qu'il avait déjà proposé ni ce qui avait été rejeté. Il
redécouvrait donc les mêmes choses indéfiniment.

Ce qui est défendu ici, dans l'ordre de ce qui coûterait le plus cher :

1. ne PAS fusionner à tort — une initiative fusionnée disparaît sans avoir été
   vue, alors qu'un doublon se contente d'agacer ;
2. le générateur voit son passé, sinon rien ne change ;
3. une file pleine n'est plus alimentée ;
4. ce qui n'est jamais tranché est marqué, pas oublié en silence.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from crush.engine.proactive import store as magasin
from crush.engine.proactive.initiative_generator import _bloc_historique
from crush.engine.proactive.schemas import InitiativeType
from crush.engine.proactive.store import (
    InitiativeStore,
    _contenance,
    _mots_significatifs,
    _similar,
)


@pytest.fixture
def dossier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirige le magasin vers un dossier jetable."""
    d = tmp_path / "initiatives"
    d.mkdir()
    monkeypatch.setattr(magasin, "INITIATIVES_DIR", d)
    return d


def _ecrire(dossier: Path, jour: datetime, entrees: list[dict]) -> None:
    fichier = dossier / (jour.strftime("%Y-%m-%d") + ".jsonl")
    with fichier.open("a", encoding="utf-8") as f:
        for e in entrees:
            base = {
                "id": e.get("id", "init_x"),
                "type": e.get("type", "suggestion"),
                "title": e.get("title", "Titre"),
                "context": "",
                "reasoning": "",
                "action": "",
                "priority": e.get("priority", "medium"),
                "execution_mode": e.get("execution_mode", "notify"),
                "status": e.get("status", "pending"),
                "created_at": e.get("created_at", jour.isoformat()),
                "autonomy_level": 0,
                "permission_required": False,
                "cost_max_usd": 0.0,
                "risk": "",
                "deadline": None,
                "next_action": "",
                "requires_validation": False,
            }
            f.write(json.dumps(base) + "\n")


# ── 1. Ne pas fusionner à tort ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Relancer comparatif casques audio", "Reprendre note vélo électrique"),
        ("Vérification rappel Tesla", "Promo Laptop Microsoft (-280€)"),
        ("Vérifier débit Livebox (Si Sosh)", "Hausse des frais de résiliation Orange"),
        ("Optimiser l'index mémoire long terme", "Mise à jour du backlog de tâches"),
        ("Configuration Mapbox", "Météo : Pluie à 18h00 (85%)"),
    ],
)
def test_deux_initiatives_distinctes_ne_fusionnent_pas(a: str, b: str) -> None:
    """LE test le plus important. Fusionner à tort fait DISPARAÎTRE une
    initiative que l'utilisateur n'aura jamais vue ; un doublon l'agace
    seulement. Les deux erreurs ne coûtent pas la même chose."""
    assert _similar(a, b, "suggestion", "suggestion") is False


def test_un_seul_mot_partage_ne_suffit_pas_a_fusionner() -> None:
    """Sinon deux chantiers différents portant « projet » n'en feraient qu'un."""
    assert (
        _similar("Projet Alpha à cadrer", "Projet Beta à livrer", "suggestion", "suggestion")
        is False
    )


def test_des_types_differents_ne_fusionnent_pas_sur_la_contenance() -> None:
    """Une alerte et une suggestion ne naissent pas du même déclencheur, même
    si elles partagent des mots."""
    assert _similar("Pluie à 18h", "Pluie imminente", "alert", "suggestion") is False


# ── 2. Fusionner ce qui doit l'être ───────────────────────────────────────────


def test_les_deux_alertes_de_pluie_fusionnent() -> None:
    """Cas réel observé le 22 août : la même averse annoncée deux fois, à deux
    cycles d'écart, avec des pourcentages différents."""
    assert _similar("Météo : Pluie à 18h00 (85%)", "Pluie imminente (98%) à 18h", "alert", "alert")


@pytest.mark.parametrize(
    "autre", ["Démarrage projet Mapbox", "Exploration Mapbox SDK"]
)
def test_les_initiatives_du_meme_projet_fusionnent(autre: str) -> None:
    """Cas réel : trois initiatives Mapbox en attente le même jour."""
    assert _similar("Configuration Mapbox", autre, "suggestion", "suggestion")


def test_les_chiffres_ne_distinguent_pas_deux_titres() -> None:
    """C'était la cause mécanique : « 85 », « 98 », « 18h00 » et « 18h »
    comptaient comme des mots distincts et noyaient les mots communs."""
    # « h » de « 18h00 » ne survit pas non plus : un mot d'une seule lettre ne
    # distingue rien, et le seuil de deux caracteres l'ecarte avec les chiffres.
    assert _mots_significatifs("Pluie à 18h00 (85%)") == {"pluie"}


def test_les_accents_ne_distinguent_pas_deux_titres() -> None:
    """Un modèle de langage écrit « météo » ou « meteo » selon l'humeur."""
    assert _mots_significatifs("Météo") == _mots_significatifs("meteo")


def test_la_contenance_repond_a_la_bonne_question() -> None:
    """Jaccard punit la différence de longueur : un titre court entièrement
    contenu dans un long obtient un score bas alors qu'il dit la même chose."""
    court, long_ = {"pluie"}, {"pluie", "imminente", "orage"}

    assert _contenance(court, long_) == 1.0
    assert _contenance(set(), long_) == 0.0


# ── 3. Le générateur voit son passé ───────────────────────────────────────────


def test_sans_historique_le_bloc_est_vide() -> None:
    """Le comportement d'avant doit rester intact quand rien n'est fourni."""
    assert _bloc_historique(None) == ""
    assert _bloc_historique({}) == ""
    assert _bloc_historique({"en_attente": [], "rejetes": []}) == ""


def test_le_bloc_dit_de_ne_pas_repeter_ce_qui_attend() -> None:
    bloc = _bloc_historique({"en_attente": ["Pluie à 18h"], "rejetes": [], "approuves": []})

    assert "DÉJÀ EN ATTENTE" in bloc
    assert "Pluie à 18h" in bloc
    assert "ne les redis pas" in bloc


def test_le_bloc_dit_de_ne_plus_proposer_ce_qui_est_rejete() -> None:
    """44 rejets sur 76 en sept jours, et le générateur ne les voyait pas."""
    bloc = _bloc_historique({"rejetes": ["Promo Laptop Microsoft"], "en_attente": []})

    assert "DÉJÀ REJETÉ" in bloc
    assert "Promo Laptop Microsoft" in bloc


def test_le_bloc_ignore_les_titres_vides() -> None:
    bloc = _bloc_historique({"rejetes": ["", "   ", "Vrai titre"]})

    assert bloc.count("- ") == 1


def test_le_resume_range_par_statut(dossier: Path) -> None:
    hier = datetime.now() - timedelta(days=1)
    _ecrire(dossier, hier, [
        {"id": "a", "title": "En attente", "status": "pending"},
        {"id": "b", "title": "Rejetee", "status": "rejected"},
        {"id": "c", "title": "Ecartee", "status": "dismissed"},
        {"id": "d", "title": "Approuvee", "status": "approved"},
    ])

    r = InitiativeStore().resume_pour_generateur()

    assert r["en_attente"] == ["En attente"]
    assert set(r["rejetes"]) == {"Rejetee", "Ecartee"}
    assert r["approuves"] == ["Approuvee"]


def test_le_resume_est_plafonne(dossier: Path) -> None:
    """Ce texte part dans CHAQUE appel du cycle proactif : le contexte se paie."""
    hier = datetime.now() - timedelta(days=1)
    _ecrire(dossier, hier, [
        {"id": f"r{i}", "title": f"Rejet {i}", "status": "rejected"} for i in range(40)
    ])

    r = InitiativeStore().resume_pour_generateur(par_categorie=5)

    assert len(r["rejetes"]) == 5
    # Les plus RÉCENTS : un rejet d'hier renseigne mieux qu'un rejet de la
    # semaine dernière.
    assert r["rejetes"][-1] == "Rejet 39"


# ── 4. Ce qui n'est jamais tranché est marqué, pas oublié ─────────────────────


def test_une_initiative_trop_vieille_passe_en_expired(dossier: Path) -> None:
    """Avant : elle sortait de la fenêtre de sept jours au huitième jour, en
    restant `pending` dans un fichier que plus personne ne lit. On ne pouvait
    donc pas savoir combien de questions étaient restées sans réponse."""
    vieux = datetime.now() - timedelta(days=9)
    _ecrire(dossier, vieux, [{"id": "vieille", "title": "Oubliee", "status": "pending"}])

    assert InitiativeStore().expirer(jours=5) == 1

    contenu = (dossier / (vieux.strftime("%Y-%m-%d") + ".jsonl")).read_text(encoding="utf-8")
    assert '"status": "expired"' in contenu


def test_une_initiative_recente_n_expire_pas(dossier: Path) -> None:
    hier = datetime.now() - timedelta(days=1)
    _ecrire(dossier, hier, [{"id": "fraiche", "title": "Fraiche", "status": "pending"}])

    assert InitiativeStore().expirer(jours=5) == 0
    assert len(InitiativeStore().load_pending_all(days=7)) == 1


def test_une_initiative_deja_tranchee_n_est_pas_touchee(dossier: Path) -> None:
    """Écraser un `approved` en `expired` effacerait la trace d'une décision."""
    vieux = datetime.now() - timedelta(days=9)
    _ecrire(dossier, vieux, [{"id": "ok", "title": "Decidee", "status": "approved"}])

    assert InitiativeStore().expirer(jours=5) == 0

    contenu = (dossier / (vieux.strftime("%Y-%m-%d") + ".jsonl")).read_text(encoding="utf-8")
    assert '"status": "approved"' in contenu


def test_expirer_est_idempotent(dossier: Path) -> None:
    """La passe tourne chaque nuit : elle ne doit pas recompter les mêmes."""
    vieux = datetime.now() - timedelta(days=9)
    _ecrire(dossier, vieux, [{"id": "v", "title": "Vieille", "status": "pending"}])
    s = InitiativeStore()

    assert s.expirer(jours=5) == 1
    assert s.expirer(jours=5) == 0


def test_expirer_sans_aucun_fichier_ne_leve_pas(dossier: Path) -> None:
    assert InitiativeStore().expirer(jours=5) == 0


# ── 5. Une file pleine n'est plus alimentée ───────────────────────────────────


class _MagasinPlein:
    def __init__(self, en_attente: int) -> None:
        self._n = en_attente
        self.sauvegardes: list[object] = []

    def resume_pour_generateur(self) -> dict[str, list]:
        return {"en_attente": [f"Titre {i}" for i in range(self._n)], "rejetes": []}

    def load_pending_all(self, days: int = 7) -> list:
        return []

    def save(self, initiative: object) -> None:
        self.sauvegardes.append(initiative)


class _GenerateurEspion:
    def __init__(self) -> None:
        self.appels: list[dict | None] = []

    async def generate(self, state: object, historique: dict | None = None) -> list:
        self.appels.append(historique)
        return []


def _moteur(store: object, generateur: object) -> object:
    from crush.engine.proactive.engine import ProactiveEngine

    return ProactiveEngine(
        notification_queue=SimpleNamespace(add=lambda _m: None),  # type: ignore[arg-type]
        broadcast_event=lambda _e: None,
        builder=SimpleNamespace(build=_build_vide),  # type: ignore[arg-type]
        generator=generateur,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
    )


async def _build_vide() -> object:
    return SimpleNamespace(to_prompt_context=lambda: "## AGENDA\nquelque chose")


async def test_une_file_pleine_arrete_la_generation() -> None:
    """Huit cycles par jour à cinq initiatives, c'est quarante par jour dans une
    file que rien ne vide : elle ne peut que croître, et une file qui croît est
    une file qu'on cesse d'ouvrir."""
    gen = _GenerateurEspion()
    moteur = _moteur(_MagasinPlein(en_attente=30), gen)

    resultat = await moteur._run_cycle()

    assert resultat == []
    assert gen.appels == [], "aucun appel LLM ne doit partir"


async def test_une_file_courte_laisse_generer() -> None:
    gen = _GenerateurEspion()
    moteur = _moteur(_MagasinPlein(en_attente=2), gen)

    await moteur._run_cycle()

    assert len(gen.appels) == 1


async def test_le_generateur_recoit_bien_l_historique() -> None:
    """Sans ça, rien de tout le reste ne sert."""
    gen = _GenerateurEspion()
    moteur = _moteur(_MagasinPlein(en_attente=2), gen)

    await moteur._run_cycle()

    assert gen.appels[0] is not None
    assert len(gen.appels[0]["en_attente"]) == 2


class _MagasinAncien:
    """Magasin qui ne sait pas rendre d'historique — version antérieure."""

    def load_pending_all(self, days: int = 7) -> list:
        return []

    def save(self, initiative: object) -> None:
        pass


async def test_un_magasin_sans_historique_ne_casse_pas_le_cycle() -> None:
    """Le cycle proactif ne doit pas tomber pour un enrichissement de prompt :
    il perdrait la seule fonction qui marchait déjà."""
    gen = _GenerateurEspion()
    moteur = _moteur(_MagasinAncien(), gen)

    await moteur._run_cycle()

    assert gen.appels == [{}]


# ── 6. Le prompt ne demande plus de la veille ────────────────────────────────


def test_le_prompt_interdit_les_items_sans_action() -> None:
    """12 des 25 initiatives en attente étaient des brèves : promotions, hausses
    de tarif, sorties de produit. Rien à décider, donc rien à faire d'autre que
    rejeter — et à force de rejeter on cesse de lire."""
    from crush.engine.proactive.initiative_generator import _INITIATIVE_BODY

    assert "que doit-il FAIRE" in _INITIATIVE_BODY
    assert "ZÉRO INITIATIVE" in _INITIATIVE_BODY


def test_le_prompt_ne_se_contredit_plus_sur_le_plafond() -> None:
    """Le corps disait « 2 HIGH max » et la requête « 3 HIGH max »."""
    from crush.engine.proactive.initiative_generator import _INITIATIVE_BODY

    assert "3 HIGH" not in _INITIATIVE_BODY
    assert "au plus 2 en priorité HIGH" in _INITIATIVE_BODY


def test_le_type_reste_lisible_apres_dedup() -> None:
    """`_type_de` doit accepter l'enum comme la chaîne : les objets relus d'un
    fichier JSONL ne portent pas toujours l'enum."""
    from crush.engine.proactive.store import _type_de

    assert _type_de(SimpleNamespace(type=InitiativeType.ALERT)) == "alert"
    assert _type_de(SimpleNamespace(type="alert")) == "alert"
    assert _type_de(SimpleNamespace()) == ""
