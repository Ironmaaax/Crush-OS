# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS,   .
 

"""Outils read_file / find_files : bonne machine annoncée, périmètre tenu.

Ce que ces tests protègent, dans l'ordre d'importance :
  1. les descriptions ne mentent plus sur la machine visée (serveur ≠ poste
     de l'utilisateur) — c'est ce mensonge qui faisait promettre au modèle de
     lire les fichiers de l'utilisateur ;
  2. le périmètre résiste à `..`, aux liens symboliques, aux arborescences
     système et aux fichiers qui portent un nom de secret ;
  3. aucune entrée bizarre (binaire, non-UTF-8, énorme, inexistante) ne
     produit de trace : chaque refus dit quoi faire ensuite.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from crush.capabilities.tools import filesystem
from crush.capabilities.tools.filesystem import (
    _MAX_FILE_SIZE,
    FindFilesTool,
    ReadFileTool,
    _parcourir,
)


class _FauxPermissions:
    """Store de permissions minimal, découplé du singleton persistant.

    Le vrai store écrit dans `memory_data/permissions.json` : le solliciter
    depuis les tests modifierait l'état réel de la machine de développement.
    """

    def __init__(self, *, files: bool) -> None:
        self._files = files

    def get(self, key: str) -> bool:
        return self._files if key == "files" else True


@pytest.fixture(autouse=True)
def _permission_accordee(monkeypatch: pytest.MonkeyPatch) -> None:
    """Accorde la permission « Fichiers » : sinon tout refus testé serait le mauvais."""
    monkeypatch.setattr(filesystem, "_perms", _FauxPermissions(files=True))


def _refuser_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(filesystem, "_perms", _FauxPermissions(files=False))


# ── 1. La machine annoncée ────────────────────────────────────────────────────


@pytest.mark.parametrize("classe", [ReadFileTool, FindFilesTool])
def test_description_ne_promet_plus_le_mac(classe: type, tmp_path: Path) -> None:
    """Aucun des deux outils ne doit plus prétendre agir sur le Mac de l'utilisateur."""
    outil = classe(allowed_roots=[tmp_path])

    assert "Mac" not in outil.description
    assert "remote_pc" in outil.description
    # Le périmètre réel figure dans la description : le modèle n'a pas à le
    # découvrir en échouant.
    assert str(tmp_path) in outil.description


@pytest.mark.parametrize("classe", [ReadFileTool, FindFilesTool])
def test_schema_des_parametres_designe_le_serveur(classe: type, tmp_path: Path) -> None:
    outil = classe(allowed_roots=[tmp_path])
    descriptions = " ".join(
        p.get("description", "") for p in outil.input_schema["properties"].values()
    )
    assert "serveur" in descriptions.lower()


async def test_refus_de_permission_dit_ou_l_accorder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Un refus de permission est une instruction : où cliquer, quelle route API."""
    _refuser_permission(monkeypatch)
    outil = ReadFileTool(allowed_roots=[tmp_path])

    resultat = await outil.execute(path=str(tmp_path / "x.txt"))

    assert resultat.is_error
    assert "/api/permissions/files" in resultat.content
    assert "Fichiers" in resultat.content
    assert "remote_pc" in resultat.content


async def test_refus_de_permission_aussi_sur_la_recherche(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _refuser_permission(monkeypatch)
    outil = FindFilesTool(allowed_roots=[tmp_path])

    resultat = await outil.execute(pattern="*.py")

    assert resultat.is_error
    assert "/api/permissions/files" in resultat.content


# ── 2. Le périmètre ───────────────────────────────────────────────────────────


async def test_traversee_par_point_point_refusee(tmp_path: Path) -> None:
    """`zone/../secret.txt` sort de la racine : la résolution doit le démasquer."""
    zone = tmp_path / "zone"
    zone.mkdir()
    (tmp_path / "secret.txt").write_text("classifié", encoding="utf-8")
    outil = ReadFileTool(allowed_roots=[zone])

    resultat = await outil.execute(path=str(zone / ".." / "secret.txt"))

    assert resultat.is_error
    assert "refusé" in resultat.content
    # Le refus nomme le périmètre et la manière de l'élargir.
    assert "FILE_SEARCH_ROOTS" in resultat.content
    assert str(zone) in resultat.content


async def test_lien_symbolique_vers_l_exterieur_refuse(tmp_path: Path) -> None:
    zone = tmp_path / "zone"
    zone.mkdir()
    dehors = tmp_path / "dehors.txt"
    dehors.write_text("classifié", encoding="utf-8")
    lien = zone / "innocent.txt"
    try:
        lien.symlink_to(dehors)
    except (OSError, NotImplementedError):
        pytest.skip("création de lien symbolique non autorisée sur cette machine")

    outil = ReadFileTool(allowed_roots=[zone])
    resultat = await outil.execute(path=str(lien))

    assert resultat.is_error
    assert "refusé" in resultat.content


async def test_arborescence_systeme_fermee_meme_dans_une_racine_large(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Une racine trop large ne doit pas rouvrir /etc & co.

    Les vrais chemins système sont POSIX ; on rejoue la règle sur un faux
    dossier système pour que le test vaille aussi sur un poste de dev Windows.
    """
    faux_etc = tmp_path / "etc"
    faux_etc.mkdir()
    (faux_etc / "hostname").write_text("crush-pi", encoding="utf-8")
    monkeypatch.setattr(filesystem, "_DOSSIERS_SYSTEME", (faux_etc,))

    outil = ReadFileTool(allowed_roots=[tmp_path])
    resultat = await outil.execute(path=str(faux_etc / "hostname"))

    assert resultat.is_error
    assert "refusé" in resultat.content
    assert "execute_cli" in resultat.content


@pytest.mark.skipif(sys.platform == "win32", reason="chemins système POSIX")
async def test_etc_refuse_en_production() -> None:
    """Sur la Pi, /etc est hors périmètre même avec une racine à la racine du disque."""
    outil = ReadFileTool(allowed_roots=[Path("/")])
    resultat = await outil.execute(path="/etc/hostname")

    assert resultat.is_error
    assert "refusé" in resultat.content


@pytest.mark.parametrize(
    "nom",
    [".env", "id_rsa", "serveur.key", "google_credentials.json", "gmail_token.json"],
)
async def test_fichier_au_nom_de_secret_refuse(tmp_path: Path, nom: str) -> None:
    """Ces fichiers sont dans le périmètre par construction : seul le nom les protège."""
    cible = tmp_path / nom
    cible.write_text("ANTHROPIC_API_KEY=sk-très-secret", encoding="utf-8")
    outil = ReadFileTool(allowed_roots=[tmp_path])

    resultat = await outil.execute(path=str(cible))

    assert resultat.is_error
    assert "sk-très-secret" not in resultat.content
    assert "refusé" in resultat.content


async def test_env_example_reste_lisible(tmp_path: Path) -> None:
    """`.env.example` est versionné et ne contient que des noms de variables."""
    exemple = tmp_path / ".env.example"
    exemple.write_text("ANTHROPIC_API_KEY=", encoding="utf-8")
    outil = ReadFileTool(allowed_roots=[tmp_path])

    resultat = await outil.execute(path=str(exemple))

    assert not resultat.is_error
    assert "ANTHROPIC_API_KEY=" in resultat.content


async def test_magasin_de_cles_refuse(tmp_path: Path) -> None:
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    cle = ssh / "config"
    cle.write_text("Host serveur", encoding="utf-8")
    outil = ReadFileTool(allowed_roots=[tmp_path])

    resultat = await outil.execute(path=str(cle))

    assert resultat.is_error
    assert ".ssh" in resultat.content


async def test_racines_vides_refusent_et_le_disent(tmp_path: Path) -> None:
    """Sans racine configurée, le message doit nommer le réglage manquant."""
    outil = ReadFileTool(allowed_roots=[])
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")

    resultat = await outil.execute(path=str(tmp_path / "a.txt"))

    assert resultat.is_error
    assert "FILE_SEARCH_ROOTS" in resultat.content


# ── 3. Robustesse de la lecture ───────────────────────────────────────────────


async def test_fichier_binaire_annonce_comme_tel(tmp_path: Path) -> None:
    """Un binaire rendu en « caractères de remplacement » noierait le contexte."""
    binaire = tmp_path / "image.png"
    binaire.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x01")
    outil = ReadFileTool(allowed_roots=[tmp_path])

    resultat = await outil.execute(path=str(binaire))

    assert resultat.is_error
    assert "binaire" in resultat.content
    assert "�" not in resultat.content
    assert "exiftool" in resultat.content


async def test_fichier_non_utf8_lu_et_signale(tmp_path: Path) -> None:
    """Un export Windows en cp1252 doit être lu, mais l'approximation annoncée."""
    latin = tmp_path / "notes.txt"
    latin.write_bytes("Réunion à 15h — café".encode("cp1252"))
    outil = ReadFileTool(allowed_roots=[tmp_path])

    resultat = await outil.execute(path=str(latin))

    assert not resultat.is_error
    assert "Réunion" in resultat.content
    assert "non-UTF-8" in resultat.content


async def test_fichier_utf8_rendu_sans_entete(tmp_path: Path) -> None:
    """Cas nominal : le contenu sort tel quel, sans préambule parasite."""
    f = tmp_path / "note.md"
    # Ecriture en octets : l'outil lit le fichier en binaire et le rend tel
    # quel, a juste titre. write_text ferait traduire le saut de ligne par
    # Windows et ferait echouer le test sur la machine de developpement,
    # alors que la cible est Debian.
    f.write_bytes(("# Titre" + chr(10) + "Contenu accentué").encode())
    outil = ReadFileTool(allowed_roots=[tmp_path])

    resultat = await outil.execute(path=str(f))

    assert resultat.content == "# Titre\nContenu accentué"


async def test_fichier_vide_le_dit(tmp_path: Path) -> None:
    """Rendre une chaîne vide laissait croire à un échec silencieux."""
    vide = tmp_path / "vide.log"
    vide.write_text("", encoding="utf-8")
    outil = ReadFileTool(allowed_roots=[tmp_path])

    resultat = await outil.execute(path=str(vide))

    assert not resultat.is_error
    assert "vide" in resultat.content


async def test_fichier_trop_grand_indique_la_sortie(tmp_path: Path) -> None:
    """Le refus doit nommer l'option qui permet de continuer."""
    gros = tmp_path / "gros.log"
    gros.write_bytes(b"x" * (_MAX_FILE_SIZE + 500))
    outil = ReadFileTool(allowed_roots=[tmp_path])

    resultat = await outil.execute(path=str(gros))

    assert resultat.is_error
    assert "trop grand" in resultat.content
    assert "truncate=true" in resultat.content


async def test_troncature_explicite_rend_le_debut(tmp_path: Path) -> None:
    gros = tmp_path / "gros.log"
    gros.write_bytes(b"a" * (_MAX_FILE_SIZE + 500))
    outil = ReadFileTool(allowed_roots=[tmp_path])

    resultat = await outil.execute(path=str(gros), truncate=True)

    assert not resultat.is_error
    assert resultat.content.startswith("a" * 100)
    assert "tronquée" in resultat.content
    # Le plafond reste tenu, note de troncature comprise.
    assert len(resultat.content) < _MAX_FILE_SIZE + 500


async def test_troncature_au_milieu_d_un_caractere_multioctet(tmp_path: Path) -> None:
    """Couper à 100 Ko peut casser un « é » : ce n'est pas un défaut d'encodage."""
    f = tmp_path / "accents.txt"
    # Le plafond tombe entre les deux octets d'un « é ».
    f.write_bytes(b"a" * (_MAX_FILE_SIZE - 1) + "é".encode() + b"suite")
    outil = ReadFileTool(allowed_roots=[tmp_path])

    resultat = await outil.execute(path=str(f), truncate=True)

    assert not resultat.is_error
    assert "non-UTF-8" not in resultat.content
    assert "�" not in resultat.content


async def test_chemin_inexistant_oriente_vers_find_files(tmp_path: Path) -> None:
    outil = ReadFileTool(allowed_roots=[tmp_path])

    resultat = await outil.execute(path=str(tmp_path / "fantome.txt"))

    assert resultat.is_error
    assert "introuvable" in resultat.content
    assert "find_files" in resultat.content


async def test_repertoire_au_lieu_d_un_fichier(tmp_path: Path) -> None:
    dossier = tmp_path / "docs"
    dossier.mkdir()
    outil = ReadFileTool(allowed_roots=[tmp_path])

    resultat = await outil.execute(path=str(dossier))

    assert resultat.is_error
    assert "répertoire" in resultat.content
    assert "find_files" in resultat.content


@pytest.mark.parametrize("chemin", ["", "   "])
async def test_chemin_vide_sans_trace(tmp_path: Path, chemin: str) -> None:
    outil = ReadFileTool(allowed_roots=[tmp_path])

    resultat = await outil.execute(path=chemin)

    assert resultat.is_error
    assert "vide" in resultat.content


async def test_chemin_invalide_sans_trace(tmp_path: Path) -> None:
    """Un octet nul dans le chemin lève ValueError : il doit être rattrapé."""
    outil = ReadFileTool(allowed_roots=[tmp_path])

    resultat = await outil.execute(path="fichier\x00.txt")

    assert resultat.is_error
    assert "invalide" in resultat.content


# ── 4. Robustesse de la recherche ─────────────────────────────────────────────


async def test_recherche_insensible_a_la_casse(tmp_path: Path) -> None:
    (tmp_path / "Rapport.PDF").write_text("x", encoding="utf-8")
    outil = FindFilesTool(allowed_roots=[tmp_path])

    resultat = await outil.execute(pattern="*.pdf", directory=str(tmp_path))

    assert not resultat.is_error
    assert "Rapport.PDF" in resultat.content


async def test_recherche_ecarte_les_secrets(tmp_path: Path) -> None:
    """Lister `google_credentials.json` invite à une lecture qui sera refusée."""
    (tmp_path / "notes.json").write_text("{}", encoding="utf-8")
    (tmp_path / "google_credentials.json").write_text("{}", encoding="utf-8")
    outil = FindFilesTool(allowed_roots=[tmp_path])

    resultat = await outil.execute(pattern="*.json", directory=str(tmp_path))

    assert "notes.json" in resultat.content
    assert "google_credentials.json" not in resultat.content
    assert "écarté" in resultat.content


async def test_recherche_sans_repertoire_part_de_la_racine_autorisee(tmp_path: Path) -> None:
    """Le défaut était `~`, souvent hors périmètre : le refus était incompréhensible."""
    zone = tmp_path / "zone"
    zone.mkdir()
    (zone / "trouve.txt").write_text("x", encoding="utf-8")
    outil = FindFilesTool(allowed_roots=[zone])

    resultat = await outil.execute(pattern="*.txt")

    assert not resultat.is_error
    assert "trouve.txt" in resultat.content


async def test_recherche_hors_perimetre_liste_les_racines(tmp_path: Path) -> None:
    zone = tmp_path / "zone"
    zone.mkdir()
    outil = FindFilesTool(allowed_roots=[zone])

    resultat = await outil.execute(pattern="*.txt", directory=str(tmp_path))

    assert resultat.is_error
    assert str(zone) in resultat.content


async def test_recherche_repertoire_inexistant(tmp_path: Path) -> None:
    outil = FindFilesTool(allowed_roots=[tmp_path])

    resultat = await outil.execute(pattern="*.txt", directory=str(tmp_path / "nulle_part"))

    assert resultat.is_error
    assert "introuvable" in resultat.content


async def test_recherche_motif_vide(tmp_path: Path) -> None:
    outil = FindFilesTool(allowed_roots=[tmp_path])

    resultat = await outil.execute(pattern="", directory=str(tmp_path))

    assert resultat.is_error
    assert "vide" in resultat.content


async def test_motif_sans_joker_conseille_les_asterisques(tmp_path: Path) -> None:
    (tmp_path / "mon_rapport_final.txt").write_text("x", encoding="utf-8")
    outil = FindFilesTool(allowed_roots=[tmp_path])

    resultat = await outil.execute(pattern="rapport", directory=str(tmp_path))

    assert not resultat.is_error
    assert "*rapport*" in resultat.content


async def test_plafond_de_resultats_signale(tmp_path: Path) -> None:
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
    outil = FindFilesTool(allowed_roots=[tmp_path])

    resultat = await outil.execute(pattern="*.txt", directory=str(tmp_path), max_results=3)

    lignes = [ligne for ligne in resultat.content.splitlines() if ligne.endswith(".txt")]
    assert len(lignes) == 3
    assert "interrompu" in resultat.content


async def test_lien_symbolique_ecarte_des_resultats(tmp_path: Path) -> None:
    zone = tmp_path / "zone"
    zone.mkdir()
    dehors = tmp_path / "dehors.txt"
    dehors.write_text("classifié", encoding="utf-8")
    try:
        (zone / "innocent.txt").symlink_to(dehors)
    except (OSError, NotImplementedError):
        pytest.skip("création de lien symbolique non autorisée sur cette machine")

    outil = FindFilesTool(allowed_roots=[zone])
    resultat = await outil.execute(pattern="*.txt", directory=str(zone))

    assert "innocent.txt" not in resultat.content


def test_parcours_respecte_son_budget_de_temps(tmp_path: Path) -> None:
    """Sans borne de durée, un `~` volumineux fige la boucle asyncio.

    Budget négatif = échéance déjà dépassée : le parcours doit rendre la main
    après le premier répertoire, en se déclarant interrompu.
    """
    profond = tmp_path
    for i in range(5):
        profond = profond / f"n{i}"
        profond.mkdir()
        (profond / "a.txt").write_text("x", encoding="utf-8")

    _, _, interrompu = _parcourir(str(tmp_path), "*.txt", cap=50, budget=-1.0)

    assert interrompu


def test_parcours_ignore_les_dossiers_lourds(tmp_path: Path) -> None:
    for nom in (".git", "node_modules", "__pycache__"):
        lourd = tmp_path / nom
        lourd.mkdir()
        (lourd / "cible.txt").write_text("x", encoding="utf-8")
    (tmp_path / "cible.txt").write_text("x", encoding="utf-8")

    chemins, _, _ = _parcourir(str(tmp_path), "*.txt", cap=50, budget=5.0)

    assert chemins == [str(tmp_path / "cible.txt")]


def test_parcours_ne_suit_pas_les_liens_de_repertoire(tmp_path: Path) -> None:
    """Un lien vers un parent ferait boucler le parcours jusqu'au budget."""
    zone = tmp_path / "zone"
    zone.mkdir()
    (zone / "a.txt").write_text("x", encoding="utf-8")
    try:
        (zone / "boucle").symlink_to(zone, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("création de lien symbolique non autorisée sur cette machine")

    chemins, _, interrompu = _parcourir(str(zone), "*.txt", cap=50, budget=5.0)

    assert chemins == [str(zone / "a.txt")]
    assert not interrompu


def test_pas_de_residu_macos() -> None:
    """Le code Spotlight/TCC visait un Mac : il n'a plus de sens sur la Pi."""
    source = Path(filesystem.__file__).read_text(encoding="utf-8")
    assert "mdfind" not in source
    assert "Darwin" not in source
    assert os.sep  # garde l'import utile si le fichier évolue
