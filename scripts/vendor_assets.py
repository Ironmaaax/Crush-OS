# Copyright (C) 2026 Maxime Song

"""Rapatriement des assets front lourds — MediaPipe et ses modèles.

Pourquoi ce script plutôt qu'un `vendor/` committé : les runtimes WASM de
MediaPipe et les modèles pèsent ~65 Mo. Dans git ils alourdiraient chaque clone
pour toujours. Ici ils sont téléchargés une fois par machine, à l'installation.

Ce que ça change pour l'interface : plus aucun appel à `cdn.jsdelivr.net` ni
`storage.googleapis.com` au chargement d'une page. Le seul accès réseau restant
est celui de ce script, exécuté explicitement.

Chaque asset est épinglé à une version exacte et vérifié par empreinte SHA-256
lue dans `vendor_assets.lock.json`. Sans cette vérification on n'aurait fait que
déplacer le problème : du code tiers non authentifié, exécuté dans le navigateur.

    python scripts/vendor_assets.py              # télécharge ce qui manque, vérifie tout
    python scripts/vendor_assets.py --check      # vérifie sans rien télécharger
    python scripts/vendor_assets.py --force      # re-télécharge tout
    python scripts/vendor_assets.py --update-lock  # regénère les empreintes (release only)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Versions épinglées. Les changer impose un --update-lock, donc une relecture
# consciente des empreintes : c'est voulu.
TASKS_VISION_NEW = "0.10.14"  # mediapipe_vision.js + module ESM de index.html
TASKS_VISION_OLD = "0.10.6"   # wake_sequence.js — API FaceLandmarker de la sequence de reveil
FACE_MESH = "0.4.1633559619"  # wakeup.js — solution FaceMesh historique

_JSD = "https://cdn.jsdelivr.net/npm/@mediapipe"
_MODELS = "https://storage.googleapis.com/mediapipe-models"

VENDOR_DIR = Path(__file__).resolve().parents[1] / "src/crush/interfaces/ui/static/vendor"
LOCK_FILE = Path(__file__).resolve().parent / "vendor_assets.lock.json"

_WASM_FILES = (
    "vision_wasm_internal.js",
    "vision_wasm_internal.wasm",
    "vision_wasm_nosimd_internal.js",
    "vision_wasm_nosimd_internal.wasm",
)

_FACE_MESH_FILES = (
    "face_mesh.js",
    "face_mesh.binarypb",
    "face_mesh_solution_packed_assets.data",
    "face_mesh_solution_packed_assets_loader.js",
    "face_mesh_solution_simd_wasm_bin.data",
    "face_mesh_solution_simd_wasm_bin.js",
    "face_mesh_solution_simd_wasm_bin.wasm",
    "face_mesh_solution_wasm_bin.js",
    "face_mesh_solution_wasm_bin.wasm",
)


def _assets() -> dict[str, str]:
    """Chemin local relatif à VENDOR_DIR -> URL amont."""
    out: dict[str, str] = {}

    for ver in (TASKS_VISION_NEW, TASKS_VISION_OLD):
        root = f"mediapipe/tasks-vision-{ver}"
        out[f"{root}/vision_bundle.mjs"] = f"{_JSD}/tasks-vision@{ver}/vision_bundle.mjs"
        for name in _WASM_FILES:
            out[f"{root}/wasm/{name}"] = f"{_JSD}/tasks-vision@{ver}/wasm/{name}"

    root = f"mediapipe/face_mesh-{FACE_MESH}"
    for name in _FACE_MESH_FILES:
        out[f"{root}/{name}"] = f"{_JSD}/face_mesh@{FACE_MESH}/{name}"

    out["mediapipe/models/blaze_face_short_range.tflite"] = (
        f"{_MODELS}/face_detector/blaze_face_short_range/float16/1/"
        "blaze_face_short_range.tflite"
    )
    out["mediapipe/models/gesture_recognizer.task"] = (
        f"{_MODELS}/gesture_recognizer/gesture_recognizer/float16/1/gesture_recognizer.task"
    )
    out["mediapipe/models/face_landmarker.task"] = (
        f"{_MODELS}/face_landmarker/face_landmarker/float16/1/face_landmarker.task"
    )
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=300) as resp, tmp.open("wb") as fh:
            while chunk := resp.read(1 << 20):
                fh.write(chunk)
    except urllib.error.URLError as exc:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"telechargement impossible : {url} ({exc})") from exc
    tmp.replace(dest)


def _load_lock() -> dict[str, str]:
    if not LOCK_FILE.exists():
        return {}
    return json.loads(LOCK_FILE.read_text(encoding="utf-8")).get("sha256", {})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="verifie sans telecharger")
    ap.add_argument("--force", action="store_true", help="re-telecharge tout")
    ap.add_argument(
        "--update-lock",
        action="store_true",
        help="telecharge tout et regenere les empreintes",
    )
    args = ap.parse_args()

    assets = _assets()
    lock = {} if args.update_lock else _load_lock()

    if not lock and not args.update_lock:
        print(
            f"ERREUR : {LOCK_FILE.name} absent ou vide. Aucune empreinte de reference,\n"
            "         donc rien ne peut etre verifie. Lancer --update-lock pour le creer.",
            file=sys.stderr,
        )
        return 2

    fresh: dict[str, str] = {}
    missing: list[str] = []
    bad: list[str] = []
    total = 0

    for rel, url in sorted(assets.items()):
        dest = VENDOR_DIR / rel
        expected = lock.get(rel)

        if args.force or args.update_lock or not dest.exists():
            if args.check:
                missing.append(rel)
                continue
            print(f"  telechargement  {rel}")
            _download(url, dest)

        digest = _sha256(dest)
        size = dest.stat().st_size
        total += size

        if args.update_lock:
            fresh[rel] = digest
            print(f"  {digest[:16]}...  {size / 1024:>9.0f} Ko  {rel}")
            continue

        if expected is None:
            bad.append(f"{rel} : absent du verrou")
        elif digest != expected:
            bad.append(f"{rel} : empreinte {digest[:16]}... attendue {expected[:16]}...")

    if args.update_lock:
        LOCK_FILE.write_text(
            json.dumps({"sha256": fresh}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"\n{len(fresh)} empreintes ecrites dans {LOCK_FILE.name}")
        print(f"total {total / 1024 / 1024:.1f} Mo")
        return 0

    if missing:
        print(f"\n{len(missing)} asset(s) absent(s) :", file=sys.stderr)
        for rel in missing:
            print(f"  {rel}", file=sys.stderr)
        print("Lancer `python scripts/vendor_assets.py` pour les recuperer.", file=sys.stderr)
        return 1

    if bad:
        print(f"\n{len(bad)} asset(s) non conforme(s) :", file=sys.stderr)
        for line in bad:
            print(f"  {line}", file=sys.stderr)
        print(
            "Ne pas servir ces fichiers : une empreinte differente signifie un contenu\n"
            "different de celui verifie a la release. Supprimer le dossier et relancer.",
            file=sys.stderr,
        )
        return 1

    print(f"{len(assets)} assets verifies, {total / 1024 / 1024:.1f} Mo — conformes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
