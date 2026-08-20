# Copyright (C) 2026 Maxime Song
# This file is part of CRUSH-OS, licensed under the GNU AGPL-3.0-or-later.
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Portabilité de la sandbox du Skill Lab (Windows de dev ↔ Debian du Pi).

La sandbox composait `.venv/lib/pythonX.Y/site-packages` à la main — la
disposition POSIX. Sous Windows les paquets vivent dans
`.venv/Lib/site-packages` : le sous-processus de test ne voyait AUCUNE
dépendance et rejetait toute candidate, y compris saine. Un rejet permanent
qui masquait les vraies régressions du Lab.

Ces tests verrouillent les trois propriétés qui rendent la sandbox portable :
  1. Les dépendances sont trouvées sur la plateforme courante, quelle qu'elle
     soit, et une candidate saine passe réellement.
  2. Quand elles manquent, la sandbox le DIT au lieu d'accuser la candidate.
  3. Les chemins hôte et les chemins conteneur ne sont pas confondus : le
     conteneur reste Linux même quand l'hôte est Windows.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import sys
import sysconfig
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from crush.capabilities.skills import lab as lab_module
from crush.capabilities.skills.lab import SkillLab, _dependency_dirs, _host_mount
from crush.capabilities.skills.lifecycle import SkillLifecycle, SkillStatus
from crush.capabilities.skills.synthesizer import SkillSynthesizer
from crush.providers.memory.kernel import MemoryKernel

# ── Fixtures ──────────────────────────────────────────────────────────────────

_SKILL_PY = """\
from __future__ import annotations
from crush.capabilities.skills.base import SkillBase


class PortableProbeSkill(SkillBase):
    \"\"\"Skill saine, utilisée pour prouver que la sandbox sait dire oui.\"\"\"

    @property  # type: ignore[override]
    def SYSTEM_PROMPT(self) -> str:
        return self.metadata.get("system_prompt", "")

    def get_system_prompt(self) -> str:
        return self.SYSTEM_PROMPT.strip()
"""

_SKILL_YAML = 'name: portable-probe\nsystem_prompt: "Prompt non vide."\n'


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def lab(workspace: Path) -> SkillLab:
    kernel = MemoryKernel(workspace / "memory.db")
    lifecycle = SkillLifecycle(db_path=workspace / "memory.db")
    return SkillLab(
        kernel=kernel,
        lifecycle=lifecycle,
        synthesizer=SkillSynthesizer(llm=SimpleNamespace()),  # non sollicité ici
        candidates_dir=workspace / "candidates",
        installed_dir=workspace / "installed",
    )


def _write_candidate(workspace: Path, name: str = "portable-probe") -> Path:
    cand_dir = workspace / "candidates" / name
    cand_dir.mkdir(parents=True)
    (cand_dir / "skill.py").write_text(_SKILL_PY, encoding="utf-8")
    (cand_dir / "skill.yaml").write_text(_SKILL_YAML, encoding="utf-8")
    return cand_dir


# ── 1. Les dépendances sont trouvées sur la plateforme courante ──────────────


def test_dependency_dirs_pointe_sur_le_site_packages_reel() -> None:
    """Le répertoire annoncé doit être celui de l'interpréteur qui tourne —
    et il doit exister, sur Windows comme sur Debian."""
    dirs = _dependency_dirs()
    assert dirs, "aucun répertoire de dépendances annoncé"
    assert Path(sysconfig.get_paths()["purelib"]) in dirs
    assert any(d.is_dir() for d in dirs)


def test_dependency_dirs_ne_suppose_pas_la_disposition_posix() -> None:
    """Le vice d'origine : `lib/pythonX.Y/site-packages` codé en dur. Sous
    Windows le dossier est `Lib/site-packages` et n'existe donc pas."""
    posix_layout = (
        Path(sys.prefix)
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    for found in _dependency_dirs():
        if found.is_dir():
            break
    else:  # pragma: no cover — filet, l'assertion précédente l'aurait dit
        pytest.fail("aucun répertoire de dépendances existant")
    # Sur une plateforme où la disposition POSIX est fausse, la sandbox ne doit
    # surtout pas s'y référer.
    if not posix_layout.is_dir():
        assert posix_layout not in _dependency_dirs()


def test_dependances_reellement_importables_depuis_le_chemin_annonce() -> None:
    """yaml est la dépendance sur laquelle butait SkillBase. Elle doit être
    présente dans l'un des répertoires annoncés."""
    assert any(
        (d / "yaml").is_dir() for d in _dependency_dirs() if d.is_dir()
    ), "yaml introuvable dans les répertoires de dépendances annoncés"


async def test_candidate_saine_passe_le_repli_local(workspace: Path, lab: SkillLab) -> None:
    """Le test de bout en bout du défaut : avant correction, cette candidate
    saine était rejetée à cause de l'environnement, pas de son code."""
    _write_candidate(workspace)
    lab._lifecycle.create_candidate(name="portable-probe")

    record = await lab.test_in_sandbox("portable-probe")

    assert record is not None
    assert record.status == SkillStatus.SANDBOXED_PASS, record.sandbox_notes


async def test_repli_local_utilise_l_interpreteur_courant(
    workspace: Path, lab: SkillLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`python` nu résout l'interpréteur du PATH — sous `uv run` c'est le
    Python de base sans les dépendances du projet, et sur Debian il peut ne pas
    exister du tout. Seul sys.executable est fiable des deux côtés."""
    cand_dir = _write_candidate(workspace)
    captured: dict = {}
    vrai_exec = asyncio.create_subprocess_exec

    async def _spy(program: str, *args: object, **kwargs: object) -> object:
        captured["program"] = program
        captured["env"] = kwargs.get("env")
        return await vrai_exec(program, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spy)
    await lab._run_direct_test(cand_dir)

    assert captured["program"] == sys.executable
    env = captured["env"]
    assert env is not None
    assert env["CRUSH_SANDBOX_CANDIDATE"] == str(cand_dir.resolve())
    # Hôte et sous-processus tournent sur la même plateforme : même séparateur.
    for chemin in env["CRUSH_SANDBOX_DEPS"].split(os.pathsep):
        assert Path(chemin).is_dir()


# ── 2. Environnement cassé ≠ candidate fautive ───────────────────────────────


async def test_dependances_absentes_accusent_la_machine_pas_la_candidate(
    workspace: Path, lab: SkillLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un rejet pour environnement cassé et un rejet pour code fautif appellent
    des actions opposées. Le verdict doit permettre de les distinguer."""
    cand_dir = _write_candidate(workspace)
    monkeypatch.setattr(
        lab_module, "_dependency_dirs", lambda: [workspace / "site-packages-fantome"]
    )

    result = await lab._run_direct_test(cand_dir)

    assert not result.passed
    assert result.environment_error is True
    assert result.layer_failed == "sandbox_env"
    assert "site-packages-fantome" in result.notes
    # Le message ne doit pas laisser croire que la skill est mauvaise.
    assert "candidate n'est pas" in result.notes.lower()


async def test_verdict_environnement_ne_se_confond_pas_avec_un_echec_d_import(
    workspace: Path, lab: SkillLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La couche annoncée doit différer de `import`, qui reste réservée aux
    candidates réellement inimportables (cf. test_skill_lab.py)."""
    cand_dir = workspace / "candidates" / "cassee"
    cand_dir.mkdir(parents=True)
    (cand_dir / "skill.py").write_text("raise RuntimeError('cassée')\n", encoding="utf-8")
    lab._lifecycle.create_candidate(name="cassee")

    faute_candidate = await lab.test_in_sandbox("cassee")
    assert faute_candidate is not None
    assert (faute_candidate.sandbox_notes or "").startswith("[import]")

    _write_candidate(workspace)
    lab._lifecycle.create_candidate(name="portable-probe")
    monkeypatch.setattr(lab_module, "_dependency_dirs", lambda: [workspace / "nulle-part"])
    faute_machine = await lab.test_in_sandbox("portable-probe")

    assert faute_machine is not None
    assert (faute_machine.sandbox_notes or "").startswith("[sandbox_env]")
    # Invariant de sécurité : une sandbox inexploitable n'installe rien.
    assert faute_machine.status == SkillStatus.SANDBOXED_FAIL
    assert lab.promote("portable-probe") is None


# ── 3. Chemins hôte vs chemins conteneur ─────────────────────────────────────


def test_host_mount_ne_laisse_pas_d_antislash() -> None:
    """Le côté gauche d'un `-v` est déjà découpé par des deux-points :
    l'antislash Windows y est une ambiguïté de trop."""
    monte = _host_mount(Path.cwd())
    assert "\\" not in monte
    assert Path(monte).is_dir()


async def test_montage_docker_separe_chemins_hote_et_chemins_conteneur(
    workspace: Path, lab: SkillLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le conteneur est Linux même quand l'hôte est Windows : le côté droit de
    chaque `-v`, et les chemins passés au script, doivent rester POSIX."""
    cand_dir = _write_candidate(workspace)
    monkeypatch.setattr(
        lab_module,
        "settings",
        SimpleNamespace(
            docker_enabled=True,
            docker_memory_limit="512m",
            docker_cpu_limit=1.0,
            docker_base_image="python:3.11-slim",
        ),
    )

    async def _docker_dispo() -> bool:
        return True

    monkeypatch.setattr(lab_module.DockerExecutor, "is_available", _docker_dispo)

    captured: dict = {}

    async def _faux_docker(program: str, *args: object, **kwargs: object) -> object:
        argv = [program, *args]
        captured["argv"] = argv

        # Le verdict n'est accepte que signe du jeton a usage unique. On le
        # relit dans l'argv plutot que de le coder en dur : cela verifie du
        # meme coup qu'il est bien transmis au conteneur, sans quoi le banc
        # ne pourrait pas signer et aucun test ne pourrait jamais passer.
        jeton = next(
            (str(a).split("=", 1)[1] for a in argv if str(a).startswith("CRUSH_SANDBOX_NONCE=")),
            "",
        )
        captured["jeton"] = jeton
        verdict = json.dumps({"layer": "ok", "ok": True, "notes": "", "nonce": jeton})

        class _Proc:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return verdict.encode(), b""

        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _faux_docker)
    result = await lab._run_sandbox_test(cand_dir)
    assert result.passed
    assert captured["jeton"], "le jeton de signature doit etre transmis au conteneur"

    argv = captured["argv"]
    montages = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert montages, "aucun montage -v construit"
    for montage in montages:
        cible = montage.rsplit(":", 1)[0].rsplit(":", 1)[1]
        assert cible.startswith("/"), f"cible conteneur non POSIX : {montage}"
        assert "\\" not in cible

    variables = dict(
        argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-e"
    )
    for cle in ("CRUSH_SANDBOX_CANDIDATE", "CRUSH_SANDBOX_SRC", "CRUSH_SANDBOX_DEPS"):
        assert variables[cle].startswith("/"), f"{cle} doit être un chemin conteneur"
        assert "\\" not in variables[cle]
    # Le séparateur de liste est celui du conteneur Linux, jamais celui de
    # l'hôte : sous Windows os.pathsep vaut ";" et casserait le sys.path.
    assert ";" not in variables["CRUSH_SANDBOX_DEPS"]

    # Le côté HÔTE, lui, désigne bien un répertoire réel de cette machine.
    hote_candidate = montages[0].rsplit(":", 2)[0]
    assert Path(hote_candidate).resolve() == cand_dir.resolve()


async def test_montage_docker_supporte_deux_repertoires_de_dependances(
    workspace: Path, lab: SkillLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cas Debian : sur un Python système, purelib et platlib sont deux
    dossiers distincts. Les deux doivent arriver dans le sys.path du conteneur,
    séparés à la mode Linux."""
    cand_dir = _write_candidate(workspace)
    pur = workspace / "dist-packages-pur"
    plat = workspace / "dist-packages-compile"
    pur.mkdir()
    plat.mkdir()
    monkeypatch.setattr(
        lab_module,
        "settings",
        SimpleNamespace(
            docker_enabled=True,
            docker_memory_limit="512m",
            docker_cpu_limit=1.0,
            docker_base_image="python:3.11-slim",
        ),
    )

    async def _docker_dispo() -> bool:
        return True

    monkeypatch.setattr(lab_module.DockerExecutor, "is_available", _docker_dispo)
    monkeypatch.setattr(lab_module, "_dependency_dirs", lambda: [pur, plat])

    captured: dict = {}

    async def _faux_docker(program: str, *args: object, **kwargs: object) -> object:
        argv = [program, *args]
        captured["argv"] = argv

        # Le verdict n'est accepte que signe du jeton a usage unique. On le
        # relit dans l'argv plutot que de le coder en dur : cela verifie du
        # meme coup qu'il est bien transmis au conteneur, sans quoi le banc
        # ne pourrait pas signer et aucun test ne pourrait jamais passer.
        jeton = next(
            (str(a).split("=", 1)[1] for a in argv if str(a).startswith("CRUSH_SANDBOX_NONCE=")),
            "",
        )
        captured["jeton"] = jeton
        verdict = json.dumps({"layer": "ok", "ok": True, "notes": "", "nonce": jeton})

        class _Proc:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return verdict.encode(), b""

        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _faux_docker)
    await lab._run_sandbox_test(cand_dir)

    argv = captured["argv"]
    montages = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    hotes = [m.rsplit(":", 2)[0] for m in montages]
    assert _host_mount(pur) in hotes
    assert _host_mount(plat) in hotes

    variables = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-e")
    cibles = variables["CRUSH_SANDBOX_DEPS"].split(":")
    assert len(cibles) == 2, cibles
    # Deux points de montage distincts : le second n'écrase pas le premier.
    assert len(set(cibles)) == 2
    for cible in cibles:
        assert f"{cible}:ro" in " ".join(montages)


async def test_docker_sans_dependances_locales_annonce_l_environnement(
    workspace: Path, lab: SkillLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Même diagnostic côté Docker : rien à monter → on le dit, on ne lance
    pas un conteneur qui rejettera la candidate pour une mauvaise raison."""
    cand_dir = _write_candidate(workspace)
    monkeypatch.setattr(
        lab_module,
        "settings",
        SimpleNamespace(
            docker_enabled=True,
            docker_memory_limit="512m",
            docker_cpu_limit=1.0,
            docker_base_image="python:3.11-slim",
        ),
    )

    async def _docker_dispo() -> bool:
        return True

    monkeypatch.setattr(lab_module.DockerExecutor, "is_available", _docker_dispo)
    monkeypatch.setattr(lab_module, "_dependency_dirs", lambda: [workspace / "absent"])

    # Ne doit jamais être appelé : le diagnostic tombe avant tout `docker run`.
    async def _interdit(*args: object, **kwargs: object) -> None:  # pragma: no cover
        pytest.fail("docker run lancé alors que les dépendances manquent")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _interdit)

    result = await lab._run_sandbox_test(cand_dir)
    assert result.environment_error is True
    assert result.layer_failed == "sandbox_env"


# ── 4. Garde-fou contre la réintroduction du vice ────────────────────────────


def test_le_source_ne_recode_plus_de_disposition_posix_en_dur() -> None:
    """Filet anti-régression sur le vice d'origine : un chemin de
    site-packages composé segment par segment."""
    source = Path(lab_module.__file__).read_text(encoding="utf-8")
    assert '"site-packages"' not in source
    assert '/ ".venv"' not in source


def test_le_repli_local_ne_lance_pas_un_python_du_path() -> None:
    """Garde-fou ciblé : le `"python"` du montage Docker est légitime — c'est
    l'interpréteur DU CONTENEUR. Celui du repli local ne l'est pas.

    Contrôle sur l'AST et non sur le texte : le nom de l'exécutable lancé est
    ce qui compte, pas les commentaires qui l'entourent.
    """
    arbre = ast.parse(textwrap.dedent(inspect.getsource(SkillLab._run_direct_test)))
    lances = [
        noeud.args[0]
        for noeud in ast.walk(arbre)
        if isinstance(noeud, ast.Call)
        and isinstance(noeud.func, ast.Attribute)
        and noeud.func.attr == "create_subprocess_exec"
        and noeud.args
    ]
    assert lances, "aucun sous-processus lancé par le repli local"
    for executable in lances:
        assert not isinstance(executable, ast.Constant), (
            f"exécutable codé en dur ({executable.value!r}) au lieu de sys.executable"
        )
        assert ast.unparse(executable) == "sys.executable"
