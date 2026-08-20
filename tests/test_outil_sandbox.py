# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS,   .
 

"""Garde-fous du sandbox : `execute_script` et `spawn_subagent` rendent TOUJOURS la main.

La sonde de production a expiré à 75 s sur `execute_script(script='print(2+2)')`.
Chaque test ici enferme l'appel dans son propre `asyncio.wait_for` : si une
régression réintroduit un blocage, le test échoue en quelques secondes au lieu
de figer la suite.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from crush.capabilities.tools.base import ToolResult
from crush.capabilities.tools.subagent import (
    ScriptRPCTool,
    SpawnSubagentTool,
    _borner,
    _diagnostic_pont_rpc,
)

# Un test qui « vérifie l'absence de blocage » doit lui-même être borné, sinon
# il transforme une régression en suite de tests gelée.
GARDE = 10


class _RegistreFactice:
    """Registre minimal : le pré-vol n'appelle jamais d'outil."""

    def schemas(self) -> list[dict]:
        return []

    async def call(self, name: str, inputs: dict) -> ToolResult:  # pragma: no cover
        raise AssertionError("aucun outil ne doit être appelé dans ces tests")


class _BackendLent:
    """Backend qui ne répond jamais — reproduit le sous-processus coincé."""

    def __init__(self, *, sonde_lente: bool = False, exec_lent: bool = True) -> None:
        self._sonde_lente = sonde_lente
        self._exec_lent = exec_lent

    async def is_available(self) -> bool:
        if self._sonde_lente:
            await asyncio.sleep(3600)
        return True

    async def execute(self, command: str, timeout: int = 60) -> dict:  # noqa: ASYNC109
        if self._exec_lent:
            await asyncio.sleep(3600)
        return {"success": True, "stdout": "", "stderr": "", "returncode": 0}


def _outil(monkeypatch: pytest.MonkeyPatch, backend: object, tmp_path: Path) -> ScriptRPCTool:
    """Construit un ScriptRPCTool dont la factory rend `backend`."""
    # La fabrique rend desormais (backend, executeur_a_arreter) : le conteneur
    # du ScriptRPCTool est ephemere, cree pour l'appel et detruit avec lui.
    # Ici aucun conteneur reel, donc None.
    async def _fabrique(_workspace: str) -> tuple[object, None]:
        return backend, None

    monkeypatch.setattr(
        "crush.capabilities.tools.subagent.get_backend_ephemere",
        _fabrique,
    )
    return ScriptRPCTool(tool_registry=_RegistreFactice(), workspace_path=str(tmp_path))


# ── 1. Bornage du timeout ────────────────────────────────────────────────────


class TestBornageTimeout:
    """Le paramètre `timeout` du schéma doit être honoré ET borné."""

    @pytest.mark.parametrize(
        ("entree", "attendu"),
        [
            (0, 5),  # sous le plancher
            (5, 5),
            (42, 42),  # valeur honorée telle quelle
            (120, 120),
            (86400, 120),  # plafonné
            (-10, 5),
            (None, 60),  # absent → défaut
            ("pas un nombre", 60),
        ],
    )
    def test_borner(self, entree: object, attendu: int) -> None:
        assert _borner(entree, 5, 120, 60) == attendu

    def test_schema_annonce_les_bornes(self) -> None:
        champ = ScriptRPCTool.input_schema["properties"]["timeout"]
        assert champ["default"] == ScriptRPCTool.TIMEOUT_DEFAUT
        assert "120" in champ["description"]

    def test_spawn_expose_aussi_un_timeout(self) -> None:
        assert "timeout" in SpawnSubagentTool.input_schema["properties"]


# ── 2. Diagnostic du pont RPC ────────────────────────────────────────────────


class TestDiagnosticPontRPC:
    """ScriptRPCRunner réécrit les chemins vers /workspace : tout backend qui ne
    fournit pas ce montage est structurellement inopérant, et doit le dire."""

    def test_local_backend_hors_workspace_est_refuse(self, tmp_path: Path) -> None:
        backend = type("LocalBackend", (), {})()
        motif = _diagnostic_pont_rpc(backend, tmp_path)

        assert motif is not None
        # Le message doit désigner le vrai remède, pas le faux : activer
        # ALLOW_UNSANDBOXED_EXEC ne corrige pas la réécriture de chemin.
        assert "ALLOW_UNSANDBOXED_EXEC" in motif
        assert "Docker" in motif

    def test_local_backend_monte_sur_workspace_est_accepte(self) -> None:
        backend = type("LocalBackend", (), {})()
        assert _diagnostic_pont_rpc(backend, Path("/workspace")) is None

    def test_docker_backend_est_accepte(self, tmp_path: Path) -> None:
        backend = type("DockerBackend", (), {})()
        assert _diagnostic_pont_rpc(backend, tmp_path) is None

    @pytest.mark.parametrize("nom", ["SSHBackend", "RemoteBackend"])
    def test_backends_distants_sont_refuses(self, nom: str, tmp_path: Path) -> None:
        backend = type(nom, (), {})()
        motif = _diagnostic_pont_rpc(backend, tmp_path)

        assert motif is not None
        assert "distante" in motif


# ── 3. execute_script rend la main, quoi qu'il arrive ────────────────────────


class TestExecuteScriptNeBloqueJamais:
    @pytest.mark.asyncio
    async def test_absence_de_backend_repond_immediatement(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        outil = _outil(monkeypatch, None, tmp_path)

        debut = time.monotonic()
        res = await asyncio.wait_for(outil.execute(script="print(2+2)"), timeout=GARDE)

        assert res.is_error is True
        assert "backends.json" in res.content
        assert time.monotonic() - debut < 2

    @pytest.mark.asyncio
    async def test_backend_indisponible_explique_le_remede(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        backend = MagicMock()
        backend.__class__ = type("DockerBackend", (MagicMock,), {})
        backend.is_available = AsyncMock(return_value=False)
        outil = _outil(monkeypatch, backend, tmp_path)

        res = await asyncio.wait_for(outil.execute(script="print(2+2)"), timeout=GARDE)

        assert res.is_error is True
        assert "DOCKER_ENABLED" in res.content

    @pytest.mark.asyncio
    async def test_sonde_de_disponibilite_figee_ne_bloque_pas(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`DockerExecutor.is_available()` lance `docker ps` sans timeout."""
        backend = _BackendLent(sonde_lente=True)
        backend.__class__ = type("DockerBackend", (_BackendLent,), {})
        outil = _outil(monkeypatch, backend, tmp_path)
        outil.DELAI_SONDE = 1  # type: ignore[misc]

        debut = time.monotonic()
        res = await asyncio.wait_for(outil.execute(script="print(2+2)"), timeout=GARDE)

        assert res.is_error is True
        assert "sonde de disponibilité" in res.content
        assert time.monotonic() - debut < 5

    @pytest.mark.asyncio
    async def test_sous_processus_coince_est_coupe(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Le cœur de la panne : un backend qui n'honore pas son propre timeout."""
        backend = _BackendLent()
        backend.__class__ = type("DockerBackend", (_BackendLent,), {})
        outil = _outil(monkeypatch, backend, tmp_path)
        outil.MARGE_ARRET = 1  # type: ignore[misc]

        debut = time.monotonic()
        res = await asyncio.wait_for(outil.execute(script="print(2+2)", timeout=5), timeout=GARDE)
        duree = time.monotonic() - debut

        assert res.is_error is True
        assert "interrompu" in res.content
        # 5 s de budget + 1 s de marge : bien en deçà des 75 s de la sonde.
        assert duree < 9

    @pytest.mark.asyncio
    async def test_timeout_absurde_est_plafonne_avant_execution(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Un timeout de 24 h fourni par le LLM ne doit pas atteindre le backend."""
        recu: list[int] = []

        class _BackendTemoin:
            async def is_available(self) -> bool:
                return True

            async def execute(self, command: str, timeout: int = 60) -> dict:  # noqa: ASYNC109
                recu.append(timeout)
                return {"success": True, "stdout": "4", "stderr": "", "returncode": 0}

        backend = _BackendTemoin()
        backend.__class__ = type("DockerBackend", (_BackendTemoin,), {})
        outil = _outil(monkeypatch, backend, tmp_path)

        await asyncio.wait_for(outil.execute(script="print(2+2)", timeout=86400), timeout=GARDE)

        assert recu == [ScriptRPCTool.TIMEOUT_MAX]


# ── 4. L'approbation ne peut pas figer un tour d'outil ───────────────────────


class TestApprobationBornee:
    @pytest.mark.asyncio
    async def test_approbation_sans_reponse_refuse_et_explique(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Cause racine de la panne : ApprovalChecker ASK attend 120 s une
        réponse UI que personne ne donne quand l'appel vient de l'API."""

        class _CheckerMuet:
            async def check(self, category: str, description: str, action_id: str) -> bool:
                await asyncio.sleep(3600)
                return True

        monkeypatch.setattr(
            "crush.capabilities.tools.subagent.get_approval_checker",
            lambda: _CheckerMuet(),
        )
        backend = _BackendLent(exec_lent=False)
        backend.__class__ = type("DockerBackend", (_BackendLent,), {})
        outil = _outil(monkeypatch, backend, tmp_path)
        outil.DELAI_APPROBATION = 1  # type: ignore[misc]

        debut = time.monotonic()
        res = await asyncio.wait_for(outil.execute(script="print(2+2)"), timeout=GARDE)

        assert res.is_error is True
        # Fail-closed : on refuse, et on dit comment débloquer durablement.
        assert "refusée" in res.content
        assert "/api/approvals/config/code_write" in res.content
        assert time.monotonic() - debut < 5

    @pytest.mark.asyncio
    async def test_refus_explicite_reste_un_refus(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        class _CheckerRefus:
            async def check(self, category: str, description: str, action_id: str) -> bool:
                return False

        monkeypatch.setattr(
            "crush.capabilities.tools.subagent.get_approval_checker",
            lambda: _CheckerRefus(),
        )
        backend = _BackendLent(exec_lent=False)
        backend.__class__ = type("DockerBackend", (_BackendLent,), {})
        outil = _outil(monkeypatch, backend, tmp_path)

        res = await asyncio.wait_for(outil.execute(script="print(2+2)"), timeout=GARDE)

        assert res.is_error is True
        assert "refusée" in res.content

    @pytest.mark.asyncio
    async def test_approbation_demandee_apres_le_prevol(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Inutile de réveiller l'utilisateur pour un sandbox qui ne démarrera pas."""
        appels: list[str] = []

        class _CheckerTemoin:
            async def check(self, category: str, description: str, action_id: str) -> bool:
                appels.append(category)
                return True

        monkeypatch.setattr(
            "crush.capabilities.tools.subagent.get_approval_checker",
            lambda: _CheckerTemoin(),
        )
        outil = _outil(monkeypatch, None, tmp_path)

        await asyncio.wait_for(outil.execute(script="print(2+2)"), timeout=GARDE)

        assert appels == []


# ── 5. spawn_subagent : même famille de risque ───────────────────────────────


class TestSpawnSubagentBorne:
    @pytest.mark.asyncio
    async def test_boucle_outils_muette_est_interrompue(self) -> None:
        """respond_tools() est une boucle d'outils sans borne native."""

        async def _jamais(_session: object) -> str:
            await asyncio.sleep(3600)
            return "jamais"

        agent = MagicMock()
        agent.respond_tools = AsyncMock(side_effect=_jamais)
        outil = SpawnSubagentTool(agent=agent)
        # Le plancher de production est de 10 s : un sous-agent a besoin de
        # temps, et l'abaisser durablement le rendrait inutile. On l'abaisse
        # pour CE test seulement, sinon il faudrait attendre 10 s pour verifier
        # une borne qui n'a rien a voir avec sa duree.
        outil.TIMEOUT_MIN = 1

        debut = time.monotonic()
        res = await asyncio.wait_for(
            outil.execute(task="tâche sans fin", timeout=1), timeout=GARDE
        )

        assert res.is_error is True
        assert "interrompu" in res.content
        assert time.monotonic() - debut < 5

    @pytest.mark.asyncio
    async def test_timeout_absurde_est_plafonne(self) -> None:
        agent = MagicMock()
        agent.respond_tools = AsyncMock(return_value="ok")
        outil = SpawnSubagentTool(agent=agent)

        # Le plafond doit s'appliquer sans faire échouer l'appel nominal.
        res = await asyncio.wait_for(
            outil.execute(task="t", timeout=999_999), timeout=GARDE
        )

        assert res.is_error is False

    @pytest.mark.asyncio
    async def test_recursion_de_delegation_est_stoppee(self) -> None:
        """Un sous-agent qui redélègue en boucle brûle du budget sans rien rendre."""
        outil_interne: dict[str, SpawnSubagentTool] = {}

        async def _redelegue(_session: object) -> str:
            res = await outil_interne["outil"].execute(task="encore")
            return res.content

        agent = MagicMock()
        agent.respond_tools = AsyncMock(side_effect=_redelegue)
        outil = SpawnSubagentTool(agent=agent)
        outil_interne["outil"] = outil

        res = await asyncio.wait_for(outil.execute(task="racine", timeout=10), timeout=GARDE)

        assert "Profondeur de délégation maximale" in res.content

    @pytest.mark.asyncio
    async def test_cas_nominal_inchange(self) -> None:
        """Le contrat existant (tests/test_backends.py) ne doit pas bouger."""
        agent = MagicMock()
        agent.respond_tools = AsyncMock(return_value="Résultat de la tâche déléguée.")
        outil = SpawnSubagentTool(agent=agent)

        res = await asyncio.wait_for(outil.execute(task="analyse"), timeout=GARDE)

        assert res.is_error is False
        assert "Sous-agent terminé" in res.content
        assert "Résultat de la tâche déléguée." in res.content
