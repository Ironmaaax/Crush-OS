# Copyright (C) 2026 Maxime Song

"""L'assistant lit enfin ce qu'il sait — et quatre bugs trouvés en chemin.

LE CONSTAT DE DÉPART, vérifié dans le code puis sur la base réelle

Le Memory Kernel contenait 50 faits structurés, scorés, corrigeables, et AUCUN
chemin ne les amenait dans une conversation :

- `MemoryRetrieval` n'était instancié nulle part — code mort.
- `memory_search` lit l'index vectoriel, alimenté par `topics/*.md` et
  `sessions/*.jsonl` uniquement : les faits n'y entrent jamais.
- Le miroir Markdown n'est indexé nulle part.
- Seul `memory_journal` lisait les faits, par DATE.

Ce que le modèle recevait à chaque tour : 389 octets de prose plate.

QUATRE BUGS DÉCOUVERTS EN RÉPARANT

1. `search_facts_fts` enveloppait la requête en PHRASE EXACTE — toute question en
   langue naturelle rendait 0 résultat.
2. `_bm25_to_relevance` était INVERSÉ — le moins pertinent gagnait. Invisible
   jusqu'ici parce que (1) faisait que la formule n'était jamais exercée.
3. `_run_micro` remplaçait tout `user_prefs.md` par la sortie brute du LLM :
   « Rien à changer. » devenait la mémoire.
4. `synthesize` appelait `_build_system()` sans arguments — la passe qui écrit la
   réponse LUE perdait notifications et rappel de sessions.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from crush.engine.faits import bloc_memoire
from crush.providers.memory.auto_dream import _refus_de_remplacement
from crush.providers.memory.kernel import MemoryKernel, _termes_fts
from crush.providers.memory.retrieval import _bm25_to_relevance
from crush.providers.memory.schemas import DecayPolicy, Fact, FactStatus

_PREFS_REELLES = """# Préférences Max

- Goût pour la concision.
- Respect du protocole.
- Souhaite être appelé « Monsieur ».
- Préfère Spotify pour la musique (via le lecteur web).
- Habitude d'écoute : Radiohead, Goo Goo Dolls, David Guetta, Pep's
- Souhaite utiliser le module « vue Obsidian »."""


@pytest.fixture
def kernel(tmp_path: Path) -> MemoryKernel:
    return MemoryKernel(tmp_path / "k.db")


def _fait(
    fid: str,
    predicate: str = "prefers",
    obj: str = "café",
    category: str = "preference",
    confidence: float = 0.8,
    importance: float = 0.6,
    support: int = 1,
    jours: int = 0,
    status: FactStatus = FactStatus.ACTIVE,
) -> Fact:
    quand = datetime.now() - timedelta(days=jours)
    return Fact(
        id=fid,
        subject="max",
        predicate=predicate,
        object=obj,
        category=category,
        status=status,
        confidence=confidence,
        support_count=support,
        decay_policy=DecayPolicy.MEDIUM,
        importance=importance,
        created_at=quand,
        last_seen_at=quand,
        updated_at=quand,
    )


# ── 1. La recherche rendait zéro pour toute phrase ────────────────────────────


def test_une_phrase_naturelle_trouve_quelque_chose(kernel: MemoryKernel) -> None:
    """LE bug de fond. La requête était enveloppée en phrase EXACTE : « mets de la
    musique » exigeait ces quatre mots consécutifs dans un fait de trois mots.
    Mesuré sur la base réelle avant correction : 0 résultat pour toute question."""
    kernel.insert_fact(_fait("f1", "uses", "spotify", "tool"))

    assert kernel.search_facts_fts("spotify musique", k=5), "la disjonction doit sauver la phrase"


def test_le_pluriel_trouve_le_singulier(kernel: MemoryKernel) -> None:
    """Un préfixe FTS5 s'étend vers la DROITE : `projets*` ne trouve pas
    « projet ». L'utilisateur écrit au pluriel, le fait est stocké au singulier —
    mesuré : « quels sont mes projets » ne trouvait pas `decided refonte projet`."""
    kernel.insert_fact(_fait("f1", "decided", "refonte projet", "decision"))

    assert kernel.search_facts_fts("quels sont mes projets", k=5)


def test_les_mots_vides_ne_remontent_pas_toute_la_base(kernel: MemoryKernel) -> None:
    """Sans ce filtre, « de » et « la » matcheraient partout avec un bon score
    BM25 et noieraient les termes qui portent l'information."""
    assert _termes_fts("de la que pour un une") == []
    assert "musique" in _termes_fts("mets de la musique")


def test_aucune_saisie_ne_peut_casser_la_requete(kernel: MemoryKernel) -> None:
    """Les termes sont reconstruits à partir de caractères alphanumériques : rien
    de ce que l'utilisateur écrit ne peut être pris pour un opérateur FTS5."""
    kernel.insert_fact(_fait("f1", "uses", "spotify", "tool"))

    hostiles = ['spotify" OR "', "spotify*(", "NEAR(a b)", "spotify AND NOT x", '""']
    for hostile in hostiles:
        kernel.search_facts_fts(hostile, k=3)  # ne doit pas lever


def test_une_requete_sans_terme_porteur_rend_vide(kernel: MemoryKernel) -> None:
    kernel.insert_fact(_fait("f1"))

    assert kernel.search_facts_fts("de la", k=5) == []
    assert kernel.search_facts_fts("   ", k=5) == []


# ── 2. La pertinence était inversée ───────────────────────────────────────────


def test_la_pertinence_croit_avec_la_qualite_du_match() -> None:
    """La formule était `exp(-|bm25|/cap)`, DÉCROISSANTE. Un fait touchant trois
    termes (bm25 ≈ -6) obtenait 0,74 ; un seul terme (-1,2) obtenait 0,94. Le
    moins pertinent gagnait de 27 %."""
    assert _bm25_to_relevance(-6.0) > _bm25_to_relevance(-1.2)
    assert _bm25_to_relevance(-12.0) > _bm25_to_relevance(-6.0)


def test_pas_de_match_vaut_zero() -> None:
    """Le repli « cold start » est traité par l'appelant, pas ici."""
    assert _bm25_to_relevance(0.0) == 0.0


@pytest.mark.parametrize("bm25", [0.0, -0.1, -1.0, -8.0, -50.0, -1e6, 3.0])
def test_la_pertinence_reste_bornee(bm25: float) -> None:
    assert 0.0 <= _bm25_to_relevance(bm25) <= 1.0


# ── 3. La mémoire en prose se faisait écraser ─────────────────────────────────


@pytest.mark.parametrize(
    "reponse",
    [
        "Rien à changer.",
        "Aucun changement nécessaire.",
        "Je ne peux pas répondre à cette demande.",
        "D'accord, j'ai noté.",
        "- Goût pour la concision.",
    ],
)
def test_une_reponse_conversationnelle_ne_devient_pas_la_memoire(reponse: str) -> None:
    """LE bug le plus coûteux. La seule garde était « non vide et différent » :
    « Rien à changer. » satisfaisait les deux, et la phrase DEVENAIT le fichier
    de préférences. Après chaque échange, sans sauvegarde."""
    assert _refus_de_remplacement(_PREFS_REELLES, reponse) is not None


def test_une_vraie_mise_a_jour_passe() -> None:
    nouveau = _PREFS_REELLES + "\n- Préfère le thé vert le matin."

    assert _refus_de_remplacement(_PREFS_REELLES, nouveau) is None


def test_l_effondrement_de_taille_est_refuse() -> None:
    """Une mémoire construite ne perd pas la moitié de sa substance en un
    échange : c'est la signature d'une réponse tronquée."""
    moitie = "# Préférences\n\n- Une seule ligne restante.\n- Et une autre."

    refus = _refus_de_remplacement(_PREFS_REELLES, moitie)

    assert refus is not None and "effondrement" in refus


def test_une_memoire_encore_vide_peut_se_remplir() -> None:
    """Le garde-fou de taille ne doit pas empêcher le premier remplissage."""
    vide = "# Préférences Max\n"
    premier = "# Préférences Max\n\n- Goût pour la concision.\n- Aime le café."

    assert _refus_de_remplacement(vide, premier) is None


# ── 4. Les doublons : ne plus en créer, absorber les anciens ──────────────────


def test_un_fait_deja_connu_est_trouve_a_l_identique(kernel: MemoryKernel) -> None:
    """`find_active_match` ne regarde que (sujet, prédicat, catégorie) et rend UN
    fait. À l'arrivée de « uses spotify » il rendait « uses vue obsidian », les
    objets différaient, et la réconciliation insérait. Mesuré : 4 doublons."""
    kernel.insert_fact(_fait("f1", "uses", "vue obsidian", "tool", jours=1))
    kernel.insert_fact(_fait("f2", "uses", "spotify", "tool"))

    exact = kernel.find_active_exact("max", "uses", "spotify", "tool")

    assert exact is not None and exact.id == "f2"


def test_la_correspondance_exacte_tolere_casse_et_espaces(kernel: MemoryKernel) -> None:
    kernel.insert_fact(_fait("f1", "uses", "spotify", "tool"))

    assert kernel.find_active_exact("  MAX ", "Uses", " Spotify ", "TOOL") is not None


def test_un_fait_absent_ne_se_devine_pas(kernel: MemoryKernel) -> None:
    kernel.insert_fact(_fait("f1", "uses", "spotify", "tool"))

    assert kernel.find_active_exact("max", "uses", "iris", "tool") is None


def test_la_fusion_somme_les_supports_et_garde_la_meilleure_confiance(
    kernel: MemoryKernel,
) -> None:
    """Une répétition est une confirmation : le survivant hérite du total."""
    kernel.insert_fact(_fait("a", "uses", "iris", "tool", confidence=0.55, support=1, jours=5))
    kernel.insert_fact(_fait("b", "uses", "iris", "tool", confidence=0.75, support=3, jours=2))
    kernel.insert_fact(_fait("c", "uses", "iris", "tool", confidence=0.65, support=1, jours=1))

    assert kernel.fusionner_doublons() == 2

    survivant = kernel.get_fact("b")
    assert survivant is not None
    assert survivant.support_count == 5
    assert survivant.confidence == 0.75
    assert survivant.status is FactStatus.ACTIVE


def test_la_fusion_n_efface_rien(kernel: MemoryKernel) -> None:
    """La constitution du projet l'interdit : on archive, on ne supprime pas."""
    kernel.insert_fact(_fait("a", "uses", "iris", "tool", support=1))
    kernel.insert_fact(_fait("b", "uses", "iris", "tool", support=3))

    kernel.fusionner_doublons()

    absorbe = kernel.get_fact("a")
    assert absorbe is not None, "le fait absorbé doit rester lisible"
    assert absorbe.status is FactStatus.SUPERSEDED
    assert kernel.list_relations("b"), "la relation vers le survivant doit exister"


def test_la_fusion_est_idempotente(kernel: MemoryKernel) -> None:
    """Elle peut être relancée : elle ne doit pas recompter les mêmes."""
    kernel.insert_fact(_fait("a", "uses", "iris", "tool"))
    kernel.insert_fact(_fait("b", "uses", "iris", "tool"))

    assert kernel.fusionner_doublons() == 1
    assert kernel.fusionner_doublons() == 0


def test_la_fusion_ne_touche_pas_aux_faits_distincts(kernel: MemoryKernel) -> None:
    kernel.insert_fact(_fait("a", "uses", "spotify", "tool"))
    kernel.insert_fact(_fait("b", "uses", "iris", "tool"))

    assert kernel.fusionner_doublons() == 0
    assert kernel.count_facts(FactStatus.ACTIVE) == 2


# ── 5. Le bloc injecté dans le prompt ─────────────────────────────────────────


def test_une_base_vide_ne_produit_aucun_bloc(kernel: MemoryKernel) -> None:
    """Pas de titre orphelin dans le prompt : rien à dire, on ne dit rien."""
    assert bloc_memoire(kernel) == ""


def test_les_faits_peu_surs_sont_ecartes(kernel: MemoryKernel) -> None:
    """En dessous de 0,5 le fait n'est pas su : c'est une hypothèse vue une fois.
    Le miroir Markdown emploie déjà ce seuil."""
    kernel.insert_fact(_fait("sur", obj="café", confidence=0.9))
    kernel.insert_fact(_fait("doute", obj="thé au safran", confidence=0.3))

    bloc = bloc_memoire(kernel)

    assert "café" in bloc
    assert "safran" not in bloc


def test_le_bloc_est_plafonne(kernel: MemoryKernel) -> None:
    """Le prompt système pèse déjà ~7 800 jetons et se repaie à chaque tour."""
    for i in range(60):
        kernel.insert_fact(_fait(f"f{i}", obj=f"objet numero {i}"))

    bloc = bloc_memoire(kernel, plafond=10)

    assert bloc.count("\n- ") == 10


def test_le_plus_important_survit_au_plafond(kernel: MemoryKernel) -> None:
    """On coupe sur importance × confiance AVANT de regrouper : l'inverse aurait
    laissé une catégorie bavarde évincer une catégorie décisive."""
    kernel.insert_fact(_fait("cle", obj="être appelé monsieur", importance=1.0, confidence=1.0))
    for i in range(20):
        kernel.insert_fact(_fait(f"bruit{i}", obj=f"detail {i}", importance=0.1, confidence=0.5))

    bloc = bloc_memoire(kernel, plafond=3)

    assert "monsieur" in bloc


def test_un_fait_incertain_est_marque_comme_tel(kernel: MemoryKernel) -> None:
    """Un fait à 55 % ne doit pas être affirmé sur le même ton qu'un fait à 95 % —
    sans marque, le modèle les traite à égalité."""
    kernel.insert_fact(_fait("sur", obj="café", confidence=0.95))
    kernel.insert_fact(_fait("moyen", obj="thé", confidence=0.55))

    bloc = bloc_memoire(kernel)

    lignes = {ligne.split(" ", 2)[-1].split(" _(")[0]: ligne for ligne in bloc.splitlines()}
    assert "à confirmer" not in lignes.get("café", "")
    assert "à confirmer" in lignes.get("thé", "")


def test_seuls_les_faits_actifs_entrent(kernel: MemoryKernel) -> None:
    kernel.insert_fact(_fait("actif", obj="café"))
    kernel.insert_fact(_fait("vieux", obj="thé périmé", status=FactStatus.SUPERSEDED))
    kernel.insert_fact(_fait("revoir", obj="hypothèse", status=FactStatus.NEEDS_REVIEW))

    bloc = bloc_memoire(kernel)

    assert "café" in bloc
    assert "périmé" not in bloc
    assert "hypothèse" not in bloc


def test_l_identite_et_la_facon_de_parler_viennent_en_tete(kernel: MemoryKernel) -> None:
    """Elles pèsent sur CHAQUE réponse, alors qu'un souvenir d'outil ne pèse que
    sur les questions qui le concernent."""
    kernel.insert_fact(_fait("t", "uses", "spotify", "tool", importance=1.0))
    kernel.insert_fact(_fait("i", "is", "ingénieur", "identity", importance=0.2))

    bloc = bloc_memoire(kernel)

    assert bloc.index("Qui il est") < bloc.index("Ses outils")


def test_un_objet_interminable_est_tronque(kernel: MemoryKernel) -> None:
    """Une extraction ratée produit une phrase entière en objet : sans troncature
    elle mangerait le bloc à elle seule."""
    kernel.insert_fact(_fait("long", obj="x" * 400))

    bloc = bloc_memoire(kernel)

    assert "…" in bloc
    assert len(bloc) < 300


def test_un_magasin_en_panne_ne_prive_pas_de_reponse() -> None:
    """Un prompt sans mémoire vaut mieux que pas de réponse du tout."""

    class _Casse:
        def list_facts_by_status(self, status: object, limit: object = None) -> list:
            raise RuntimeError("base verrouillée")

    assert bloc_memoire(_Casse()) == ""  # type: ignore[arg-type]


def test_le_bloc_est_deterministe(kernel: MemoryKernel) -> None:
    """À base inchangée, sortie identique : c'est ce qui le rend relisable, et ce
    qui permettra un jour de mettre le préfixe système en cache."""
    for i in range(8):
        kernel.insert_fact(_fait(f"f{i}", obj=f"objet {i}", importance=0.5, confidence=0.8))

    assert bloc_memoire(kernel) == bloc_memoire(kernel)
