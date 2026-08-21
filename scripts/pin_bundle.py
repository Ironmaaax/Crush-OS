# Copyright (C) 2026 Maxime Song

"""Épingle la taille et l'empreinte SHA-256 du bundle publié.

Pourquoi ce script : `Compress-Archive` n'est pas déterministe au bit près, donc
l'empreinte du bundle ne peut pas être connue avant le build. Elle est relevée par
le workflow et publiée en sidecar `.sha256` à côté de l'archive. Ce script la
récupère et la fige dans `bundle_download.py`, qui la vérifiera ensuite chez chaque
utilisateur avant d'exécuter les 700 MB de binaires que contient l'archive.

Tant que `BUNDLE_ZIP_SHA256` reste vide, seule la TAILLE est contrôlée — et une
taille identique ne prouve rien sur le contenu. C'est cet écart que ce script ferme.

Aucun téléchargement de l'archive : la taille vient de l'API des releases, le hash
du sidecar. Deux requêtes de quelques kilo-octets.

    python scripts/pin_bundle.py              # epingle la version deja dans le code
    python scripts/pin_bundle.py v0.3.3       # epingle ce tag et met a jour la version
    python scripts/pin_bundle.py --check      # compare sans rien ecrire
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "src/crush/kernel/bundle_download.py"
FALLBACK_REPO = "Ironmaaax/Crush-OS"


def _repo() -> str:
    """owner/name déduit de origin, pour ne pas coder en dur un nom de dépôt."""
    try:
        url = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=ROOT, capture_output=True, text=True, timeout=15, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return FALLBACK_REPO
    m = re.search(r"github\.com[:/]([^/]+/[^/.]+)", url)
    return m.group(1) if m else FALLBACK_REPO


def _get(url: str, *, accept_404: bool = False) -> bytes | None:
    req = urllib.request.Request(url, headers={"Accept": "*/*", "User-Agent": "crush-pin-bundle"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404 and accept_404:
            return None
        raise RuntimeError(f"HTTP {e.code} sur {url}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"reseau indisponible : {url} ({e})") from e


def _current_version() -> str:
    src = TARGET.read_text(encoding="utf-8")
    m = re.search(r'^BUNDLE_RELEASE_VERSION\s*=\s*"([^"]+)"', src, re.M)
    if not m:
        raise RuntimeError("BUNDLE_RELEASE_VERSION introuvable dans bundle_download.py")
    return m.group(1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tag", nargs="?", help="tag de release (defaut : celui du code)")
    ap.add_argument("--check", action="store_true", help="compare sans ecrire")
    args = ap.parse_args()

    repo = _repo()
    tag = args.tag or _current_version()
    asset_name = f"crush-offline-windows-{tag}.zip"

    raw = _get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", accept_404=True)
    if raw is None:
        print(
            f"Aucune release « {tag} » sur {repo}.\n"
            "Le bundle est construit et publie par .github/workflows/build-windows-bundle.yml\n"
            f"au push d'un tag. Pousse le tag, attends la fin du workflow (~15-25 min),\n"
            "puis relance ce script.",
            file=sys.stderr,
        )
        return 1

    rel = json.loads(raw)
    assets = {a["name"]: a for a in rel.get("assets", [])}

    if asset_name not in assets:
        print(
            f"La release « {tag} » existe mais n'a pas d'asset « {asset_name} ».\n"
            f"Assets presents : {', '.join(sorted(assets)) or 'aucun'}\n"
            "Le workflow a probablement echoue avant l'etape de packaging.",
            file=sys.stderr,
        )
        return 1

    size = int(assets[asset_name]["size"])

    sidecar = f"{asset_name}.sha256"
    if sidecar not in assets:
        print(
            f"L'archive est publiee mais le sidecar « {sidecar} » manque.\n"
            "Il est produit par l'etape « Empreinte SHA-256 de l'archive ». Sans lui,\n"
            "l'empreinte ne peut pas etre epinglee sans retelecharger 700 MB.\n"
            "Republie avec un workflow a jour, ou calcule le hash a la main.",
            file=sys.stderr,
        )
        return 1

    body = _get(assets[sidecar]["browser_download_url"])
    assert body is not None
    line = body.decode("ascii", errors="replace").strip()
    m = re.match(r"^([0-9a-fA-F]{64})\s+(\S+)$", line)
    if not m:
        print(f"Sidecar illisible : {line!r}", file=sys.stderr)
        return 1
    digest, named = m.group(1).lower(), m.group(2)
    if named != asset_name:
        print(
            f"Le sidecar nomme « {named} » alors que l'asset est « {asset_name} ».\n"
            "Empreinte non appariee : on n'epingle pas.",
            file=sys.stderr,
        )
        return 1

    print(f"depot   : {repo}")
    print(f"tag     : {tag}")
    print(f"archive : {asset_name}")
    print(f"octets  : {size}")
    print(f"sha256  : {digest}")

    src = TARGET.read_text(encoding="utf-8")
    out = src

    out = re.sub(
        r'^BUNDLE_RELEASE_VERSION\s*=\s*"[^"]*"',
        f'BUNDLE_RELEASE_VERSION = "{tag}"',
        out,
        count=1,
        flags=re.M,
    )

    # Remplace le bloc d'avertissement « taille de l'ancien hebergement » posé
    # tant qu'aucune release propre n'existait : il n'a plus lieu d'être.
    out = re.sub(
        r"(?:^# ATTENTION[^\n]*\n(?:^#[^\n]*\n)*)?^BUNDLE_ZIP_BYTES\s*=\s*[0-9_]+",
        f"BUNDLE_ZIP_BYTES = {size:_}",
        out,
        count=1,
        flags=re.M,
    )

    out = re.sub(
        r'^BUNDLE_ZIP_SHA256\s*=\s*"[^"]*"',
        f'BUNDLE_ZIP_SHA256 = "{digest}"',
        out,
        count=1,
        flags=re.M,
    )

    if out == src:
        print("\nbundle_download.py deja a jour — rien a ecrire.")
        return 0

    if args.check:
        print("\n--check : des valeurs differeraient, rien n'a ete ecrit.")
        return 1

    TARGET.write_text(out, encoding="utf-8")
    print(f"\n{TARGET.relative_to(ROOT)} mis a jour.")
    print("Verifie le diff, puis commit — c'est cette valeur qui protegera")
    print("les installations contre une archive substituee.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
