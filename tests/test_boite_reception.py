# Copyright (C) 2026 Maxime Song

"""Boîte de réception Obsidian — le chemin de retour qui manquait.

Le miroir Markdown est unidirectionnel : il se régénère depuis SQLite à chaque
passe nocturne. Un souvenir faux repéré en le lisant ne pouvait donc pas être
corrigé là où on le lisait — l'éditer sur place n'avait aucun effet, et la
modification disparaissait au rendu suivant sans rien dire.

Ce qui est vérifié ici, dans l'ordre de ce qui coûterait le plus cher :

1. l'invariant structurel — le rendu du miroir ne touche JAMAIS la boîte ;
2. le mode d'emploi du fichier n'est pas exécuté comme une consigne ;
3. rien de ce qui est écrit ne se perd, même incompris ;
4. une boîte vide n'entraîne aucune écriture — c'est ce qui rend une
   synchronisation bidirectionnelle sûre le reste du temps ;
5. et seulement ensuite : les trois verbes font ce qu'ils annoncent.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from crush.kernel.schemas import FactStatus
from crush.providers.memory.boite_reception import NOM_FICHIER, BoiteReception, _analyser
from crush.providers.memory.kernel import MemoryKernel
from crush.providers.memory.mirror import MemoryMirror, ancre_de_fait, id_depuis_ancre
from crush.providers.memory.schemas import DecayPolicy, Fact


def _fait(
    fid: str = "fact_00c2b7c5e5",
    subject: str = "max",
    predicate: str = "prefers",
    obj: str = "café",
    category: str = "preference",
) -> Fact:
    now = datetime.now()
    return Fact(
        id=fid,
        subject=subject,
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


class _IngestFactice:
    """Reproduit la forme de retour de `MemoryIngest.ingest` sans appeler de LLM."""

    def __init__(self, nouveaux: int = 1, confirmes: int = 0) -> None:
        self.appels: list[str] = []
        self._nouveaux = nouveaux
        self._confirmes = confirmes

    async def ingest(self, content: str, **_: str) -> object:
        self.appels.append(content)
        return type(
            "Rendu",
            (),
            {
                "new_facts": [object()] * self._nouveaux,
                "confirmed": [object()] * self._confirmes,
            },
        )()


class _IngestQuiCasse:
    async def ingest(self, content: str, **_: str) -> object:
        raise RuntimeError("le modèle de fond ne répond pas")


@pytest.fixture
def kernel(tmp_path: Path) -> MemoryKernel:
    return MemoryKernel(tmp_path / "k.db")


@pytest.fixture
def mirror(tmp_path: Path, kernel: MemoryKernel) -> MemoryMirror:
    return MemoryMirror(kernel, tmp_path / "mirror")


@pytest.fixture
def boite(kernel: MemoryKernel, mirror: MemoryMirror) -> BoiteReception:
    return BoiteReception(kernel, mirror, ingest=_IngestFactice())


def _ecrire(boite: BoiteReception, *consignes: str) -> None:
    """Ajoute des consignes en tête du fichier, comme on le ferait dans Obsidian."""
    boite.creer_si_absente()
    boite.chemin.write_text(
        boite.chemin.read_text(encoding="utf-8") + "\n" + "\n".join(consignes) + "\n",
        encoding="utf-8",
    )


# ── 1. L'invariant : le miroir n'écrit jamais dans la boîte ───────────────────


def test_le_rendu_du_miroir_ne_touche_pas_a_la_boite(
    kernel: MemoryKernel, mirror: MemoryMirror, boite: BoiteReception
) -> None:
    """LE test qui justifie l'emplacement du fichier.

    Si la boîte vivait dans `user/` ou `crush/`, une catégorie ajoutée un jour
    dans `_CATEGORY_TO_FILE` pourrait la viser et effacer des consignes non
    encore traitées. À la racine, `export()` ne peut pas l'atteindre.
    """
    _ecrire(boite, "retiens : je bois du thé le matin")
    avant = boite.chemin.read_text(encoding="utf-8")

    kernel.insert_fact(_fait(category="preference"))
    kernel.insert_fact(_fait("fact_beef", category="goal", obj="courir un marathon"))
    rapport = mirror.export()

    assert boite.chemin.read_text(encoding="utf-8") == avant
    assert NOM_FICHIER not in rapport.files_written


def test_aucun_fichier_du_miroir_ne_porte_le_nom_de_la_boite() -> None:
    """Garde-fou à la source : le nom ne doit pas pouvoir devenir une cible."""
    from crush.providers.memory.mirror import (
        _CATEGORY_TO_FILE,
        _NEEDS_REVIEW_FILE,
        _UNCERTAIN_FILE,
    )

    cibles = {*_CATEGORY_TO_FILE.values(), _UNCERTAIN_FILE, _NEEDS_REVIEW_FILE}
    assert all(NOM_FICHIER not in c for c in cibles)
    # Et toutes les cibles sont dans un sous-dossier, jamais à la racine.
    assert all("/" in c for c in cibles)


# ── 2. Le mode d'emploi n'est pas une consigne ────────────────────────────────


async def test_le_gabarit_seul_ne_declenche_rien(boite: BoiteReception) -> None:
    """Le mode d'emploi vit dans une citation, donc hors de portée de la lecture.

    Deux pièges se sont refermés ici avant que ce test ne passe. Les exemples
    (`retiens : je passe au thé vert le matin`) étaient exécutés comme de vraies
    consignes — une préférence inventée toutes les dix minutes, indéfiniment.
    Puis, l'explication écrite en texte normal se retrouvait signalée « pas
    comprise » à chaque passe : le fichier se plaignait de son propre contenu.
    """
    boite.creer_si_absente()

    resultat = await boite.traiter()

    assert (resultat.appliquees, resultat.ignorees, resultat.incomprises) == (0, 0, 0)


def test_un_bloc_de_code_colle_reste_inerte() -> None:
    """On colle volontiers un extrait dans une note ; il ne doit pas s'exécuter."""
    from crush.providers.memory.boite_reception import _lire

    lecture = _lire(
        "oublie ^fact-00c2b7c5e5\n"
        "```\n"
        "retiens : ceci est un exemple copié, pas une consigne\n"
        "```\n"
    )

    assert len(lecture.instructions) == 1
    assert lecture.instructions[0].action == "oublie"
    assert lecture.incomprises == []


async def test_le_gabarit_ne_fait_appeler_aucune_extraction(
    kernel: MemoryKernel, mirror: MemoryMirror
) -> None:
    ingest = _IngestFactice()
    boite = BoiteReception(kernel, mirror, ingest=ingest)
    boite.creer_si_absente()

    await boite.traiter()

    assert ingest.appels == []


# ── 3. Rien ne se perd ────────────────────────────────────────────────────────


async def test_une_ligne_incomprise_est_conservee_avec_sa_raison(
    boite: BoiteReception,
) -> None:
    _ecrire(boite, "j'aime pas le café en fait")

    resultat = await boite.traiter()

    contenu = boite.chemin.read_text(encoding="utf-8")
    assert resultat.incomprises == 1
    assert "j'aime pas le café en fait" in contenu
    assert "Pas comprises" in contenu
    assert "ne commence pas par" in contenu


async def test_une_ligne_incomprise_n_est_pas_rejouee(boite: BoiteReception) -> None:
    """Sinon le même échec reviendrait à chaque passe et le fichier grossirait seul."""
    _ecrire(boite, "j'aime pas le café en fait")
    await boite.traiter()

    seconde = await boite.traiter()

    assert seconde.incomprises == 0
    # Conservée une seule fois, pas dupliquée à chaque relecture.
    contenu = boite.chemin.read_text(encoding="utf-8")
    assert contenu.count("j'aime pas le café en fait") == 1


async def test_ce_qui_est_traite_n_est_pas_reexecute(
    kernel: MemoryKernel, mirror: MemoryMirror
) -> None:
    """Le compte rendu contient la consigne : le relire la rejouerait."""
    ingest = _IngestFactice()
    boite = BoiteReception(kernel, mirror, ingest=ingest)
    _ecrire(boite, "retiens : je cours le dimanche")
    await boite.traiter()

    seconde = await boite.traiter()

    assert seconde.appliquees == 0
    assert len(ingest.appels) == 1


async def test_le_fait_vise_par_faux_est_cite_dans_le_compte_rendu(
    kernel: MemoryKernel, boite: BoiteReception
) -> None:
    """On doit pouvoir constater CE qui a changé, pas seulement que ça a marché."""
    kernel.insert_fact(_fait(obj="café"))
    _ecrire(boite, "faux ^fact-00c2b7c5e5 : thé vert")

    await boite.traiter()

    assert "thé vert" in boite.chemin.read_text(encoding="utf-8")


# ── 4. Une boîte vide n'écrit rien ────────────────────────────────────────────


async def test_une_boite_vide_ne_reecrit_pas_le_fichier(boite: BoiteReception) -> None:
    """C'est ce qui rend une synchronisation bidirectionnelle sûre.

    Réécrire toutes les dix minutes, c'est offrir à Syncthing 144 occasions par
    jour de fabriquer un fichier de conflit avec le téléphone. Ici, en régime
    normal, la Pi ne touche pas au fichier.
    """
    boite.creer_si_absente()
    empreinte = boite.chemin.stat().st_mtime_ns
    contenu = boite.chemin.read_text(encoding="utf-8")

    await boite.traiter()
    await boite.traiter()

    assert boite.chemin.stat().st_mtime_ns == empreinte
    assert boite.chemin.read_text(encoding="utf-8") == contenu


async def test_la_boite_est_creee_si_absente(boite: BoiteReception) -> None:
    assert not boite.chemin.exists()

    assert boite.creer_si_absente() is True
    assert boite.chemin.exists()
    assert boite.creer_si_absente() is False


def test_le_mode_d_emploi_est_dans_le_fichier(boite: BoiteReception) -> None:
    """On l'ouvre sur un téléphone, loin de ce dépôt : la doc doit y être."""
    boite.creer_si_absente()
    contenu = boite.chemin.read_text(encoding="utf-8")

    assert "faux" in contenu and "oublie" in contenu and "retiens" in contenu


# ── 5. Les trois verbes ───────────────────────────────────────────────────────


async def test_faux_corrige_le_fait_vise(kernel: MemoryKernel, boite: BoiteReception) -> None:
    kernel.insert_fact(_fait(obj="café"))
    _ecrire(boite, "faux ^fact-00c2b7c5e5 : thé vert")

    resultat = await boite.traiter()

    assert resultat.appliquees == 1
    fait = kernel.get_fact("fact_00c2b7c5e5")
    assert fait is not None and fait.object == "thé vert"
    assert fait.status is FactStatus.ACTIVE


async def test_oublie_archive_sans_supprimer(kernel: MemoryKernel, boite: BoiteReception) -> None:
    """Un souvenir qu'on demande d'oublier est celui dont on veut garder la trace."""
    kernel.insert_fact(_fait())
    _ecrire(boite, "oublie ^fact-00c2b7c5e5")

    resultat = await boite.traiter()

    assert resultat.appliquees == 1
    fait = kernel.get_fact("fact_00c2b7c5e5")
    assert fait is not None, "le fait ne doit pas disparaître de la base"
    assert fait.status is FactStatus.ARCHIVED


async def test_retiens_passe_par_la_chaine_d_extraction(
    kernel: MemoryKernel, mirror: MemoryMirror
) -> None:
    """La même chaîne que les conversations : garde-fou persona inclus."""
    ingest = _IngestFactice(nouveaux=2)
    boite = BoiteReception(kernel, mirror, ingest=ingest)
    _ecrire(boite, "retiens : je passe au thé vert le matin")

    resultat = await boite.traiter()

    assert ingest.appels == ["je passe au thé vert le matin"]
    assert resultat.appliquees == 1
    assert resultat.retenus == 2


async def test_toute_correction_laisse_une_trace_human_correction(
    kernel: MemoryKernel, boite: BoiteReception
) -> None:
    """L'historique ne doit pas distinguer Obsidian d'une correction dictée."""
    kernel.insert_fact(_fait())
    avant = kernel.count_events()
    _ecrire(boite, "oublie ^fact-00c2b7c5e5")

    await boite.traiter()

    assert kernel.count_events() > avant


# ── 6. Ce qui rate, et comment ────────────────────────────────────────────────


async def test_un_fait_disparu_est_signale_sans_casser(boite: BoiteReception) -> None:
    _ecrire(boite, "oublie ^fact-deadbeef")

    resultat = await boite.traiter()

    assert resultat.appliquees == 0
    assert resultat.ignorees == 1
    assert "aucun fait" in boite.chemin.read_text(encoding="utf-8")


async def test_retiens_sans_extraction_branchee_le_dit(
    kernel: MemoryKernel, mirror: MemoryMirror
) -> None:
    """Sans LLM de fond, les deux autres verbes marchent quand même."""
    boite = BoiteReception(kernel, mirror, ingest=None)
    _ecrire(boite, "retiens : je cours le dimanche")

    resultat = await boite.traiter()

    assert resultat.ignorees == 1
    assert "pas branchée" in boite.chemin.read_text(encoding="utf-8")


async def test_une_extraction_qui_casse_ne_perd_pas_le_reste(
    kernel: MemoryKernel, mirror: MemoryMirror
) -> None:
    kernel.insert_fact(_fait())
    boite = BoiteReception(kernel, mirror, ingest=_IngestQuiCasse())
    _ecrire(boite, "retiens : quelque chose", "oublie ^fact-00c2b7c5e5")

    resultat = await boite.traiter()

    assert resultat.ignorees == 1
    assert resultat.appliquees == 1, "la seconde consigne doit être exécutée malgré la première"
    fait = kernel.get_fact("fact_00c2b7c5e5")
    assert fait is not None and fait.status is FactStatus.ARCHIVED


async def test_le_miroir_est_regenere_apres_une_correction(
    kernel: MemoryKernel, mirror: MemoryMirror, boite: BoiteReception
) -> None:
    """Sinon on corrige à 10 h et le fichier affiche l'ancienne valeur jusqu'à 3 h."""
    kernel.insert_fact(_fait(obj="café"))
    mirror.export()
    rendu = mirror.root / "user/preferences.md"
    assert "café" in rendu.read_text(encoding="utf-8")

    _ecrire(boite, "faux ^fact-00c2b7c5e5 : thé vert")
    await boite.traiter()

    assert "thé vert" in rendu.read_text(encoding="utf-8")


# ── 7. La syntaxe permissive ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "ligne",
    [
        "faux ^fact-00c2b7c5e5 : thé",
        "- faux ^fact-00c2b7c5e5 : thé",  # puce ajoutée par Obsidian
        "* faux ^fact-00c2b7c5e5 : thé",
        "- [ ] faux ^fact-00c2b7c5e5 : thé",  # case à cocher
        "- [x] faux ^fact-00c2b7c5e5 : thé",
        "Faux ^fact-00c2b7c5e5 : thé",  # majuscule automatique du clavier
        "FAUX ^fact-00c2b7c5e5 : thé",
        "faux: ^fact-00c2b7c5e5 thé",
        "faux fact_00c2b7c5e5 : thé",  # identifiant copié depuis l'API
        "non ^fact-00c2b7c5e5 : thé",
        "corrige ^fact-00c2b7c5e5 : thé",
    ],
)
def test_les_formes_qu_on_tape_vraiment_sont_comprises(ligne: str) -> None:
    instr, raison = _analyser(ligne)

    assert instr is not None, f"non comprise : {raison}"
    assert instr.action == "corrige"
    assert id_depuis_ancre(instr.ancre) == "fact_00c2b7c5e5"
    assert instr.texte == "thé"


@pytest.mark.parametrize(
    ("ligne", "attendu"),
    [
        ("oublie ^fact-00c2b7c5e5", "oublie"),
        ("oublié ^fact-00c2b7c5e5", "oublie"),
        ("- supprime ^fact-00c2b7c5e5", "oublie"),
        ("efface ^fact-00c2b7c5e5", "oublie"),
        ("retiens : je cours", "retiens"),
        ("retiens je cours", "retiens"),
        ("note : je cours", "retiens"),
        ("- Retiens : je cours", "retiens"),
    ],
)
def test_les_autres_verbes_et_leurs_variantes(ligne: str, attendu: str) -> None:
    instr, _raison = _analyser(ligne)

    assert instr is not None and instr.action == attendu


@pytest.mark.parametrize(
    ("ligne", "extrait_de_la_raison"),
    [
        ("j'aime pas le café", "ne commence pas par"),
        ("faux : je bois du thé", "aucune référence"),
        ("oublie ma préférence de café", "aucune référence"),
        ("faux ^fact-00c2b7c5e5", "sans la version correcte"),
        ("retiens :", "rien à retenir"),
    ],
)
def test_une_ligne_ambigue_dit_pourquoi_elle_ne_passe_pas(
    ligne: str, extrait_de_la_raison: str
) -> None:
    """Une ligne rejetée sans raison est une ligne qu'on ne saura pas réparer."""
    instr, raison = _analyser(ligne)

    assert instr is None
    assert extrait_de_la_raison in raison


# ── 8. L'ancre ────────────────────────────────────────────────────────────────


def test_l_ancre_est_valide_pour_obsidian() -> None:
    """Obsidian n'accepte ni underscore ni point dans un `^identifiant`."""
    ancre = ancre_de_fait("fact_00c2b7c5e5")

    assert "_" not in ancre
    assert ancre.replace("-", "").isalnum()


@pytest.mark.parametrize("forme", ["fact-00c2b7c5e5", "^fact-00c2b7c5e5", "fact_00c2b7c5e5"])
def test_l_ancre_revient_toujours_au_meme_fait(forme: str) -> None:
    assert id_depuis_ancre(forme) == "fact_00c2b7c5e5"


def test_le_miroir_affiche_une_ancre_copiable(kernel: MemoryKernel, mirror: MemoryMirror) -> None:
    """Sans prise sur un fait, rien ne peut être corrigé depuis Obsidian."""
    kernel.insert_fact(_fait(category="preference"))

    mirror.export()

    rendu = (mirror.root / "user/preferences.md").read_text(encoding="utf-8")
    assert "^fact-00c2b7c5e5" in rendu
