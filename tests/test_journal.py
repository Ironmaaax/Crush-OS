# Copyright (C) 2026 Maxime Song

"""« Qu'est-ce que j'ai fait mardi ? » — la mémoire sur l'axe du temps.

La mémoire retenait des faits sans âge utile. La table `events` portait pourtant
chaque échange avec sa date, et un index sur `created_at` depuis le premier jour :
aucune méthode ne permettait de la lire par période. Le journal était là, indexé,
et inatteignable.

Ce qui est défendu ici, dans l'ordre de ce qui coûterait le plus cher :

1. une date illisible est REFUSÉE et non ramenée à aujourd'hui — répondre sur la
   mauvaise journée est pire que ne pas répondre ;
2. une période inclut bien ses bornes attendues, sinon on perd un jour entier ;
3. un souvenir ancien reconfirmé compte dans la journée où on en a parlé ;
4. une période vide ne se lit pas comme « je n'ai rien fait ».
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from crush.capabilities.tools.journal import MemoryJournalTool, _lire_date
from crush.kernel.schemas import FactStatus
from crush.providers.memory.kernel import MemoryKernel
from crush.providers.memory.schemas import DecayPolicy, Fact


@pytest.fixture
def kernel(tmp_path: Path) -> MemoryKernel:
    return MemoryKernel(tmp_path / "k.db")


@pytest.fixture
def outil(kernel: MemoryKernel) -> MemoryJournalTool:
    return MemoryJournalTool(kernel=kernel)  # type: ignore[arg-type]


def _fait(kernel: MemoryKernel, fid: str, obj: str, cree: datetime, vu: datetime) -> None:
    kernel.insert_fact(
        Fact(
            id=fid,
            subject="max",
            predicate="prefers",
            object=obj,
            category="preference",
            status=FactStatus.ACTIVE,
            confidence=0.8,
            support_count=1,
            decay_policy=DecayPolicy.MEDIUM,
            importance=0.6,
            created_at=cree,
            last_seen_at=vu,
            updated_at=vu,
        )
    )


# ── 1. Une date illisible est refusée ─────────────────────────────────────────


@pytest.mark.parametrize("saisie", ["mardi", "la semaine derniere", "32/13/2026", "bientôt", "??"])
async def test_une_date_illisible_est_refusee(outil: MemoryJournalTool, saisie: str) -> None:
    """Ramenée à aujourd'hui, elle répondrait sur la mauvaise journée en silence —
    et une réponse fausse sur une question de mémoire ne se remarque pas."""
    r = await outil.execute(depuis=saisie)

    assert r.is_error
    assert "illisible" in r.content
    assert "2026-08-19" in r.content, "le refus doit montrer les formes admises"


async def test_une_fin_illisible_est_refusee_aussi(outil: MemoryJournalTool) -> None:
    r = await outil.execute(depuis="hier", jusqu_a="n'importe quoi")

    assert r.is_error


async def test_une_periode_a_l_envers_est_refusee(outil: MemoryJournalTool) -> None:
    r = await outil.execute(depuis="hier", jusqu_a="-9j")

    assert r.is_error
    assert "précède" in r.content


# ── 2. Les formes qu'un modèle écrit vraiment ─────────────────────────────────


@pytest.mark.parametrize(
    ("saisie", "jours_avant"),
    [("aujourd_hui", 0), ("aujourd'hui", 0), ("hier", 1), ("avant-hier", 2), ("-7j", 7),
     ("-1 jour", 1), ("- 3 j", 3)],
)
def test_les_dates_relatives_sont_comprises(saisie: str, jours_avant: int) -> None:
    attendu = (datetime.now() - timedelta(days=jours_avant)).date()

    lu = _lire_date(saisie, datetime.now())

    assert lu is not None and lu.date() == attendu


def test_une_date_sans_annee_reste_dans_l_annee_courante() -> None:
    """`%d/%m` tombe en 1900 : la période serait vide et l'on croirait n'avoir
    rien fait ce jour-là."""
    lu = _lire_date("19/08", datetime.now())

    assert lu is not None and lu.year == datetime.now().year


@pytest.mark.parametrize("saisie", ["2026-08-19", "19/08/2026", "2026-08-19T14:30"])
def test_les_dates_absolues_sont_comprises(saisie: str) -> None:
    lu = _lire_date(saisie, datetime.now())

    assert lu is not None and lu.year == 2026 and lu.month == 8 and lu.day == 19


# ── 3. Les bornes de la période ───────────────────────────────────────────────


async def test_sans_fin_la_journee_entiere_est_couverte(
    outil: MemoryJournalTool, kernel: MemoryKernel
) -> None:
    hier = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    kernel.log_event("exchange", "test", "quelque chose hier")
    # L'event vient d'être écrit avec la date du jour : il ne doit PAS sortir.
    r = await outil.execute(depuis=hier.strftime("%Y-%m-%d"))

    assert "Aucun événement" in r.content


async def test_une_fin_sans_heure_inclut_ce_jour_la(
    outil: MemoryJournalTool, kernel: MemoryKernel
) -> None:
    """« du 19 au 20 » doit inclure le 20, sinon on répond à côté d'un jour."""
    kernel.log_event("exchange", "test", "aujourd'hui")
    aujourd_hui = datetime.now().strftime("%Y-%m-%d")

    r = await outil.execute(depuis=aujourd_hui, jusqu_a=aujourd_hui)

    assert "aujourd'hui" in r.content


async def test_les_evenements_sortent_en_ordre_chronologique(
    outil: MemoryJournalTool, kernel: MemoryKernel
) -> None:
    """On relit une journée dans le sens où elle s'est déroulée."""
    kernel.log_event("exchange", "test", "premier")
    kernel.log_event("exchange", "test", "deuxieme")
    kernel.log_event("exchange", "test", "troisieme")

    r = await outil.execute(depuis="aujourd_hui")

    assert r.content.index("premier") < r.content.index("deuxieme") < r.content.index("troisieme")


# ── 4. Ce qui compte dans une journée ─────────────────────────────────────────


async def test_un_souvenir_ancien_reconfirme_compte_aujourd_hui(
    outil: MemoryJournalTool, kernel: MemoryKernel
) -> None:
    """Ne compter que les créations donnerait une journée artificiellement vide
    dès qu'on a surtout parlé de choses déjà sues."""
    vieux = datetime.now() - timedelta(days=90)
    _fait(kernel, "fact_vieux", "café noir", cree=vieux, vu=datetime.now())

    r = await outil.execute(depuis="aujourd_hui")

    assert "café noir" in r.content
    assert "[revu]" in r.content


async def test_un_souvenir_appris_aujourd_hui_est_marque_nouveau(
    outil: MemoryJournalTool, kernel: MemoryKernel
) -> None:
    maintenant = datetime.now()
    _fait(kernel, "fact_neuf", "thé vert", cree=maintenant, vu=maintenant)

    r = await outil.execute(depuis="aujourd_hui")

    assert "[nouveau] max prefers thé vert" in r.content


async def test_le_bruit_technique_est_ecarte(
    outil: MemoryJournalTool, kernel: MemoryKernel
) -> None:
    """« Qu'est-ce que j'ai fait » ne parle pas de ce que l'assistant a fait de
    lui-même. Ces lignes ont leur place dans l'audit, pas dans la réponse."""
    kernel.log_event("session_summary", "auto", "resume interne")
    kernel.log_event("exchange", "test", "vraie conversation")

    r = await outil.execute(depuis="aujourd_hui")

    assert "vraie conversation" in r.content
    assert "resume interne" not in r.content


async def test_une_periode_vide_ne_se_lit_pas_comme_une_absence(
    outil: MemoryJournalTool,
) -> None:
    """Sans cette précision, une question sur l'an dernier renverrait un vide
    qu'on lirait comme « je n'ai rien fait »."""
    r = await outil.execute(depuis="2020-01-01")

    assert not r.is_error
    assert "premier échange enregistré" in r.content


async def test_un_contenu_tres_long_est_tronque(
    outil: MemoryJournalTool, kernel: MemoryKernel
) -> None:
    """Le journal alimente un contexte de modèle : trente échanges entiers le
    rempliraient à eux seuls."""
    kernel.log_event("exchange", "test", "x" * 5000)

    r = await outil.execute(depuis="aujourd_hui")

    assert "…" in r.content
    assert len(r.content) < 2000


async def test_la_limite_est_bornee(outil: MemoryJournalTool, kernel: MemoryKernel) -> None:
    for i in range(12):
        kernel.log_event("exchange", "test", f"message {i}")

    r = await outil.execute(depuis="aujourd_hui", limite=3)

    assert "3 événement(s)" in r.content


async def test_une_limite_absurde_ne_casse_rien(
    outil: MemoryJournalTool, kernel: MemoryKernel
) -> None:
    kernel.log_event("exchange", "test", "un message")

    r = await outil.execute(depuis="aujourd_hui", limite="beaucoup")  # type: ignore[arg-type]

    assert not r.is_error


# ── 5. La lecture par période, côté Kernel ────────────────────────────────────


def test_le_kernel_borne_la_periode_par_le_haut(kernel: MemoryKernel) -> None:
    """La fin est EXCLUE : sinon deux journées consécutives se chevaucheraient
    d'un événement, et on le compterait deux fois."""
    kernel.log_event("exchange", "test", "maintenant")
    minuit = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    avant = kernel.list_events_between(minuit - timedelta(days=1), minuit)
    pendant = kernel.list_events_between(minuit, minuit + timedelta(days=1))

    assert avant == []
    assert len(pendant) == 1


def test_le_kernel_exclut_les_types_demandes(kernel: MemoryKernel) -> None:
    kernel.log_event("exchange", "test", "garde")
    kernel.log_event("session_summary", "test", "jette")
    minuit = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    gardes = kernel.list_events_between(
        minuit, minuit + timedelta(days=1), types_exclus=("session_summary",)
    )

    assert [e.content for e in gardes] == ["garde"]


def test_le_kernel_trouve_un_fait_par_sa_creation_ou_sa_derniere_vue(
    kernel: MemoryKernel,
) -> None:
    minuit = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    hier = minuit - timedelta(days=1)
    # Créé hier, revu hier : ne doit pas sortir aujourd'hui.
    _fait(kernel, "fact_hier", "vieux", cree=hier, vu=hier)
    # Créé hier, revu aujourd'hui : doit sortir aujourd'hui.
    _fait(kernel, "fact_mixte", "revu", cree=hier, vu=datetime.now())

    trouves = kernel.list_facts_seen_between(minuit, minuit + timedelta(days=1))

    assert [f.id for f in trouves] == ["fact_mixte"]
