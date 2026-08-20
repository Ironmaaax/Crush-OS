# Copyright (C) 2026 Barthélemy Houot
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Permissions runtime accordées par l'utilisateur depuis l'UI, persistées.

L'état vivait uniquement en mémoire : chaque redémarrage du service (donc
chaque déploiement) reverrouillait screen/camera/files. L'utilisateur cochait
« accès fichiers », le service redémarrait, et read_file/find_files
redevenaient morts sans que rien ne le signale.

Le fichier vit dans `MEMORY_DATA_DIR` (données utilisateur, gitignoré) et non
dans `CONFIG_DIR` : `config/` est tracké en git, or une permission est un choix
propre à une machine — la committer l'imposerait à tous les clones.
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from crush.kernel.paths import MEMORY_DATA_DIR
from crush.kernel.persistance import ecrire_atomique as _write_atomic

PERMISSION_KEYS: frozenset[str] = frozenset({"microphone", "screen", "camera", "files"})

_DEFAULTS: dict[str, bool] = {
    "microphone": True,
    "screen": False,
    "camera": False,
    "files": False,
}

STATE_FILE: Path = MEMORY_DATA_DIR / "permissions.json"


class PermissionStore:
    """Permissions runtime accordées par l'utilisateur depuis l'UI."""

    def __init__(self, path: Path | None = None) -> None:
        """`path=None` construit un store éphémère, sans aucune I/O.

        Indispensable pour les tests et les instances jetables : un
        `PermissionStore()` de passage ne doit jamais écraser l'état réel de
        la machine, ni en hériter.
        """
        self._path = path
        self._state: dict[str, bool] = dict(_DEFAULTS)
        self._stamp: tuple[int, int] | None = None
        self._storage_error: str | None = None
        if path is not None:
            self._load()

    # ── Lecture ───────────────────────────────────────────────────────────

    def get(self, key: str) -> bool:
        self._refresh_if_changed()
        # Une clé inconnue n'est pas une capacité gardée : elle vaut « autorisé »
        # (comportement historique dont dépendent les appelants non listés ici).
        return self._state.get(key, True)

    def all(self) -> dict[str, bool]:
        self._refresh_if_changed()
        return dict(self._state)

    @property
    def storage_error(self) -> str | None:
        """Message à afficher tel quel si le dernier enregistrement a échoué.

        `None` quand tout va bien. Sert à ne pas mentir à l'utilisateur : sans
        lui, une permission cochée alors que le disque refuse l'écriture
        semblerait acquise et disparaîtrait au redémarrage.
        """
        return self._storage_error

    # ── Écriture ──────────────────────────────────────────────────────────

    def set(self, key: str, value: bool) -> bool:
        """Applique et persiste une permission. Retourne False si la clé est inconnue."""
        if key not in PERMISSION_KEYS:
            return False
        self._state[key] = bool(value)
        self._persist()
        return True

    def reload(self) -> None:
        """Force une relecture du fichier (défauts si absent ou illisible)."""
        self._state = dict(_DEFAULTS)
        self._load()

    # ── Persistance ───────────────────────────────────────────────────────

    def _load(self) -> None:
        if self._path is None:
            return
        self._stamp = _stamp_of(self._path)
        self._state.update(_read_state(self._path))

    def _refresh_if_changed(self) -> None:
        """Relit le fichier s'il a bougé depuis notre dernière lecture.

        L'API web et l'agent vocal sont deux processus distincts autour du même
        fichier : sans cette relecture, une permission cochée dans l'UI
        resterait invisible de l'autre côté jusqu'au prochain redémarrage.
        Un `stat` par appel est négligeable (le daemon vision interroge à 2 fps).
        """
        if self._path is None:
            return
        stamp = _stamp_of(self._path)
        # stamp None = fichier momentanément illisible : on garde l'état courant
        # plutôt que de révoquer des permissions sur un incident passager.
        if stamp is None or stamp == self._stamp:
            return
        self._stamp = stamp
        self._state = {**_DEFAULTS, **_read_state(self._path)}

    def _persist(self) -> None:
        if self._path is None:
            return
        payload = json.dumps(self._state, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        try:
            _write_atomic(self._path, payload)
        except OSError as exc:
            self._storage_error = (
                f"Permission appliquée mais non enregistrée dans {self._path} ({exc}) : "
                f"elle sera perdue au prochain redémarrage du service. "
                f"Vérifie les droits d'écriture sur {self._path.parent}."
            )
            logger.error(self._storage_error)
            return
        self._storage_error = None
        self._stamp = _stamp_of(self._path)


def _stamp_of(path: Path) -> tuple[int, int] | None:
    """Empreinte bon marché du fichier — None s'il est absent ou illisible."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _read_state(path: Path) -> dict[str, bool]:
    """Lit les permissions persistées. Ne lève jamais.

    Un fichier absent, tronqué ou bricolé à la main doit dégrader vers les
    valeurs par défaut, pas empêcher le service de démarrer. Seules les clés
    connues et les vrais booléens sont retenus : le reste est ignoré, ce qui
    évite d'injecter une capacité fantôme dans l'état.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning(f"Permissions illisibles dans {path} ({exc}) — valeurs par défaut")
        return {}
    if not isinstance(raw, dict):
        logger.warning(f"Permissions malformées dans {path} (objet attendu) — valeurs par défaut")
        return {}
    return {k: v for k, v in raw.items() if k in PERMISSION_KEYS and isinstance(v, bool)}


permissions = PermissionStore(STATE_FILE)
