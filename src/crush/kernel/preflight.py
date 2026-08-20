# Copyright (C) 2026 Max Ea
# This file is part of CRUSH-OS,   .
 

"""preflight.py — Diagnostic de démarrage.

Détecte les problèmes les plus courants AVANT de lancer l'API et les explique
clairement (cause + fix), pour ne pas laisser l'utilisateur face à un traceback
Python illisible ou à un « API timeout » opaque.

Lancé par le launcher (`crush` / `crush.ps1`) juste avant `python -m crush.app`.
N'IMPORTE QUE LA STDLIB au niveau module : il doit pouvoir tourner même si les
dépendances du projet sont cassées — c'est précisément ce qu'il vérifie.

Sortie : 0 si tout est OK (démarrage autorisé), 1 si un problème bloquant est
détecté (le message d'explication est déjà affiché).
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

_USE_COLOR = sys.stderr.isatty() and os.name != "nt"


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _USE_COLOR else s


def _block(marker: str, color: str, title: str, body: str) -> None:
    print("\n" + _c(color, f"{marker} {title}"), file=sys.stderr)
    for line in body.strip("\n").splitlines():
        print("  " + line, file=sys.stderr)


def _err(title: str, body: str) -> None:
    _block("[ERREUR]", "91", title, body)


def _warn(title: str, body: str) -> None:
    _block("[ATTENTION]", "93", title, body)


# ── 1. Version de Python ──────────────────────────────────────────────────────


def check_python() -> bool:
    if sys.version_info < (3, 11):  # noqa: UP036 — détection runtime volontaire
        _err(
            "Version de Python trop ancienne",
            f"""
Crush a besoin de Python 3.11 ou plus récent.
Version détectée : {sys.version.split()[0]}

POURQUOI : le code utilise des fonctionnalités de Python 3.11 (typing, etc.).
FIX : installe Python 3.11+ puis recrée l'environnement avec « uv sync ».
""",
        )
        return False
    return True


# ── 2. Dépendances nécessaires au démarrage ───────────────────────────────────

# module importable -> rôle (pour un message compréhensible). Ce sont les paquets
# requis pour que l'API démarre ; les binaires natifs (pydantic_core) sont les
# premiers suspects quand « uv sync » a échoué ou que le venv a été déplacé/copié.
_CRITICAL_DEPS: dict[str, str] = {
    "fastapi": "le serveur web de l'API",
    "uvicorn": "le moteur qui fait tourner l'API",
    "pydantic": "la validation de la configuration",
    "pydantic_core": "le cœur compilé de pydantic (binaire natif)",
    "httpx": "les appels réseau (LLM, Notion, YouTube…)",
    "numpy": "le calcul (mémoire vectorielle)",
    "loguru": "les logs",
}


def check_deps() -> bool:
    missing: list[tuple[str, str, str]] = []
    for mod, role in _CRITICAL_DEPS.items():
        try:
            importlib.import_module(mod)
        except Exception as e:  # ImportError, mais aussi erreurs de binaire natif
            missing.append((mod, role, type(e).__name__))

    if not missing:
        return True

    lines = ["Des paquets nécessaires au démarrage de l'API sont absents ou cassés :", ""]
    for mod, role, err in missing:
        lines.append(f"  - {mod}  ({role})  [{err}]")
    lines += [
        "",
        "POURQUOI : c'est presque toujours l'environnement Python (.venv), PAS l'API LLM.",
        "Causes typiques : « uv sync » a échoué, le .venv a été copié/déplacé d'une",
        "autre machine, ou un binaire natif (pydantic_core, onnxruntime) ne correspond",
        "pas à ton OS/CPU.",
        "",
        "FIX, dans le dossier du projet :",
        "    uv sync --extra vision",
        "Si l'erreur persiste (typiquement sur pydantic_core / onnxruntime / dlib) :",
        "    supprime le dossier .venv, puis relance « uv sync --extra vision ».",
    ]
    _err("Dépendances manquantes ou cassées", "\n".join(lines))
    return False


# ── 3. Fichier .env ───────────────────────────────────────────────────────────

# Marqueurs qui trahissent une COMMANDE shell collée dans une valeur (erreur très
# fréquente : on copie un « setx KEY=... » ou un « $env:KEY=... » au lieu de la valeur).
_SHELL_TOKENS = ("$env:", "Set-", "setx ", "export ", "&&", "|", "<", ">")


def check_env() -> bool:
    """Vérifie .env. Non bloquant (avertit) : un .env douteux n'empêche pas
    forcément de démarrer, mais c'est la 2e cause de « ça capte mais rien ne marche »."""
    env_path = Path(".env")
    if not env_path.exists():
        _warn(
            "Fichier .env absent",
            """
Crush lit ses clés API et sa config dans un fichier .env, introuvable ici.
FIX : copie « .env.example » en « .env » et remplis au minimum la clé de ton LLM
      (ANTHROPIC_API_KEY, ou OPENAI_API_KEY si API_BACKEND=openai).
""",
        )
        return True  # non bloquant : l'app peut démarrer et le dira aussi

    suspicious: list[tuple[int, str, str]] = []
    text = env_path.read_text(encoding="utf-8", errors="replace")
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if any(tok in val for tok in _SHELL_TOKENS):
            suspicious.append((i, key, "ressemble à une commande shell, pas à une valeur"))
        elif (val.count('"') % 2) or (val.count("'") % 2):
            suspicious.append((i, key, "guillemets non équilibrés"))

    if suspicious:
        lines = ["Des lignes de .env semblent cassées (cause fréquente de crash) :", ""]
        for ln, key, why in suspicious:
            lines.append(f"  - ligne {ln}  ({key})  : {why}")
        lines += [
            "",
            "POURQUOI : une valeur de .env doit être la clé BRUTE, sur une seule ligne,",
            "sans guillemets ni commande. Exemple correct :",
            "    DEEPGRAM_API_KEY=ab12cd34ef...",
            "Exemple CASSÉ (commande PowerShell collée) :",
            '    DEEPGRAM_API_KEY=$env:DEEPGRAM_API_KEY = "ab12..."',
        ]
        _warn("Configuration .env suspecte", "\n".join(lines))

    # Clé LLM du backend choisi : présente et pas un placeholder ? Sinon Crush
    # démarre mais ne peut PAS répondre — le cas « ça marche pas » le plus déroutant.
    env: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    backend = (env.get("API_BACKEND") or "anthropic").lower()
    key_name = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "mistral": "MISTRAL_API_KEY",
    }.get(backend)
    if key_name:
        val = env.get(key_name, "")
        if not val or "..." in val or len(val) < 20:
            _warn(
                "Clé LLM manquante ou non remplie",
                f"""
API_BACKEND={backend} mais {key_name} est vide ou encore à sa valeur d'exemple.
CONSÉQUENCE : Crush va démarrer, mais ne pourra PAS répondre (ni chat, ni voix).
FIX : mets ta vraie clé {key_name} dans .env (la clé brute, sans guillemets).
""",
            )
        else:
            _check_llm_key_live(backend, key_name, val)
    return True


# Endpoint /models : valide la clé SANS consommer de crédits (≠ une vraie requête).
_LLM_MODELS = {
    "anthropic": (
        "https://api.anthropic.com/v1/models",
        {"x-api-key": "{key}", "anthropic-version": "2023-06-01"},
    ),
    "openai": ("https://api.openai.com/v1/models", {"Authorization": "Bearer {key}"}),
    "mistral": ("https://api.mistral.ai/v1/models", {"Authorization": "Bearer {key}"}),
}


def _check_llm_key_live(backend: str, key_name: str, key: str) -> None:
    """Valide la clé LLM en vrai (appel /models). Non bloquant, offline-safe.

    Distingue la clé ERRONÉE (401/403) du QUOTA/CRÉDITS (429). Un /models ne
    consomme pas de tokens : il valide la clé mais NE détecte PAS un solde épuisé
    (ça, ça n'apparaît qu'à une vraie requête → on le précise dans le message 429).
    """
    entry = _LLM_MODELS.get(backend)
    if entry is None:
        return
    url, headers_tmpl = entry
    headers = {k: v.replace("{key}", key) for k, v in headers_tmpl.items()}
    try:
        urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=6)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            _warn(
                "Clé LLM refusée par le fournisseur",
                f"""
{key_name} est remplie mais {backend} la REFUSE (HTTP {e.code}).
CAUSE : clé erronée, révoquée, expirée, ou copiée incomplètement.
CONSÉQUENCE : Crush démarre mais ne pourra PAS répondre.
FIX : régénère une clé sur le tableau de bord {backend} et remplace {key_name} dans .env.
""",
            )
        elif e.code == 429:
            _warn(
                "Quota / crédits LLM atteints",
                f"""
{backend} répond 429 sur {key_name} : limite de débit atteinte, ou plus de crédits.
FIX : vérifie ton solde / ta facturation sur le tableau de bord {backend}, ou change
de backend (API_BACKEND) le temps de recharger.
""",
            )
        # autres codes (5xx, etc.) : transitoire côté fournisseur, on n'alarme pas.
    except Exception:
        # Réseau coupé / offline / DNS : on NE bloque PAS (mode hors-ligne légitime).
        pass


# ── 4. Port de l'API ──────────────────────────────────────────────────────────


def check_port() -> bool:
    port = int(os.getenv("PORT", "8000"))
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        _err(
            f"Port {port} déjà utilisé",
            f"""
Le port {port} est occupé : soit une instance de Crush tourne déjà, soit un
autre programme l'utilise. L'API ne pourra pas démarrer dessus.

FIX :
  - ferme l'instance précédente (Ctrl-C dans son terminal), ou
  - tue le processus qui écoute sur {port}, ou
  - change PORT=… dans .env pour un autre port libre.
""",
        )
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


# ── Orchestration ─────────────────────────────────────────────────────────────


# ── 5. Assets front rapatriés ─────────────────────────────────────────────────

# Runtimes WASM MediaPipe et modèles, ~64 Mo, hors git et récupérés par
# scripts/vendor_assets.py. Deux situations très différentes :
#   - absents  -> reconnaissance faciale/gestuelle indisponible, mais l'assistant
#                 fonctionne : simple avertissement ;
#   - présents mais empreinte différente -> du code qui sera EXÉCUTÉ dans le
#                 navigateur ne correspond plus à ce qui a été vérifié : bloquant.
_VENDOR_DIR = Path(__file__).resolve().parents[1] / "interfaces/ui/static/vendor"
_VENDOR_LOCK = Path(__file__).resolve().parents[3] / "scripts/vendor_assets.lock.json"


def check_vendor_assets() -> bool:
    if not _VENDOR_LOCK.exists():
        return True  # dépôt sans le verrou : rien à vérifier, on ne bloque pas

    try:
        expected = json.loads(_VENDOR_LOCK.read_text(encoding="utf-8")).get("sha256", {})
    except (OSError, ValueError) as e:
        _warn("Verrou des assets front illisible", f"{_VENDOR_LOCK.name} : {e}")
        return True

    missing: list[str] = []
    altered: list[str] = []
    for rel, digest in sorted(expected.items()):
        f = _VENDOR_DIR / rel
        if not f.is_file():
            missing.append(rel)
            continue
        h = hashlib.sha256()
        with f.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != digest:
            altered.append(rel)

    if altered:
        lines = ["Des assets servis au navigateur ne correspondent plus à leur empreinte :", ""]
        lines += [f"  - {rel}" for rel in altered]
        lines += [
            "",
            "POURQUOI C'EST BLOQUANT : ces fichiers sont du WebAssembly et du JS",
            "EXÉCUTÉS dans ton navigateur. Une empreinte différente veut dire un",
            "contenu différent de celui vérifié à la release — corruption ou",
            "substitution. On ne les sert pas.",
            "",
            "FIX :",
            "    supprime src/crush/interfaces/ui/static/vendor/mediapipe/",
            "    puis  python scripts/vendor_assets.py",
        ]
        _err("Assets front altérés", "\n".join(lines))
        return False

    if missing:
        _warn(
            "Assets front absents",
            f"{len(missing)} fichier(s) MediaPipe manquant(s) sur {len(expected)}.\n"
            "La reconnaissance faciale, la séquence de réveil et la détection de\n"
            "gestes seront indisponibles ; le reste de l'assistant fonctionne.\n\n"
            "FIX :  python scripts/vendor_assets.py",
        )

    return True


def main() -> int:
    print(_c("2", "Vérification de l'environnement Crush…"), file=sys.stderr)

    fatal = False
    for chk in (check_python, check_deps, check_env, check_port, check_vendor_assets):
        try:
            if not chk():
                fatal = True
        except Exception as e:  # un check ne doit JAMAIS faire planter le préflight
            _err(f"Échec de la vérification « {chk.__name__} »", str(e))
            fatal = True

    if fatal:
        msg = "Démarrage annulé : corrige le(s) problème(s) ci-dessus, puis relance."
        print("\n" + _c("91", msg), file=sys.stderr)
        return 1

    print(_c("92", "[OK] Environnement vérifié — démarrage de l'API."), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
