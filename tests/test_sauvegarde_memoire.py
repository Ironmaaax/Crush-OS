# Copyright (C) 2026 Maxime Song

"""Sauvegarde de la mémoire — `providers/memory/sauvegarde.py` et sa planification.

Ce qui est gardé ici n'est pas « le tar se crée » mais les quatre propriétés qui
font la différence entre une sauvegarde et l'illusion d'en avoir une :

- le `.env` n'entre PAS dans l'archive — elle se recopie et se transfère ;
- les bases SQLite passent par un instantané cohérent, pas une copie à l'octet
  d'un fichier en cours d'écriture ;
- la purge borne l'occupation, sinon la sauvegarde remplit le support qu'elle
  protège ;
- un échec REMONTE dans la conversation, et un succès reste silencieux — une
  notification quotidienne « tout va bien » finit par ne plus être lue.
"""

from __future__ import annotations

import asyncio
import sqlite3
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from crush.engine.background import scheduler as module_scheduler
from crush.engine.background.scheduler import Scheduler
from crush.kernel.schemas import ResultatSauvegarde
from crush.providers.memory.sauvegarde import SauvegardeMemoire


def _memoire_factice(racine: Path) -> Path:
    """Une mémoire crédible : une base SQLite en WAL, un .env, un dossier exclu."""
    source = racine / "memory_data"
    source.mkdir()
    (source / "topics").mkdir()
    (source / "topics" / "sport.md").write_text("# Sport\n", encoding="utf-8")

    base = source / "crush_memory.db"
    conn = sqlite3.connect(base)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE facts (id TEXT, contenu TEXT)")
    conn.execute("INSERT INTO facts VALUES ('1', 'court le dimanche')")
    conn.commit()
    conn.close()

    (source / ".env").write_text("ANTHROPIC_API_KEY=sk-tres-secret\n", encoding="utf-8")
    (source / "vector_index").mkdir()
    (source / "vector_index" / "gros.bin").write_bytes(b"\x00" * 4096)
    return source


def _noms_dans(archive: Path) -> set[str]:
    with tarfile.open(archive, "r:gz") as tar:
        return set(tar.getnames())


# ── Le provider ──────────────────────────────────────────────────────────────


async def test_archive_ce_qui_compte_et_rien_d_autre(tmp_path: Path) -> None:
    source = _memoire_factice(tmp_path)
    s = SauvegardeMemoire(source=source, destination=tmp_path / "sauvegardes")

    resultat = await s.sauvegarder()

    assert resultat.reussie
    assert resultat.bases_instantanees == 1
    archive = (tmp_path / "sauvegardes") / str(resultat.archive)
    noms = _noms_dans(archive)
    assert "crush_memory.db" in noms
    assert "topics/sport.md" in noms


async def test_le_env_n_entre_jamais_dans_l_archive(tmp_path: Path) -> None:
    """Une archive se recopie, se transfère, se laisse traîner. Pas les clés d'API."""
    source = _memoire_factice(tmp_path)
    s = SauvegardeMemoire(source=source, destination=tmp_path / "sauvegardes")

    resultat = await s.sauvegarder()
    archive = (tmp_path / "sauvegardes") / str(resultat.archive)

    with tarfile.open(archive, "r:gz") as tar:
        contenus = b"".join(
            tar.extractfile(m).read()  # type: ignore[union-attr]
            for m in tar.getmembers()
            if m.isfile()
        )
    assert b"sk-tres-secret" not in contenus


async def test_les_donnees_reconstructibles_sont_exclues(tmp_path: Path) -> None:
    source = _memoire_factice(tmp_path)
    s = SauvegardeMemoire(source=source, destination=tmp_path / "sauvegardes")

    resultat = await s.sauvegarder()
    noms = _noms_dans((tmp_path / "sauvegardes") / str(resultat.archive))

    assert not any(n.startswith("vector_index") for n in noms)


async def test_la_base_archivee_est_relisible(tmp_path: Path) -> None:
    """Le vrai test d'une sauvegarde : ce qui en sort s'ouvre et contient les données.

    Une copie à l'octet d'une base en écriture produit un fichier qui s'ouvre mais
    dont il manque la fin. C'est ce cas que l'instantané SQLite évite.
    """
    source = _memoire_factice(tmp_path)
    s = SauvegardeMemoire(source=source, destination=tmp_path / "sauvegardes")
    resultat = await s.sauvegarder()

    extrait = tmp_path / "extrait"
    with tarfile.open((tmp_path / "sauvegardes") / str(resultat.archive), "r:gz") as tar:
        tar.extract("crush_memory.db", path=extrait, filter="data")

    conn = sqlite3.connect(extrait / "crush_memory.db")
    lignes = conn.execute("SELECT contenu FROM facts").fetchall()
    conn.close()
    assert lignes == [("court le dimanche",)]


async def test_la_purge_borne_l_occupation(tmp_path: Path) -> None:
    """Sans purge, la sauvegarde quotidienne remplit le support qu'elle protège."""
    source = _memoire_factice(tmp_path)
    dest = tmp_path / "sauvegardes"
    dest.mkdir()
    for i in range(5):
        (dest / f"memoire-2020-01-0{i + 1}_0000.tar.gz").write_bytes(b"vieux")

    s = SauvegardeMemoire(source=source, destination=dest, conserver=3)
    resultat = await s.sauvegarder()

    assert resultat.purgees == 3
    assert len(list(dest.glob("memoire-*.tar.gz"))) == 3


async def test_copie_hors_machine(tmp_path: Path) -> None:
    source = _memoire_factice(tmp_path)
    ailleurs = tmp_path / "ailleurs"
    s = SauvegardeMemoire(
        source=source, destination=tmp_path / "sauvegardes", copier_vers=str(ailleurs)
    )

    resultat = await s.sauvegarder()

    assert resultat.copiee_hors_machine
    assert (ailleurs / str(resultat.archive)).is_file()


async def test_une_copie_hors_machine_ratee_ne_perd_pas_l_archive_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Partage réseau non monté, clé USB absente : cas courant, pas catastrophe.

    Faire échouer toute la passe ferait perdre les deux archives au lieu d'une.
    """
    source = _memoire_factice(tmp_path)

    def refuse(*_a: object, **_k: object) -> None:
        raise OSError("destination non montée")

    monkeypatch.setattr("crush.providers.memory.sauvegarde.shutil.copy2", refuse)
    s = SauvegardeMemoire(
        source=source, destination=tmp_path / "sauvegardes", copier_vers=str(tmp_path / "nfs")
    )

    resultat = await s.sauvegarder()

    assert resultat.reussie  # l'archive locale existe
    assert not resultat.copiee_hors_machine
    assert resultat.erreur and "hors machine" in resultat.erreur


async def test_source_absente_echoue_explicitement(tmp_path: Path) -> None:
    s = SauvegardeMemoire(source=tmp_path / "nulle_part", destination=tmp_path / "s")
    resultat = await s.sauvegarder()
    assert not resultat.reussie
    assert resultat.erreur


def test_age_heures_dit_none_sans_archive(tmp_path: Path) -> None:
    """L'âge, pas l'existence : une archive de trois mois ne protège plus."""
    s = SauvegardeMemoire(source=tmp_path, destination=tmp_path / "vide")
    assert s.age_heures() is None


# ── La planification ─────────────────────────────────────────────────────────


def _scheduler(sauvegarde: object, notifications: object) -> Scheduler:
    """Scheduler minimal : le constructeur ne fait qu'affecter ses dépendances."""
    return Scheduler(
        proactive=SimpleNamespace(broadcast=lambda _m: None),  # type: ignore[arg-type]
        auto_dream=SimpleNamespace(),  # type: ignore[arg-type]
        calendar_tool=SimpleNamespace(),  # type: ignore[arg-type]
        settings=SimpleNamespace(backup_hour=4, backup_enabled=True, backup_keep=7),  # type: ignore[arg-type]
        notifications=notifications,  # type: ignore[arg-type]
        sauvegarde=sauvegarde,  # type: ignore[arg-type]
    )


async def _une_passe(sched: Scheduler, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exerce exactement UNE itération de la boucle, puis la laisse se garer.

    La deuxième attente est longue : sans ça, `_seconds_until` à 0 ferait tourner
    la boucle à vide des milliers de fois pendant le test.
    """
    attentes = iter([0])
    monkeypatch.setattr(module_scheduler, "_seconds_until", lambda _h: next(attentes, 3600))
    tache = asyncio.create_task(sched._sauvegarde_loop())
    for _ in range(50):
        await asyncio.sleep(0)
    tache.cancel()


async def test_un_echec_remonte_dans_la_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    """`broadcast` n'atteint que les clients connectés — donc personne, la nuit."""
    recu: list[str] = []
    sauvegarde = SimpleNamespace(
        sauvegarder=lambda: _resultat(ResultatSauvegarde(reussie=False, erreur="disque plein")),
        age_heures=lambda: None,
    )
    sched = _scheduler(sauvegarde, SimpleNamespace(add=recu.append))

    await _une_passe(sched, monkeypatch)

    assert recu, "un échec de sauvegarde n'a rien signalé"
    assert "disque plein" in recu[0]
    assert "un seul exemplaire" in recu[0]


async def test_un_succes_ne_dit_rien(monkeypatch: pytest.MonkeyPatch) -> None:
    """Une notification quotidienne « tout va bien » finit par ne plus être lue."""
    recu: list[str] = []
    sauvegarde = SimpleNamespace(
        sauvegarder=lambda: _resultat(
            ResultatSauvegarde(reussie=True, archive="memoire-x.tar.gz", copiee_hors_machine=True)
        ),
        age_heures=lambda: 1.0,
    )
    sched = _scheduler(sauvegarde, SimpleNamespace(add=recu.append))

    await _une_passe(sched, monkeypatch)

    assert recu == []


async def test_une_copie_hors_machine_ratee_est_signalee(monkeypatch: pytest.MonkeyPatch) -> None:
    """L'archive existe mais reste sur le support qu'elle protège : ça vaut un mot."""
    recu: list[str] = []
    sauvegarde = SimpleNamespace(
        sauvegarder=lambda: _resultat(
            ResultatSauvegarde(
                reussie=True,
                archive="memoire-x.tar.gz",
                copiee_hors_machine=False,
                erreur="copie hors machine impossible vers /mnt/nas : non monté.",
            )
        ),
        age_heures=lambda: 1.0,
    )
    sched = _scheduler(sauvegarde, SimpleNamespace(add=recu.append))

    await _une_passe(sched, monkeypatch)

    assert recu and "même support" in recu[0]


async def test_une_exception_ne_tue_pas_la_boucle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Une passe qui explose ne doit pas emporter la planification des suivantes."""
    recu: list[str] = []

    async def explose() -> ResultatSauvegarde:
        raise RuntimeError("carte SD en lecture seule")

    sched = _scheduler(
        SimpleNamespace(sauvegarder=explose, age_heures=lambda: None),
        SimpleNamespace(add=recu.append),
    )

    await _une_passe(sched, monkeypatch)

    assert recu and "RuntimeError" in recu[0]


async def _resultat(r: ResultatSauvegarde) -> ResultatSauvegarde:
    return r
