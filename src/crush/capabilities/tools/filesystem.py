# Copyright (C) 2026 Barthélemy Houot
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Lecture et recherche de fichiers sur la machine qui héberge l'assistant.

Les descriptions annonçaient « le Mac de l'utilisateur ». C'était faux et
coûteux : le service tourne sur une Raspberry Pi headless, donc le modèle
promettait de lire les fichiers de l'utilisateur et lisait ceux du serveur.
Pour agir sur la machine de l'utilisateur, l'outil est `remote_pc`.

Le confinement se fait en trois couches, la première seule ne suffisant pas :

1. `FILE_SEARCH_ROOTS` (.env) — les racines autorisées, résolues à la
   construction. Le chemin demandé est résolu AVANT le contrôle, donc un `..`
   ou un lien symbolique qui sort d'une racine est démasqué.
2. Les arborescences système — fermées même si une racine trop large les
   englobe. Voir `_DOSSIERS_SYSTEME`.
3. Les noms de secrets — un `.env`, une clé privée ou un `*token*.json` ne
   sort jamais d'ici, où qu'il soit. L'assistant lit ses propres fichiers :
   sans cette couche, une injection de prompt suffisait à exfiltrer sa clé
   d'API depuis le `.env` du dépôt, qui est sous la racine par défaut (`~/`).
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import time
from pathlib import Path

from loguru import logger

from crush.capabilities.tools.base import Tool, ToolResult
from crush.kernel.permissions import permissions as _perms

_MAX_FILE_SIZE = 100_000  # 100 Ko — au-delà, la réponse noierait le contexte du modèle
_EXCLUDED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".cache"}

# Un parcours de `~` sur une Pi (carte SD lente, éventuel montage réseau) peut
# durer des minutes. Passé ce budget on rend ce qu'on a trouvé, en le disant :
# une réponse partielle immédiate vaut mieux qu'un outil qui semble planté.
_BUDGET_PARCOURS_S = 5.0

# Arborescences fermées quelle que soit la configuration des racines.
# /proc et /sys : `stat` y annonce 0 octet, donc le plafond de taille ne protège
#   de rien, et /proc/self/environ livre l'environnement du service — dont ses
#   clés d'API.
# /etc : mots de passe (shadow), PSK Wi-Fi (wpa_supplicant), état Tailscale.
# /dev et /run : pseudo-fichiers, certains bloquants ou infinis en lecture.
# Ces chemins sont POSIX : sur un poste de dev Windows ils ne matchent rien, ce
# qui est sans conséquence puisque la machine à protéger est la Pi.
_DOSSIERS_SYSTEME = (
    Path("/proc"),
    Path("/sys"),
    Path("/dev"),
    Path("/run"),
    Path("/boot"),
    Path("/etc"),
)

# Magasins de clés : refusés même sur un chemin explicite.
_DOSSIERS_SENSIBLES = {".ssh", ".gnupg", ".aws", ".password-store", ".docker"}

_FICHIERS_SENSIBLES = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.ppk",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".netrc",
    ".pgpass",
    ".htpasswd",
    "shadow",
    "gshadow",
    "*credentials*.json",
    "*token*.json",
    "*secret*.json",
)

# Ces fichiers-là sont versionnés dans git et ne contiennent que des noms de
# variables : les refuser n'aurait protégé personne et aurait empêché la
# question la plus fréquente (« quelles variables faut-il renseigner ? »).
_EXCEPTIONS_SENSIBLES = {".env.example", ".env.sample", ".env.template", ".env.dist"}

_ENTETE_NON_UTF8 = (
    "[Fichier non-UTF-8, relu en cp1252 : quelques caractères peuvent être approximatifs]\n"
)

_OU_ACCORDER = (
    "Pour l'accorder : bouton « Fichiers » dans la section « Accès » de l'interface web, "
    'ou PATCH /api/permissions/files {"enabled": true}.'
)

_PAS_LA_BONNE_MACHINE = (
    "Rappel : ces fichiers sont ceux du serveur qui héberge l'assistant. "
    "Pour ceux de l'ordinateur de l'utilisateur, l'outil est remote_pc."
)


def _nom_sensible(nom: str) -> bool:
    """Vrai si le NOM du fichier annonce un secret, indépendamment de son contenu."""
    minuscule = nom.lower()
    if minuscule in _EXCEPTIONS_SENSIBLES:
        return False
    return any(fnmatch.fnmatch(minuscule, motif) for motif in _FICHIERS_SENSIBLES)


def _dossier_systeme(p: Path) -> Path | None:
    return next((d for d in _DOSSIERS_SYSTEME if p == d or p.is_relative_to(d)), None)


def _dossier_sensible(p: Path) -> str | None:
    return next((part for part in p.parts if part.lower() in _DOSSIERS_SENSIBLES), None)


def _resoudre(chemin: str) -> tuple[Path | None, str | None]:
    """Résout `~`, les `..` et les liens. Retourne (chemin, message d'erreur)."""
    if not chemin or not chemin.strip():
        return None, "Chemin vide : indiquer le chemin du fichier à lire."
    try:
        return Path(chemin).expanduser().resolve(), None
    except (OSError, ValueError, RuntimeError) as exc:
        # RuntimeError : `~compte_inconnu`. ValueError : octet nul dans le chemin.
        return None, f"Chemin invalide ({chemin!r}) : {exc}"


def _decoder(brut: bytes, tronque: bool) -> tuple[str, str]:
    """Décode le fichier. Retourne (texte, en-tête à afficher au modèle)."""
    try:
        return brut.decode("utf-8"), ""
    except UnicodeDecodeError as exc:
        # Une coupure à 100 Ko peut tomber au milieu d'un caractère multi-octets.
        # Ce n'est pas un fichier « non UTF-8 », juste une fin de bloc : le
        # signaler comme un problème d'encodage induirait le modèle en erreur.
        if tronque and exc.start >= len(brut) - 4:
            return brut[: exc.start].decode("utf-8"), ""
        # Beaucoup de fichiers réels (exports Windows, .srt, .csv) sont en cp1252.
        return brut.decode("cp1252", errors="replace"), _ENTETE_NON_UTF8


def _parcourir(root: str, pattern: str, cap: int, budget: float) -> tuple[list[str], int, bool]:
    """Parcours borné dans le temps et en résultats.

    Retourne (chemins, nombre de fichiers écartés, parcours interrompu).
    Fonction synchrone appelée dans un thread : `os.walk` bloque, et sur un
    home volumineux il gèlerait la boucle asyncio — donc l'API et la voix.
    """
    echeance = time.monotonic() + budget
    resultats: list[str] = []
    ecartes = 0
    motif = pattern.lower()

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # followlinks=False : un lien vers `/` relancerait le parcours depuis la
        # racine du disque, et deux liens croisés boucleraient indéfiniment.
        # Les dossiers cachés sont écartés d'office, ce qui couvre .ssh, .gnupg
        # et les caches d'outils.
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS and not d.startswith(".")]
        for filename in filenames:
            # Comparaison insensible à la casse : le modèle écrit '*.PY' aussi
            # souvent que '*.py', et un échec de casse ressemble à une absence.
            if not fnmatch.fnmatch(filename.lower(), motif):
                continue
            if _nom_sensible(filename):
                ecartes += 1
                continue
            resultats.append(str(Path(dirpath) / filename))
            if len(resultats) >= cap:
                return resultats, ecartes, True
        if time.monotonic() > echeance:
            return resultats, ecartes, True

    return resultats, ecartes, False


class _OutilFichiers(Tool):
    """Socle commun aux deux outils : périmètre autorisé et refus explicites."""

    def __init__(self, allowed_roots: list[Path]) -> None:
        self._allowed_roots = [Path(r).expanduser().resolve() for r in allowed_roots]

    @property
    def _racines_lisibles(self) -> str:
        if not self._allowed_roots:
            return "aucune (FILE_SEARCH_ROOTS est vide dans le .env du serveur)"
        return ", ".join(str(r) for r in self._allowed_roots)

    def _refus_permission(self) -> ToolResult:
        return ToolResult(
            content=(
                f"Accès aux fichiers refusé : la permission « Fichiers » est désactivée. "
                f"{_OU_ACCORDER} {_PAS_LA_BONNE_MACHINE}"
            ),
            is_error=True,
        )

    def _hors_perimetre(self, p: Path) -> str | None:
        """Motif du refus, ou None si le chemin est dans le périmètre lisible."""
        systeme = _dossier_systeme(p)
        if systeme is not None:
            return (
                f"Accès refusé : {systeme} appartient au système de la machine qui héberge "
                f"l'assistant (secrets et pseudo-fichiers). Ce périmètre reste fermé même "
                f"avec la permission « Fichiers ». Pour une information système, passer par "
                f"execute_cli, dont les binaires sont en liste blanche."
            )
        if not any(p == root or p.is_relative_to(root) for root in self._allowed_roots):
            return (
                f"Accès refusé : {p} est hors des répertoires autorisés. "
                f"Lisibles : {self._racines_lisibles}. Pour élargir, ajouter le répertoire à "
                f"FILE_SEARCH_ROOTS dans le .env du serveur puis redémarrer crush-api. "
                f"{_PAS_LA_BONNE_MACHINE}"
            )
        sensible = _dossier_sensible(p)
        if sensible is not None:
            return (
                f"Accès refusé : {sensible} est un magasin de clés. L'assistant ne lit jamais "
                f"son contenu, même avec la permission « Fichiers »."
            )
        if _nom_sensible(p.name):
            return (
                f"Accès refusé : « {p.name} » porte un nom de fichier de secrets (clé privée, "
                f"jeton, identifiants). L'assistant ne le lit jamais, y compris le sien. "
                f"Si une valeur précise est nécessaire, la demander à l'utilisateur."
            )
        return None


class ReadFileTool(_OutilFichiers):
    """Lecture seule d'un fichier texte du serveur — aucune écriture possible."""

    name = "read_file"
    input_schema = {  # noqa: RUF012
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Chemin absolu (ou avec ~) d'un fichier DU SERVEUR qui héberge "
                    "l'assistant. « ~ » désigne le compte de service, pas le dossier "
                    "personnel de l'utilisateur."
                ),
            },
            "truncate": {
                "type": "boolean",
                "description": (
                    "true pour lire seulement le début d'un fichier trop grand "
                    "(100 Ko) au lieu d'échouer. Défaut : false."
                ),
            },
        },
        "required": ["path"],
    }

    def __init__(self, allowed_roots: list[Path]) -> None:
        super().__init__(allowed_roots)
        # Description construite à l'instance : annoncer le périmètre réel évite
        # au modèle de promettre une lecture qu'il n'obtiendra pas.
        self.description = (
            "Lit un fichier texte sur la machine qui héberge l'assistant — un serveur "
            "Linux headless, PAS l'ordinateur de l'utilisateur. Lecture seule, 100 Ko max. "
            f"Répertoires lisibles : {self._racines_lisibles}. "
            "Pour un fichier de l'ordinateur de l'utilisateur, utiliser remote_pc."
        )

    async def execute(self, path: str = "", truncate: bool = False, **_: object) -> ToolResult:
        if not _perms.get("files"):
            return self._refus_permission()

        p, erreur = _resoudre(path)
        if p is None:
            return ToolResult(content=erreur or "Chemin invalide.", is_error=True)

        refus = self._hors_perimetre(p)
        if refus is not None:
            return ToolResult(content=refus, is_error=True)

        try:
            infos = p.stat()
        except FileNotFoundError:
            return ToolResult(
                content=(
                    f"Fichier introuvable : {p}. Vérifier le chemin, ou le localiser "
                    f"avec find_files."
                ),
                is_error=True,
            )
        except OSError as exc:
            return ToolResult(content=f"Chemin illisible ({p}) : {exc}", is_error=True)

        if p.is_dir():
            return ToolResult(
                content=(
                    f"{p} est un répertoire, pas un fichier. Pour en lister le contenu, "
                    f"utiliser find_files avec pattern='*'."
                ),
                is_error=True,
            )
        if not p.is_file():
            # Socket, FIFO, périphérique : une lecture peut bloquer sans fin.
            return ToolResult(
                content=f"{p} n'est pas un fichier régulier : lecture impossible.",
                is_error=True,
            )

        plafond_ko = _MAX_FILE_SIZE // 1000
        if infos.st_size > _MAX_FILE_SIZE and not truncate:
            return ToolResult(
                content=(
                    f"Fichier trop grand : {infos.st_size // 1000} Ko pour un plafond de "
                    f"{plafond_ko} Ko. Relancer avec truncate=true pour n'en lire que les "
                    f"{plafond_ko} premiers Ko, ou cibler la partie utile avec execute_cli "
                    f"(head, tail, grep)."
                ),
                is_error=True,
            )

        try:
            with p.open("rb") as fh:
                # On lit un octet de plus que le plafond : la troncature est
                # déduite de la lecture réelle, pas de `stat` — /proc et les
                # fichiers en cours d'écriture y annoncent une taille fausse.
                brut = fh.read(_MAX_FILE_SIZE + 1)
        except PermissionError:
            return ToolResult(
                content=(
                    f"Lecture refusée par le système : {p} n'appartient pas au compte qui "
                    f"exécute l'assistant. Corriger les droits sur le serveur, ou copier le "
                    f"fichier dans un répertoire lisible."
                ),
                is_error=True,
            )
        except OSError as exc:
            return ToolResult(content=f"Erreur de lecture ({p}) : {exc}", is_error=True)

        if not brut:
            return ToolResult(content=f"Fichier vide (0 octet) : {p}")

        if b"\x00" in brut[:4096]:
            return ToolResult(
                content=(
                    f"Fichier binaire ({p.suffix or 'sans extension'}, {infos.st_size} octets) : "
                    f"read_file ne rend que du texte. Pour un média ou un PDF, execute_cli avec "
                    f"exiftool donne les métadonnées."
                ),
                is_error=True,
            )

        tronque = len(brut) > _MAX_FILE_SIZE
        texte, entete = _decoder(brut[:_MAX_FILE_SIZE], tronque)
        if tronque:
            texte += (
                f"\n\n[Lecture tronquée aux {plafond_ko} premiers Ko "
                f"sur {infos.st_size // 1000} Ko.]"
            )

        logger.debug(f"read_file {p} ({len(texte)} caractères, tronqué={tronque})")
        return ToolResult(content=entete + texte)


class FindFilesTool(_OutilFichiers):
    """Recherche de fichiers par nom sur le serveur — parcours borné, lecture seule."""

    name = "find_files"
    input_schema = {  # noqa: RUF012
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": (
                    "Motif glob sur le nom du fichier : '*.py', 'main*', 'README.md'. "
                    "Pour un nom partiel, encadrer d'astérisques : '*rapport*'. "
                    "La casse est ignorée."
                ),
            },
            "directory": {
                "type": "string",
                "description": (
                    "Répertoire de départ SUR LE SERVEUR qui héberge l'assistant. "
                    "Défaut : la première racine autorisée."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "Nombre max de résultats (défaut : 20, max : 50).",
            },
        },
        "required": ["pattern"],
    }

    def __init__(self, allowed_roots: list[Path]) -> None:
        super().__init__(allowed_roots)
        self.description = (
            "Cherche des fichiers par nom sur la machine qui héberge l'assistant — un "
            "serveur Linux headless, PAS l'ordinateur de l'utilisateur. "
            f"Répertoires explorés : {self._racines_lisibles}. "
            "Les dossiers cachés et les caches sont ignorés. "
            "Pour chercher sur l'ordinateur de l'utilisateur, utiliser remote_pc."
        )

    async def execute(
        self,
        pattern: str = "",
        directory: str | None = None,
        max_results: int = 20,
        **_: object,
    ) -> ToolResult:
        if not _perms.get("files"):
            return self._refus_permission()

        if not pattern or not pattern.strip():
            return ToolResult(
                content="Motif vide : indiquer un motif, par exemple '*.pdf' ou '*rapport*'.",
                is_error=True,
            )

        if directory:
            root, erreur = _resoudre(directory)
            if root is None:
                return ToolResult(content=erreur or "Répertoire invalide.", is_error=True)
        elif self._allowed_roots:
            # Pas Path.home() : le home du compte de service n'est pas forcément
            # dans le périmètre, et le refus qui suivait était incompréhensible.
            root = self._allowed_roots[0]
        else:
            return ToolResult(
                content=(
                    "Aucun répertoire autorisé : FILE_SEARCH_ROOTS est vide dans le .env du "
                    "serveur. Y déclarer au moins un répertoire, puis redémarrer crush-api."
                ),
                is_error=True,
            )

        refus = self._hors_perimetre(root)
        if refus is not None:
            return ToolResult(content=refus, is_error=True)

        if not root.is_dir():
            return ToolResult(
                content=f"Répertoire introuvable sur le serveur : {root}",
                is_error=True,
            )

        cap = max(1, min(max_results, 50))
        chemins, ecartes, interrompu = await asyncio.to_thread(
            _parcourir, str(root), pattern, cap, _BUDGET_PARCOURS_S
        )

        # Un fichier trouvé peut être un lien symbolique pointant hors périmètre :
        # le parcours ne suit pas les liens, mais il en liste les noms.
        retenus: list[str] = []
        for chemin in chemins:
            cible = Path(chemin).resolve()
            if self._hors_perimetre(cible) is None:
                retenus.append(chemin)
            else:
                ecartes += 1

        if not retenus:
            conseil = (
                f" Si le nom est partiel, encadrer d'astérisques : '*{pattern.strip('*')}*'."
                if "*" not in pattern and "?" not in pattern
                else ""
            )
            return ToolResult(
                content=f"Aucun fichier trouvé pour '{pattern}' sous {root}.{conseil}"
            )

        lignes = list(retenus)
        if interrompu:
            lignes.append(
                f"[Parcours interrompu à {len(retenus)} résultats ou après "
                f"{_BUDGET_PARCOURS_S:.0f} s. Préciser 'directory' pour couvrir le reste.]"
            )
        if ecartes:
            lignes.append(f"[{ecartes} fichier(s) écarté(s) : nom de secret ou hors périmètre.]")

        logger.debug(f"find_files '{pattern}' sous {root} : {len(retenus)} résultat(s)")
        return ToolResult(content="\n".join(lignes))
