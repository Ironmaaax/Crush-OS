# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .


"""Factory `get_backend()` — sélection runtime du backend d'exécution.

Engine-aware : instancie les classes `DockerBackend/LocalBackend/SSHBackend/
RemoteBackend` de `crush.engine.mission.backends`. La configuration (type
sélectionné, paramètres SSH) vient de `crush.kernel.backends` (loader pur).

Migré depuis l'ancien shim racine `config/backends.py::get_backend` en
Phase F.7. Le split kernel↔engine casse le cycle RÈGLE 2 attrapé par le
4e contrat import-linter (`capabilities.tools.subagent → config.backends →
crush.engine.mission.backends`).
"""

from __future__ import annotations

import uuid

from loguru import logger

from crush.engine.mission.backends import (
    DockerBackend,
    LocalBackend,
    RemoteBackend,
    SSHBackend,
)
from crush.kernel.backends import BackendType, load_backends_config
from crush.kernel.settings import settings


def get_backend(
    workspace_path: str,
    docker_executor: object | None = None,
) -> object | None:
    """Retourne l'ExecutionBackend configuré pour ce workspace.

    Retourne None si aucun backend sûr n'est disponible.
    Le docker_executor (si fourni) doit être déjà démarré.
    """

    config = load_backends_config()

    if config.default_backend in (BackendType.AUTO, BackendType.DOCKER):
        if docker_executor is not None and settings.docker_enabled:
            return DockerBackend(docker_executor)

        if config.default_backend == BackendType.DOCKER:
            logger.error(
                "Backend DOCKER configuré mais non disponible "
                "(docker_executor=None ou docker_enabled=False)"
            )
            return None

        return LocalBackend(workspace_path)

    if config.default_backend == BackendType.LOCAL:
        return LocalBackend(workspace_path)

    if config.default_backend == BackendType.SSH:
        ssh = config.ssh
        if not ssh.host or not ssh.user:
            logger.error("Backend SSH : host ou user manquant dans config/backends.json")
            return None
        return SSHBackend(ssh.host, ssh.user, ssh.port, ssh.key_path, ssh.remote_workdir)

    if config.default_backend == BackendType.REMOTE:
        return RemoteBackend(config.remote_provider)

    return None


async def get_backend_ephemere(workspace_path: str) -> tuple[object | None, object | None]:
    """Backend prêt à l'emploi, avec son conteneur s'il en faut un.

    Retourne `(backend, executeur_a_arreter)`. L'appelant DOIT arrêter
    l'exécuteur rendu, s'il n'est pas None.

    Pourquoi cette variante. `get_backend()` attend qu'on lui FOURNISSE un
    `docker_executor` déjà démarré. Seul le worker de mission en crée un, par
    projet. `ScriptRPCTool`, qui n'a pas de projet, appelait donc
    `get_backend()` sans exécuteur : avec `default_backend: docker`, la branche
    Docker rendait `None` par construction, et l'outil concluait à une
    configuration fautive. Il renvoyait l'utilisateur corriger un fichier déjà
    correct — une erreur pire qu'une panne, puisqu'elle envoie chercher ailleurs.

    Le conteneur est créé pour la durée d'un appel puis détruit, ce qui
    correspond à la sémantique de l'outil : un script ponctuel, sans état à
    conserver entre deux exécutions.
    """
    from crush.engine.mission.docker_executor import DockerExecutor

    config = load_backends_config()
    veut_docker = config.default_backend in (BackendType.AUTO, BackendType.DOCKER)

    if veut_docker and settings.docker_enabled and await DockerExecutor.is_available():
        executeur = DockerExecutor(
            workspace_path=workspace_path,
            # Identifiant propre à cet appel : deux scripts lancés en parallèle
            # ne doivent pas se disputer le même nom de conteneur.
            project_id=f"script-{uuid.uuid4().hex[:8]}",
            network=settings.docker_network,
        )
        await executeur.start()
        return DockerBackend(executeur), executeur

    # Docker demandé mais injoignable : on ne retombe PAS en silence sur un
    # backend local. Le pont RPC réécrit les chemins vers /workspace et le
    # local ne fournit pas ce montage — `_diagnostic_pont_rpc` le dira
    # précisément à l'appelant.
    return get_backend(workspace_path), None
