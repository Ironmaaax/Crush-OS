# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .


from __future__ import annotations

import asyncio
import re
import shlex
import tempfile
from datetime import UTC, datetime, timedelta
from itertools import chain
from pathlib import Path
from typing import NamedTuple

import yaml
from loguru import logger

from crush.capabilities.tools.base import Tool, ToolResult
from crush.kernel.settings import settings

# ── Whitelist binaires autorisés pour execute_cli ────────────────────────────
# La machine hôte est une Raspberry Pi 5 sous Debian ARM64, sans écran, sans
# webcam, sans carte son. La liste précédente venait d'un portage macOS (sips,
# osascript, open, say, screencapture, afinfo, pmset) : ces binaires n'existent
# pas ici. Les garder ne protégeait rien et laissait croire au modèle qu'il
# pilotait un Mac — il obtenait un « No such file or directory » incompréhensible
# au lieu d'être orienté vers 'remote_pc', le seul outil qui atteint la machine
# de l'utilisateur.
#
# Aucun shell n'est interposé (asyncio.create_subprocess_exec) : « | », « > »,
# « && » et « $(...) » arrivent au binaire comme des arguments littéraux. C'est
# ce qui rend une redirection hors espace de travail structurellement impossible,
# et c'est pourquoi la liste peut accueillir des outils de lecture sans ouvrir
# de primitive d'écriture.
#
# La liste est rangée par usage : elle sert aussi de menu rendu au modèle quand
# une commande est refusée (cf. _message_refus).
_WHITELIST_PAR_CATEGORIE: dict[str, tuple[str, ...]] = {
    "dev": ("git", "python", "python3", "pip", "pip3", "uv"),
    "fichiers (lecture)": (
        "ls",
        "cat",
        "head",
        "tail",
        "grep",
        "find",
        "tree",
        "stat",
        "file",
        "realpath",
        "basename",
        "dirname",
        "wc",
        "sort",
        "uniq",
        "cut",
        "md5sum",
        "sha256sum",
        "jq",  # lecture/filtrage JSON : pas d'écriture possible sans redirection shell
    ),
    "fichiers (écriture)": ("mkdir", "touch", "cp", "mv", "rename", "rm", "zip", "unzip"),
    "médias": (
        "yt-dlp",
        "ffmpeg",
        "ffprobe",  # inspection d'un média sans le réencoder — évite le détour par ffmpeg -i
        "convert",
        "magick",
        "rembg",
        "pdftk",
        "pdftoppm",
        "exiftool",
    ),
    "système (lecture)": (
        "echo",
        "date",
        "uname",
        "hostname",
        "whoami",
        "id",
        "pwd",
        "uptime",
        "df",
        "du",
        "free",
        "ps",
        "which",
        "journalctl",  # l'assistant doit pouvoir lire ses propres logs : il n'a pas d'écran
        "vcgencmd",  # télémétrie Pi (température, throttling) — spécifique Raspberry Pi
    ),
    # shutdown reste joignable mais sous approbation humaine : la Pi tourne sans
    # écran ni clavier, un arrêt non voulu impose un déplacement physique.
    "système (sensible, approbation requise)": ("shutdown",),
}

CLI_WHITELIST: frozenset[str] = frozenset(chain.from_iterable(_WHITELIST_PAR_CATEGORIE.values()))

# ── Interpréteurs : binaires qui exécutent du code passé en argument ─────────
# Ces binaires déclenchent TOUJOURS _requires_approval, même whitelistés.
# Le LLM — ou une injection de prompt — ne peut pas les utiliser sans confirmation.
_INTERPRETERS_REQUIRE_APPROVAL: frozenset[str] = frozenset(
    {
        "python",  # python -c / -m : exécution de code arbitraire
        "python3",
        "pip",  # une install exécute le setup.py du paquet téléchargé
        "pip3",
        "uv",
        "osascript",  # AppleScript arbitraire = exécution de code
    }
)

# ── Binaires système à effets de bord irréversibles ───────────────────────────
# pmset et sudo ne sont plus whitelistés : ils restent listés ici pour que la
# porte soit déjà fermée si quelqu'un réintroduit le binaire dans la whitelist.
_SYSTEM_REQUIRE_APPROVAL: frozenset[str] = frozenset(
    {
        "shutdown",
        "pmset",
        "sudo",
        "rm",
    }
)

# ── Binaires qui écrasent des fichiers existants ──────────────────────────────
# Le sandbox ne déplace que le cwd : un chemin absolu écrit toujours où il veut.
# `cp`, `mv`, `rename` et `unzip -d` peuvent donc remplacer un fichier hors de
# l'espace de travail sans jamais le dire — d'où l'approbation humaine.
_WRITE_REQUIRE_APPROVAL: frozenset[str] = frozenset({"cp", "mv", "rename", "unzip"})

# ── Options qui transforment un binaire de lecture en primitive d'exécution ───
# Le nom du binaire ne suffit pas à décider : `find` est inoffensif, `find -exec`
# est un shell déguisé qui contourne toute la whitelist.
class _RegleArgs(NamedTuple):
    interdits: frozenset[str]
    raison: str
    # `git -c x=y log` est une injection ; `git log -c` est un affichage de diff
    # parfaitement légitime. Pour git, seules les options GLOBALES — celles qui
    # précèdent la sous-commande — sont examinées.
    globales_seulement: bool = False


_ARGS_INTERDITS: dict[str, _RegleArgs] = {
    "find": _RegleArgs(
        frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprintf", "-fprint", "-fls"}),
        "ces options font de `find` un exécuteur (ou un effaceur) de fichiers. "
        "Liste d'abord les chemins avec `find`, puis agis dessus par une commande explicite.",
    ),
    "git": _RegleArgs(
        frozenset({"-c", "--config-env", "--exec-path", "--upload-pack", "--receive-pack"}),
        "ces options injectent une commande via la configuration git "
        "(alias, pager, sshCommand). Utilise `git` sans surcharge de configuration.",
        globales_seulement=True,
    ),
    "journalctl": _RegleArgs(
        frozenset({"--vacuum-size", "--vacuum-time", "--vacuum-files", "--rotate", "--flush"}),
        "ces options effacent ou font tourner les journaux. `journalctl` n'est autorisé "
        "qu'en lecture (-u, -n, --since, -p).",
    ),
}

# ── Refus : orienter vers ce qui marche ici ───────────────────────────────────
# Un refus sans issue fait boucler le modèle sur des variantes de la même
# commande. Chaque entrée nomme le chemin qui, lui, aboutit.
_MACOS_HORS_SUJET = (
    "binaire macOS : cette machine est une Raspberry Pi sous Debian, sans écran ni son. "
    "Pour agir sur l'ordinateur de l'utilisateur, utilise l'outil 'remote_pc'."
)
_INTERACTIF_HORS_SUJET = (
    "outil interactif : aucun terminal n'est attaché, la commande resterait bloquée "
    "jusqu'au timeout. Utilise cat, head, tail ou ps."
)
_ALTERNATIVES: dict[str, str] = {
    "curl": "pour consulter une page ou une API web, utilise l'outil 'browser'.",
    "wget": "pour consulter une page web, utilise l'outil 'browser' ; "
    "pour télécharger une vidéo, `yt-dlp` est autorisé.",
    "bash": "aucun shell n'est exposé. Appelle directement le binaire, ou déclare ta "
    "séquence dans config/tools.yaml et lance-la avec l'outil run_script.",
    "sh": "aucun shell n'est exposé. Appelle directement le binaire, ou déclare ta "
    "séquence dans config/tools.yaml et lance-la avec l'outil run_script.",
    "zsh": "aucun shell n'est exposé — appelle directement le binaire voulu.",
    "sudo": "aucune escalade de privilège n'est possible depuis un outil. "
    "Demande à l'utilisateur de le faire en SSH sur la Pi.",
    "apt": "installer un paquet demande les droits root : à faire en SSH sur la Pi.",
    "apt-get": "installer un paquet demande les droits root : à faire en SSH sur la Pi.",
    "systemctl": "piloter un service demande les droits root : à faire en SSH sur la Pi. "
    "Pour seulement lire les journaux, `journalctl -u <service> -n 50` est autorisé.",
    "reboot": "redémarrer la Pi n'est pas possible depuis un outil : elle est sans écran, "
    "un échec au redémarrage demanderait une intervention physique.",
    "chmod": "modifier des permissions est refusé par conception : à faire en SSH.",
    "chown": "changer de propriétaire est refusé par conception : à faire en SSH.",
    "dd": "écriture bloc à bloc refusée par conception.",
    "ssh": "les connexions sortantes ne passent pas par cet outil.",
    "scp": "les transferts sortants ne passent pas par cet outil.",
    "nc": "les connexions réseau brutes ne passent pas par cet outil.",
    "sips": _MACOS_HORS_SUJET + " Pour retailler une image ici : `convert`.",
    "osascript": _MACOS_HORS_SUJET,
    "open": _MACOS_HORS_SUJET,
    "say": _MACOS_HORS_SUJET + " La synthèse vocale passe par la sortie vocale de Crush.",
    "screencapture": _MACOS_HORS_SUJET + " La Pi n'a pas d'écran à capturer.",
    "afinfo": _MACOS_HORS_SUJET + " Pour inspecter un fichier audio ici : `ffprobe`.",
    "pmset": _MACOS_HORS_SUJET,
    "nano": _INTERACTIF_HORS_SUJET,
    "vim": _INTERACTIF_HORS_SUJET,
    "less": _INTERACTIF_HORS_SUJET,
    "top": _INTERACTIF_HORS_SUJET,
    "htop": _INTERACTIF_HORS_SUJET,
}

_TIMEOUT = 30.0
_APPROVAL_TTL = timedelta(minutes=5)

# Patterns vraiment irréversibles — refusés même avec confirmation
_EXEC_BLOCKED_RE = re.compile(
    r"rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?/?(\.\./)*/?$"
    r"|rm\s+--no-preserve-root"
    r"|:\(\)\s*\{.*\}"  # fork bomb
    r"|\bmkfs\b|\bfdisk\b|\bparted\b"
    r"|dd\s+if=.*\bof=/dev/"
    r"|>\s*/dev/(sda|hda|nvme|loop|disk)\d*"
    r"|\|\s*(bash|sh|zsh|fish|dash)\b"
    r"|curl\b[^|]*\|\s*sudo"
    r"|wget\b[^|]*-O\s*-[^|]*\|\s*(bash|sh)",
    re.IGNORECASE | re.DOTALL,
)

# ── Blocklist inconditionnelle ────────────────────────────────────────────────
# Ces patterns sont refusés MÊME si le script est whitelisté et marqué "safe".
# Contrôle de sécurité de dernier recours.
_BLOCKED_PATTERNS: list[str] = [
    r"rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?/?(\.\./)*/?$",  # rm -rf / ou rm /
    r"rm\s+--no-preserve-root",
    r":\(\)\s*\{.*\}",  # fork bomb
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bhalt\b",
    r"\bmkfs\b",
    r"\bfdisk\b",
    r"\bparted\b",
    r"dd\s+if=.*\bof=/dev/",
    r">\s*/dev/(sda|hda|nvme|loop|disk)\d*",
    r"\|\s*(bash|sh|zsh|fish|dash)\b",  # piping to shell
    r"curl\b[^|]*\|\s*sudo",
    r"wget\b[^|]*-O\s*-[^|]*\|\s*(bash|sh)",
    r"\bsudo\b",  # sudo bloqué par défaut
]
_BLOCKED_RE = re.compile("|".join(_BLOCKED_PATTERNS), re.IGNORECASE | re.DOTALL)

# Exemple d'entrée de catalogue, servi au modèle quand aucun script n'est installé :
# un outil vide doit dire comment le remplir, sinon il n'est qu'une impasse.
_EXEMPLE_CATALOGUE = (
    "  mon_script:\n"
    '    command: ["python3", "scripts/mon_script.py"]\n'
    '    description: "ce que fait le script"\n'
    "    tier: safe        # safe = lancé directement | confirm = approbation | reject = coupé\n"
)


def _whitelist_lisible() -> str:
    """Rend la whitelist par catégorie, sur plusieurs lignes.

    Une énumération à plat de trente binaires ne dit pas au modèle lequel prendre ;
    le regroupement par usage lui permet de choisir un remplaçant du même ordre.
    """
    return "\n".join(
        f"  {categorie} : {', '.join(binaires)}"
        for categorie, binaires in _WHITELIST_PAR_CATEGORIE.items()
    )


def _message_refus(binary: str) -> str:
    """Refus nominatif + issue praticable — jamais un simple constat."""
    lignes = [f"Binaire '{binary}' non autorisé par la whitelist d'execute_cli."]
    conseil = _ALTERNATIVES.get(binary.lower())
    if conseil:
        lignes.append(f"À la place : {conseil}")
    lignes.append("Binaires autorisés sur cette machine (Raspberry Pi 5, Debian ARM64) :")
    lignes.append(_whitelist_lisible())
    return "\n".join(lignes)


class _PendingApproval:
    """Script en attente de confirmation utilisateur."""

    __slots__ = ("cmd", "alias", "description", "expires_at")

    def __init__(self, alias: str, cmd: list[str], description: str) -> None:
        self.alias = alias
        self.cmd = cmd
        self.description = description
        self.expires_at = datetime.now(UTC) + _APPROVAL_TTL


class CLIRunnerTool(Tool):
    """Lance des scripts shell whitelistés avec 3 niveaux de sécurité.

    Tiers :
      safe    — exécuté immédiatement (whitelist suffit comme garantie)
      confirm — mis en attente, nécessite que l'utilisateur dise 'confirme <alias>'
      reject  — toujours refusé

    Sécurité supplémentaire :
      • Blocklist de patterns dangereux (appliquée avant le tier)
      • Option sandboxed=true : exécution dans un répertoire temporaire isolé
      • Timeout strict (30s par défaut)
      • TTL de 5 min sur les approbations en attente
    """

    name = "run_script"

    def __init__(self, whitelist_path: Path) -> None:
        self._whitelist_path = whitelist_path
        self._scripts: dict[str, dict] = self._charger_catalogue(whitelist_path)
        self._pending: dict[str, _PendingApproval] = {}

        safe_names = [k for k, v in self._scripts.items() if v.get("tier", "safe") == "safe"]
        confirm_names = [k for k, v in self._scripts.items() if v.get("tier") == "confirm"]

        if self._scripts:
            self.description = (
                f"Lance un script préenregistré sur la machine hôte (Raspberry Pi, Debian). "
                f"Alias disponibles : {', '.join(sorted(self._scripts))}. "
                f"Niveaux : safe (lancé directement)={safe_names or 'aucun'}, "
                f"confirm (approbation requise)={confirm_names or 'aucun'}. "
                "Pour exécuter un script mis en attente, rappelle avec action='confirm'."
            )
        else:
            # Description honnête : sans elle, le modèle appelle un outil qui ne
            # peut que refuser, puis recommence avec un autre alias inventé.
            self.description = (
                "Lance un script préenregistré. AUCUN script n'est installé : le catalogue "
                f"{whitelist_path} est vide, cet outil ne peut donc rien lancer pour l'instant. "
                "Pour une commande ponctuelle, utilise execute_cli."
            )

        # Le schéma est la SEULE description des arguments que reçoit le modèle.
        # S'il n'annonce pas 'alias', l'appel arrive sans argument et execute()
        # échoue avant même d'entrer dans la fonction : schéma et signature sont
        # donc écrits ici côte à côte, et 'alias' est déclaré requis.
        alias_schema: dict = {
            "type": "string",
            "description": (
                "Alias du script à lancer, tel que déclaré dans le catalogue. "
                f"Disponibles : {', '.join(sorted(self._scripts)) or 'aucun script installé'}."
            ),
        }
        if self._scripts:
            # enum vide = schéma invalide côté API : on ne le pose que s'il y a des alias.
            alias_schema["enum"] = sorted(self._scripts)

        self.input_schema = {
            "type": "object",
            "properties": {
                "alias": alias_schema,
                "action": {
                    "type": "string",
                    "enum": ["run", "confirm"],
                    "default": "run",
                    "description": (
                        "'run' (défaut) : lance le script, ou le met en attente si tier=confirm. "
                        "'confirm' : exécute un script précédemment mis en attente."
                    ),
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Arguments ajoutés à la fin de la commande du script (optionnel). "
                        "Ils ne passent pas par un shell : pas de « | », « > » ni « $(...) »."
                    ),
                },
            },
            "required": ["alias"],
        }

    @staticmethod
    def _charger_catalogue(path: Path) -> dict[str, dict]:
        """Lit config/tools.yaml et écarte les entrées inutilisables.

        Un alias annoncé au modèle mais dépourvu de clé `command` planterait à
        l'exécution sur un KeyError illisible. Mieux vaut ne pas l'annoncer et
        laisser une trace dans le journal pour celui qui a écrit le YAML.
        """
        if not path.exists():
            logger.info("run_script : aucun catalogue à {}", path)
            return {}
        try:
            brut = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError) as e:
            logger.error("run_script : catalogue {} illisible ({})", path, e)
            return {}
        if not isinstance(brut, dict):
            if brut is not None:
                logger.warning("run_script : {} n'est pas un mapping alias → script", path)
            return {}

        scripts: dict[str, dict] = {}
        for alias, entree in brut.items():
            if not isinstance(entree, dict) or not entree.get("command"):
                logger.warning("run_script : alias '{}' ignoré (clé 'command' absente)", alias)
                continue
            scripts[str(alias)] = entree
        return scripts

    @staticmethod
    def _commande(script: dict) -> list[str]:
        """Normalise `command:`, écrit à la main donc parfois en chaîne."""
        brut = script["command"]
        if isinstance(brut, str):
            return shlex.split(brut)
        return [str(x) for x in brut]

    def _indice_catalogue(self) -> str:
        """Ce qu'il faut savoir pour réussir l'appel suivant."""
        if self._scripts:
            return f"Alias disponibles : {', '.join(sorted(self._scripts))}."
        return (
            f"Aucun script n'est installé : le catalogue {self._whitelist_path} ne contient "
            "aucune entrée exploitable. Pour en ajouter un, écris dedans :\n"
            f"{_EXEMPLE_CATALOGUE}"
            "puis redémarre le service (systemctl restart crush-api). "
            "Pour une commande ponctuelle sans passer par le catalogue, utilise execute_cli."
        )

    async def execute(
        self,
        alias: str | None = None,
        action: str = "run",
        args: list[str] | str | None = None,
        **_: object,
    ) -> ToolResult:
        # 'alias' est requis par le schéma, mais un modèle peut l'omettre : une
        # signature à argument obligatoire transformerait cet oubli en TypeError
        # remonté brut par le registre, message dont personne ne peut rien faire.
        if not isinstance(alias, str) or not alias.strip():
            return ToolResult(
                content=(
                    "Argument 'alias' manquant : run_script lance un script du catalogue, "
                    f"il faut lui dire lequel. {self._indice_catalogue()}"
                ),
                is_error=True,
            )
        alias = alias.strip()

        if action not in ("run", "confirm"):
            return ToolResult(
                content=(
                    f"Action inconnue : '{action}'. Actions acceptées : "
                    "'run' (lancer) et 'confirm' (exécuter un script mis en attente)."
                ),
                is_error=True,
            )

        # ── Confirmation d'un script en attente ──────────────────────────────
        if action == "confirm":
            return await self._confirm_pending(alias)

        # ── Lookup whitelist ──────────────────────────────────────────────────
        script = self._scripts.get(alias)
        if script is None:
            return ToolResult(
                content=f"Script inconnu : '{alias}'. {self._indice_catalogue()}",
                is_error=True,
            )

        # Le modèle envoie parfois une chaîne là où le schéma annonce un tableau.
        if isinstance(args, str):
            args = shlex.split(args)
        cmd = self._commande(script) + [str(a) for a in (args or [])]
        cmd_str = " ".join(cmd)
        tier = str(script.get("tier", "safe")).lower()

        # ── Blocklist inconditionnelle ─────────────────────────────────────────
        if _BLOCKED_RE.search(cmd_str):
            logger.warning("CLIRunner BLOCKED by pattern", alias=alias, cmd=cmd_str)
            return ToolResult(
                content=(
                    f"Commande '{alias}' refusée — pattern dangereux détecté dans : `{cmd_str}`. "
                    "Cette vérification est inconditionnelle."
                ),
                is_error=True,
            )

        # ── Tier reject ───────────────────────────────────────────────────────
        if tier == "reject":
            logger.info("CLIRunner rejected by tier", alias=alias)
            return ToolResult(
                content=(
                    f"Script '{alias}' désactivé (tier: reject)."
                    f" Passe son tier à 'safe' ou 'confirm' dans {self._whitelist_path}"
                    " puis redémarre le service pour l'activer."
                ),
                is_error=True,
            )

        # ── Tier confirm : mise en attente d'approbation ──────────────────────
        if tier == "confirm":
            desc = script.get("description", cmd_str)
            self._pending[alias] = _PendingApproval(alias=alias, cmd=cmd, description=cmd_str)
            logger.info("CLIRunner awaiting approval", alias=alias)
            return ToolResult(
                content=(
                    f"⚠️ Ce script nécessite ton approbation avant exécution.\n"
                    f"Script : {desc}\n"
                    f"Commande : `{cmd_str}`\n\n"
                    f"Pour exécuter : réponds 'confirme {alias}' "
                    f"(approbation valide 5 minutes)."
                )
            )

        # ── Tier safe : exécution (avec sandbox optionnelle) ──────────────────
        sandboxed = bool(script.get("sandboxed", False))
        return await self._run(cmd, alias, sandboxed=sandboxed)

    async def _confirm_pending(self, alias: str) -> ToolResult:
        """Exécute un script préalablement mis en attente de confirmation."""
        # Nettoyage des entrées expirées
        now = datetime.now(UTC)
        expired = [k for k, p in self._pending.items() if p.expires_at <= now]
        for k in expired:
            logger.debug("CLIRunner approval expired", alias=k)
            del self._pending[k]

        pending = self._pending.pop(alias, None)
        if pending is None:
            return ToolResult(
                content=(
                    f"Aucun script '{alias}' en attente d'approbation "
                    "(ou délai de 5 minutes expiré). "
                    "Relance la commande pour un nouveau cycle d'approbation."
                ),
                is_error=True,
            )

        logger.info("CLIRunner confirmed and executing", alias=alias)
        return await self._run(pending.cmd, alias, sandboxed=False)

    async def _run(self, cmd: list[str], alias: str, *, sandboxed: bool) -> ToolResult:
        """Exécute le subprocess, en sandbox si demandé."""
        extra_kwargs: dict = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.STDOUT,
        }

        tmp_dir: str | None = None
        if sandboxed:
            tmp_dir = tempfile.mkdtemp(prefix="crush_sandbox_")
            extra_kwargs["cwd"] = tmp_dir
            extra_kwargs["env"] = {
                "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                "HOME": tmp_dir,
                "TMPDIR": tmp_dir,
                "LANG": "fr_FR.UTF-8",
                "LC_ALL": "fr_FR.UTF-8",
            }
            logger.info("CLIRunner sandboxed", alias=alias, cwd=tmp_dir)
        else:
            logger.info("CLIRunner executing", alias=alias, cmd=cmd)

        try:
            proc = await asyncio.create_subprocess_exec(*cmd, **extra_kwargs)
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
            output = stdout.decode(errors="replace").strip() or "Terminé (pas de sortie)."
            success = proc.returncode == 0
            logger.info("CLIRunner done", alias=alias, returncode=proc.returncode)
            return ToolResult(content=output, is_error=not success)
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return ToolResult(content=f"Timeout après {_TIMEOUT}s.", is_error=True)
        except FileNotFoundError:
            # Cause la plus fréquente : le catalogue référence un binaire ou un
            # script absent de la Pi. Le dire, plutôt que « [Errno 2] ».
            return ToolResult(
                content=(
                    f"Script '{alias}' introuvable à l'exécution : '{cmd[0]}' n'existe pas sur "
                    f"cette machine. Vérifie le chemin déclaré dans {self._whitelist_path}."
                ),
                is_error=True,
            )
        except OSError as e:
            return ToolResult(content=f"Erreur d'exécution : {e}", is_error=True)


class ExecuteCLITool(Tool):
    """Exécute une commande shell libre depuis la whitelist de binaires autorisés.

    La commande s'exécute sur la machine qui héberge Crush (Raspberry Pi 5,
    Debian ARM64, sans écran ni carte son), JAMAIS sur l'ordinateur de
    l'utilisateur — celui-ci se pilote avec l'outil 'remote_pc'.

    Modèle de menace :
      Ce tool est déclenché par le LLM, lequel peut avoir lu du contenu externe
      non fiable (Gmail, navigateur, Notion). Une injection de prompt dans ce
      contenu peut pousser le LLM à émettre une commande malveillante.
      La confirmation humaine des commandes sensibles est la DERNIÈRE ligne de
      défense non contournable.

    Couches de sécurité (dans l'ordre d'application) :
      1. Blocklist irréversible — fork bomb, rm -rf /, pipe→shell :
         refus même avec confirmed=True
      2. Parsing strict         — shlex.split requis ; guillemets non fermés = refus
         (jamais de split naïf)
      3. Allowlist binaire      — seuls les binaires de CLI_WHITELIST sont admis,
         résolu via Path(parts[0]).name (robuste aux chemins absolus)
      3bis. Allowlist d'options — refus des options qui font d'un binaire de
         lecture un exécuteur (find -exec, git -c…), cf. _ARGS_INTERDITS
      4. Approbation robuste    — basée sur le binaire résolu + args :
         interpréteurs (python, pip, uv…), écrasement de fichiers (cp, mv…),
         rm, shutdown, sudo
      5. Sandbox par défaut     — tmpdir isolé + env restreint
         (ALLOW_UNSANDBOXED_EXEC=true pour opt-out explicite)

    Aucun shell n'est interposé : « | », « > » et « && » ne sont pas interprétés,
    ils arrivent au binaire comme des arguments littéraux.

    Les interpréteurs (_INTERPRETERS_REQUIRE_APPROVAL) exigent confirmed=True
    systématiquement ; confirmed=True ne contourne JAMAIS la couche 1.
    """

    name = "execute_cli"
    description = (
        "Exécute une commande shell sur la machine qui héberge Crush (Raspberry Pi 5 sous "
        "Debian ARM64, sans écran ni son) — PAS sur l'ordinateur de l'utilisateur : pour "
        "cela, c'est l'outil 'remote_pc'. Le premier mot doit être un binaire whitelisté ; "
        "aucun shell n'est interposé, donc « | », « > » et « && » ne sont pas interprétés. "
        "Interpréteurs (python, python3, pip, uv) et commandes qui écrasent ou suppriment "
        "(cp, mv, rename, unzip, rm, shutdown) : appelle d'abord sans confirmed, montre la "
        "commande à l'utilisateur, puis rappelle avec confirmed=true après son accord. "
        "Sans approbation : ls, cat, grep, find, stat, jq, df, ps, journalctl, vcgencmd, "
        "git, ffmpeg, ffprobe, yt-dlp, convert, exiftool, pdftoppm."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "Commande complète, binaire whitelisté en tête. "
                    "Ex: 'journalctl -u crush-api -n 50', "
                    "'ffprobe -hide_banner video.mp4', "
                    "'convert entree.png -resize 800x600 sortie.jpg'. "
                    "Pas de tube ni de redirection : ils ne seraient pas interprétés."
                ),
            },
            "confirmed": {
                "type": "boolean",
                "description": (
                    "true après confirmation explicite de l'utilisateur (commandes sensibles)."
                ),
            },
        },
        "required": ["command"],
    }

    @staticmethod
    def _requires_approval(parts: list[str]) -> bool:
        """Détermine si la commande nécessite une confirmation humaine.

        Basé sur le BINAIRE RÉSOLU (Path(parts[0]).name) et les arguments parsés.
        Robuste aux chemins absolus (/usr/bin/python3) et à la casse.
        """
        if not parts:
            return False
        binary = Path(parts[0]).name.lower()

        # Interpréteurs : exécutent du code passé en argument → approbation systématique
        if binary in _INTERPRETERS_REQUIRE_APPROVAL:
            return True

        # open / xdg-open : approbation si lancement d'app (-a) ou URL externe
        if binary in ("open", "xdg-open"):
            rest = parts[1:]
            if "-a" in rest or any(a.startswith(("http://", "https://")) for a in rest):
                return True

        # Binaires système à effets de bord irréversibles
        if binary in _SYSTEM_REQUIRE_APPROVAL:
            return True

        # Binaires capables d'écraser un fichier hors de l'espace de travail
        return binary in _WRITE_REQUIRE_APPROVAL

    @staticmethod
    def _option_interdite(parts: list[str]) -> str | None:
        """Repère une option qui déguise un binaire de lecture en exécuteur."""
        binary = Path(parts[0]).name.lower()
        regle = _ARGS_INTERDITS.get(binary)
        if regle is None:
            return None
        for arg in parts[1:]:
            if regle.globales_seulement and not arg.startswith("-"):
                break  # sous-commande atteinte : la suite ne concerne plus le binaire lui-même
            # `--exec-path=/x` doit être reconnu comme `--exec-path`.
            if arg.split("=", 1)[0] in regle.interdits:
                return f"Option '{arg}' refusée pour '{binary}' : {regle.raison}"
        return None

    async def _run(self, parts: list[str], cmd_str: str) -> ToolResult:
        """Exécute le subprocess, sandboxé par défaut."""

        sandboxed = not getattr(settings, "allow_unsandboxed_exec", False)
        extra_kwargs: dict = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.STDOUT,
        }

        if sandboxed:
            tmp_dir = tempfile.mkdtemp(prefix="crush_exec_")
            extra_kwargs["cwd"] = tmp_dir
            extra_kwargs["env"] = {
                "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                "HOME": tmp_dir,
                "TMPDIR": tmp_dir,
                "LANG": "fr_FR.UTF-8",
                "LC_ALL": "fr_FR.UTF-8",
            }
            logger.info(f"ExecuteCLI sandboxed cwd={tmp_dir}: {cmd_str[:60]}")
        else:
            logger.info(f"ExecuteCLI unsandboxed (allow_unsandboxed_exec=true): {cmd_str[:60]}")

        try:
            proc = await asyncio.create_subprocess_exec(*parts, **extra_kwargs)
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300.0)
            output = stdout.decode(errors="replace").strip() or "Terminé (pas de sortie)."
            success = proc.returncode == 0
            logger.info(f"ExecuteCLI done: rc={proc.returncode}")
            return ToolResult(content=output, is_error=not success)
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return ToolResult(content="Timeout après 300s.", is_error=True)
        except FileNotFoundError:
            # Whitelisté mais pas installé : dire lequel et comment l'obtenir,
            # sinon le modèle relance la même commande en boucle.
            return ToolResult(
                content=(
                    f"'{Path(parts[0]).name}' est autorisé mais n'est pas installé sur cette "
                    "Raspberry Pi. Demande à l'utilisateur de l'installer en SSH "
                    f"(sudo apt install …), ou emploie un autre binaire de la whitelist."
                ),
                is_error=True,
            )
        except OSError as e:
            return ToolResult(content=f"Erreur d'exécution : {e}", is_error=True)

    async def execute(
        self,
        command: str | None = None,
        confirmed: bool = False,
        **_: object,
    ) -> ToolResult:
        # Argument requis par le schéma : un oubli du modèle doit produire une
        # consigne, pas un TypeError levé avant l'entrée dans la fonction.
        if not isinstance(command, str) or not command.strip():
            return ToolResult(
                content=(
                    "Argument 'command' manquant : passe la commande complète en une chaîne, "
                    "binaire whitelisté en tête. Ex : 'journalctl -u crush-api -n 50'."
                ),
                is_error=True,
            )

        # Couche 1 : blocklist irréversible — refus inconditionnel, avant tout parsing
        if _EXEC_BLOCKED_RE.search(command):
            logger.warning(f"ExecuteCLI BLOCKED: {command[:80]}")
            return ToolResult(
                content=(
                    "Refusé — pattern dangereux détecté (destruction irréversible ou "
                    "exécution détournée). Ce refus ne peut pas être levé par confirmed=true : "
                    "reformule sans cette construction."
                ),
                is_error=True,
            )

        # Couche 2 : parsing strict — refus si syntaxe invalide (guillemets non fermés…)
        try:
            parts = shlex.split(command)
        except ValueError as e:
            logger.warning(f"ExecuteCLI parse error ({e}): {command[:60]}")
            return ToolResult(
                content=f"Commande non parsable ({e}). Vérifiez les guillemets.",
                is_error=True,
            )

        if not parts:
            return ToolResult(content="Commande vide.", is_error=True)

        # Couche 3 : allowlist — binaire résolu (robuste aux chemins absolus)
        binary = Path(parts[0]).name
        if binary not in CLI_WHITELIST:
            logger.info(f"ExecuteCLI refusé (hors whitelist) : {binary}")
            return ToolResult(content=_message_refus(binary), is_error=True)

        # Couche 3bis : options qui contournent la whitelist depuis un binaire admis
        refus_option = self._option_interdite(parts)
        if refus_option is not None:
            logger.warning(f"ExecuteCLI option refusée : {command[:80]}")
            return ToolResult(content=refus_option, is_error=True)

        # Couche 4 : approbation robuste — basée sur le binaire résolu + args
        if self._requires_approval(parts) and not confirmed:
            logger.info(f"ExecuteCLI awaiting approval: {command[:60]}")
            return ToolResult(
                content=(
                    f"⚠️ Commande sensible — confirmation requise avant exécution.\n"
                    f"Commande : `{command}`\n\n"
                    "Présente cette commande à l'utilisateur et demande sa confirmation. "
                    "Si l'utilisateur dit oui, rappelle execute_cli avec confirmed=true."
                )
            )

        # Couche 5 : exécution sandboxée par défaut
        logger.info(f"ExecuteCLI running: {command[:80]}")
        return await self._run(parts, command)
