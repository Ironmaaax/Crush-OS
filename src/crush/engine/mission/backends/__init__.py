# Copyright (C) 2026 Barthélemy Houot
# This file is part of CRUSH-OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Package backends — abstraction d'exécution multi-environnement pour Crush."""

from __future__ import annotations

from crush.engine.mission.backends.base import BackendResult, ExecutionBackend
from crush.engine.mission.backends.docker import DockerBackend
from crush.engine.mission.backends.local import LocalBackend
from crush.engine.mission.backends.remote import RemoteBackend
from crush.engine.mission.backends.rpc import RPC_ALLOWED_TOOLS, ScriptRPCRunner
from crush.engine.mission.backends.ssh import SSHBackend

__all__ = [
    "BackendResult",
    "DockerBackend",
    "ExecutionBackend",
    "LocalBackend",
    "RemoteBackend",
    "RPC_ALLOWED_TOOLS",
    "ScriptRPCRunner",
    "SSHBackend",
]
