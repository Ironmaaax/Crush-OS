# Copyright (C) 2026 Maxime Song

"""Archivage de la mémoire — planifiable, et qui rend compte de ce qu'il a fait.

POURQUOI CE MODULE EXISTE

La logique d'archivage était correcte mais vivait dans `scripts/sauvegarde_memoire.py`,
que **rien n'appelait**. La mémoire — la seule donnée du projet qui n'existe nulle
part ailleurs — tenait donc en un exemplaire, sur la carte d'une machine qui écrit
24 heures sur 24. La page Écosystème signalait le risque en rouge, ce qui suppose
d'ouvrir la page pour l'apprendre.

Elle est ici, en L1, pour que `engine/background/scheduler.py` puisse l'appeler à
travers le Protocol `MemoryBackup` : l'engine n'a pas le droit d'importer
`providers` (RÈGLE 3 du CDC §2.2), la dépendance est donc injectée par `bootstrap`.

DEUX CHOIX QUI COMPTENT

1. L'archivage tourne dans un thread (`asyncio.to_thread`). Compresser une mémoire
   de plusieurs dizaines de Mo prend plusieurs secondes sur un Pi. Exécuté dans la
   boucle asyncio, il gèlerait l'API, le pipeline vocal et les routines pendant
   tout ce temps — une sauvegarde qui rend l'assistant muet chaque nuit serait
   désactivée en une semaine.

2. Une copie hors machine est possible (`backup_copy_to`). Sans elle l'archive vit
   sur le support même qu'elle protège : utile contre une fausse manœuvre, inutile
   contre la panne du support. C'est la moitié du travail qui compte.

Le `.env` reste délibérément EXCLU : une archive se recopie, se transfère, se
laisse traîner, et n'a aucune raison de transporter des clés d'API.
"""

from __future__ import annotations

import asyncio
import fnmatch
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

from loguru import logger

from crush.kernel.schemas import ResultatSauvegarde

# Exclus de l'archive : volumineux et intégralement reconstructibles depuis la base.
_EXCLUS = {"vector_index", "rpc_workspace"}

# Noms qui ne doivent JAMAIS entrer dans une archive, où qu'ils se trouvent.
# Le `.env` vit à la racine du projet, donc hors du périmètre archivé — mais
# « hors périmètre » est une propriété accidentelle, pas une garantie. Il suffit
# qu'un jeton atterrisse un jour dans memory_data/ pour qu'une archive destinée
# à une clé USB ou un NAS transporte des identifiants. Le filtre rend la
# promesse du module vérifiable au lieu de la supposer.
_NOMS_SENSIBLES = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*token*.json",
    "*credentials*.json",
)


def _est_sensible(nom: str) -> bool:
    return any(fnmatch.fnmatch(nom, motif) for motif in _NOMS_SENSIBLES)

_MOTIF = "memoire-*.tar.gz"


class SauvegardeMemoire:
    """Archive `memory_data/`, purge les anciennes, copie hors machine si demandé."""

    def __init__(
        self,
        source: Path,
        destination: Path,
        conserver: int = 7,
        copier_vers: str = "",
    ) -> None:
        self._source = source
        self._destination = destination
        # Sans purge, une sauvegarde quotidienne finit par remplir la carte qu'elle
        # est censée protéger. Le plafond est le nombre d'archives, pas une durée :
        # une machine éteinte trois semaines ne doit pas perdre ses archives.
        self._conserver = max(1, conserver)
        self._copier_vers = copier_vers.strip()

    # ── Lecture d'état ────────────────────────────────────────────────────────

    def derniere(self) -> Path | None:
        """L'archive la plus récente, ou None. Sert aussi à la page Écosystème."""
        if not self._destination.is_dir():
            return None
        archives = sorted(self._destination.glob(_MOTIF))
        return archives[-1] if archives else None

    def age_heures(self) -> float | None:
        """Âge de la dernière archive en heures, None s'il n'y en a aucune.

        L'âge est ce qui compte, pas l'existence : une archive de trois mois donne
        un voyant vert et une fausse assurance.
        """
        derniere = self.derniere()
        if derniere is None:
            return None
        age = datetime.now().timestamp() - derniere.stat().st_mtime
        return age / 3600

    # ── Exécution ─────────────────────────────────────────────────────────────

    async def sauvegarder(self) -> ResultatSauvegarde:
        """Point d'entrée du Protocol `MemoryBackup`. Ne bloque pas la boucle."""
        return await asyncio.to_thread(self._sauvegarder_bloquant)

    def _sauvegarder_bloquant(self) -> ResultatSauvegarde:
        if not self._source.is_dir():
            return ResultatSauvegarde(
                reussie=False, erreur=f"{self._source} n'existe pas — rien à sauvegarder"
            )

        try:
            self._destination.mkdir(parents=True, exist_ok=True)
            horodatage = datetime.now().strftime("%Y-%m-%d_%H%M")
            archive = self._destination / f"memoire-{horodatage}.tar.gz"
            bases = self._ecrire_archive(archive)
        except (OSError, tarfile.TarError) as exc:
            return ResultatSauvegarde(reussie=False, erreur=f"{type(exc).__name__}: {exc}")

        octets = archive.stat().st_size
        purgees = self._purger()
        copiee, erreur_copie = self._copier_hors_machine(archive)

        logger.info(
            "Mémoire sauvegardée",
            archive=archive.name,
            mo=round(octets / 1024**2, 1),
            bases=bases,
            purgees=purgees,
            hors_machine=copiee,
        )
        return ResultatSauvegarde(
            reussie=True,
            archive=archive.name,
            octets=octets,
            bases_instantanees=bases,
            purgees=purgees,
            copiee_hors_machine=copiee,
            # Une copie hors machine ratée n'invalide pas l'archive locale : on la
            # signale sans faire échouer la passe, sinon on perdrait les deux.
            erreur=erreur_copie,
        )

    # ── Détail ────────────────────────────────────────────────────────────────

    def _ecrire_archive(self, archive: Path) -> int:
        bases = 0
        with tempfile.TemporaryDirectory() as tmp:
            instantanes = Path(tmp)
            with tarfile.open(archive, "w:gz") as tar:
                for chemin in sorted(self._source.rglob("*")):
                    if not chemin.is_file():
                        continue
                    relatif = chemin.relative_to(self._source)
                    if relatif.parts and relatif.parts[0] in _EXCLUS:
                        continue
                    # Les annexes -wal et -shm n'ont pas de sens hors de leur base :
                    # l'instantané ci-dessous les intègre déjà.
                    if chemin.name.endswith(("-wal", "-shm")):
                        continue
                    if _est_sensible(chemin.name):
                        logger.warning(
                            "Fichier sensible exclu de l'archive", fichier=str(relatif)
                        )
                        continue

                    if chemin.suffix == ".db":
                        copie = instantanes / relatif.name
                        if _instantane_sqlite(chemin, copie):
                            tar.add(copie, arcname=str(relatif))
                            bases += 1
                            continue
                        logger.warning(
                            "Instantané SQLite impossible, copie brute", base=str(relatif)
                        )
                    tar.add(chemin, arcname=str(relatif))
        return bases

    def _purger(self) -> int:
        archives = sorted(self._destination.glob(_MOTIF))
        trop = archives[: -self._conserver] if len(archives) > self._conserver else []
        for vieille in trop:
            vieille.unlink(missing_ok=True)
        return len(trop)

    def _copier_hors_machine(self, archive: Path) -> tuple[bool, str | None]:
        if not self._copier_vers:
            return False, None
        cible = Path(self._copier_vers).expanduser()
        try:
            cible.mkdir(parents=True, exist_ok=True)
            shutil.copy2(archive, cible / archive.name)
        except OSError as exc:
            # Cas courant et attendu : partage réseau non monté, clé USB absente.
            # L'archive locale existe, elle ; on ne perd rien à le dire calmement.
            logger.warning("Copie hors machine impossible", cible=str(cible), erreur=str(exc))
            return False, f"copie hors machine impossible vers {cible} : {exc}"
        return True, None


def _instantane_sqlite(source: Path, cible: Path) -> bool:
    """Copie une base SQLite en cours d'utilisation, sans la corrompre.

    Le service écrit en permanence, en mode WAL. Copier le fichier à l'octet
    pendant une transaction produit une archive qui s'ouvre mais dont il manque la
    fin — une sauvegarde qu'on croit avoir et qui n'existe pas. L'API `backup` de
    SQLite prend un instantané cohérent.
    """
    src: sqlite3.Connection | None = None
    dst: sqlite3.Connection | None = None
    try:
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        dst = sqlite3.connect(cible)
        src.backup(dst)
        return True
    except sqlite3.Error:
        return False
    finally:
        # `with sqlite3.connect(...)` ne FERME PAS la connexion : il ne gère que
        # la transaction. Sans fermeture explicite, le fichier reste ouvert, et
        # Windows refuse de supprimer le dossier temporaire qui le contient — la
        # sauvegarde echouait alors entierement. Piege present dans la version
        # d'origine du script, invisible tant qu'il ne tournait que sur le Pi.
        for connexion in (dst, src):
            if connexion is not None:
                connexion.close()
