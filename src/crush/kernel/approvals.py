# Copyright (C) 2026 Barthélemy Houot
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Système d'approbation par catégorie — loader kernel.

Chaque catégorie peut être : "always", "ask", "never"
  always → Crush exécute sans demander
  ask    → Demande confirmation avant d'exécuter
  never  → Refuse d'exécuter cette catégorie

Loader pur : dataclass + JSON persisté dans `MEMORY_DATA_DIR`.

L'état vivait auparavant dans `config/approvals.json`, **fichier suivi par
git**. Chaque décision prise depuis l'interface salissait donc le dépôt, et le
prochain `git pull` sur la machine de production partait en conflit sur un
fichier que personne n'avait édité à la main. Un choix d'approbation est propre
à une machine, comme les permissions runtime : il appartient à `memory_data/`,
qui est ignoré par git.

La migration est automatique et se fait une seule fois : si l'ancien fichier
existe et que le nouveau non, ses valeurs sont reprises. Rien à faire à la main
sur une installation existante.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

from crush.kernel.paths import CONFIG_DIR, MEMORY_DATA_DIR
from crush.kernel.persistance import ecrire_atomique


class ApprovalMode(StrEnum):
    ALWAYS = "always"
    ASK = "ask"
    NEVER = "never"


@dataclass
class ApprovalConfig:
    """Configuration des approbations par catégorie."""

    system_shutdown: ApprovalMode = ApprovalMode.ASK
    system_restart: ApprovalMode = ApprovalMode.ASK

    file_read: ApprovalMode = ApprovalMode.ALWAYS
    file_write: ApprovalMode = ApprovalMode.ASK
    file_delete: ApprovalMode = ApprovalMode.ASK

    app_launch: ApprovalMode = ApprovalMode.ALWAYS
    app_close: ApprovalMode = ApprovalMode.ALWAYS

    web_search: ApprovalMode = ApprovalMode.ALWAYS
    web_navigate: ApprovalMode = ApprovalMode.ALWAYS
    web_agent: ApprovalMode = ApprovalMode.ASK

    email_draft: ApprovalMode = ApprovalMode.ALWAYS
    email_send: ApprovalMode = ApprovalMode.ASK

    code_write: ApprovalMode = ApprovalMode.ASK
    agent_mission: ApprovalMode = ApprovalMode.ALWAYS

    printer_slice: ApprovalMode = ApprovalMode.ASK
    printer_print: ApprovalMode = ApprovalMode.ASK
    fusion_create: ApprovalMode = ApprovalMode.ALWAYS
    fusion_modify: ApprovalMode = ApprovalMode.ASK
    fusion_delete: ApprovalMode = ApprovalMode.ASK

    smart_home_read: ApprovalMode = ApprovalMode.ALWAYS
    smart_home_write: ApprovalMode = ApprovalMode.ALWAYS


CONFIG_FILE = MEMORY_DATA_DIR / "approvals.json"

# Ancien emplacement, suivi par git. Lu une seule fois, pour ne pas perdre les
# choix d'une installation existante ; jamais réécrit.
_ANCIEN_FICHIER = CONFIG_DIR / "approvals.json"


def _depuis_json(chemin: Path) -> ApprovalConfig | None:
    """Relit une configuration, ou None si le fichier est absent ou abîmé.

    Ne lève jamais : une approbation illisible ne doit pas empêcher le service
    de démarrer. Les défauts sont conservateurs, donc retomber dessus est sûr.
    """
    try:
        data = json.loads(chemin.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return ApprovalConfig(
        **{
            k: ApprovalMode(v)
            for k, v in data.items()
            if hasattr(ApprovalConfig, k)
            and isinstance(v, str)
            and v in tuple(ApprovalMode)
        }
    )


def load_approval_config() -> ApprovalConfig:
    """Charge l'état, en reprenant l'ancien emplacement au premier démarrage."""
    config = _depuis_json(CONFIG_FILE)
    if config is not None:
        return config

    # Reprise unique de l'ancien fichier. On ne le supprime pas : un dépôt
    # propre s'obtient par `git rm --cached`, pas en effaçant sous les pieds
    # de l'utilisateur un fichier qu'il croit encore actif.
    ancienne = _depuis_json(_ANCIEN_FICHIER)
    config = ancienne if ancienne is not None else ApprovalConfig()
    try:
        save_approval_config(config)
    except OSError:
        # Disque en lecture seule ou droits manquants : on sert la
        # configuration en mémoire plutôt que de refuser de démarrer.
        pass
    return config


def save_approval_config(config: ApprovalConfig) -> None:
    ecrire_atomique(
        CONFIG_FILE,
        json.dumps(asdict(config), indent=2, ensure_ascii=False),
    )


approval_config = load_approval_config()
