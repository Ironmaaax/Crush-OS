# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Skill Lab (CDC §7) — génération + test sandbox + cycle de vie.

Pipeline complète :
  signal `skill_candidate_proposal` (PHASE 2)
    → SkillLab.propose_from_event(event_id)
    → SkillSynthesizer.propose_skill_candidate(trajectory) (zone tampon)
    → SkillLab.test_in_sandbox(name) (test générique en Docker)
    → si test vert : status SANDBOXED_PASS, attend validation humaine
    → si test rouge : status SANDBOXED_FAIL (REJET AUTOMATIQUE, audit)
    → après validation humaine : SkillLab.promote(name) → ACTIVE,
      déplace candidates/{name}/ → installed/{name}/, reload SkillRegistry

GATE TEST-VERT-SINON-REJET : c'est le cœur dur de la phase. Une skill qui
échoue son test sandbox n'est JAMAIS installée. C'est l'analogue de la couche
sémantique du verifier PHASE 1.

Le test sandbox est GÉNÉRIQUE (décision Q-D=a) :
  1. Le fichier skill.py s'importe sans erreur.
  2. La classe (subclass de SkillBase) s'instancie sans crash.
  3. `get_system_prompt()` retourne une chaîne non-vide.
  4. (Si get_tools() retourne) chaque tool a `name`, `description`,
     `input_schema` valides.

Aucune skill ne modifie le core (CDC §7 anti-patterns) — la sandbox Docker
isole tout effet de bord. Le test est read-only sur /workspace.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import sysconfig
import textwrap
import uuid
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from crush.capabilities.skills.lifecycle import SkillLifecycle, SkillRecord, SkillStatus
from crush.capabilities.skills.synthesizer import (
    SKILLS_CANDIDATES_DIR,
    SKILLS_INSTALLED_DIR,
    SkillSynthesizer,
)
from crush.engine.mission.docker_executor import DockerExecutor
from crush.kernel.contracts import MemoryStore as MemoryKernel
from crush.kernel.paths import PROJECT_ROOT
from crush.kernel.settings import settings

# Plafond du nombre d'events skill_candidate_proposal traités par scan.
_MAX_EVENTS_PER_SCAN = 20
# Plafond du nombre de skills générées par scan (cap dur sur les appels LLM).
_MAX_CANDIDATES_PER_SCAN = 5
# Timeout (s) du test sandbox dans Docker.
_SANDBOX_TIMEOUT = 30

# Points de montage DANS le conteneur. Le conteneur est Linux même quand
# l'hôte est Windows : ces trois chemins restent POSIX en toutes circonstances,
# seul le côté hôte du `-v` suit la plateforme de la machine.
_CONTAINER_CANDIDATE_DIR = "/workspace/candidate"
_CONTAINER_SRC_DIR = "/crush_src"
_CONTAINER_DEPS_DIR = "/crush_deps"

# Variables d'environnement par lesquelles le repli local surcharge ces trois
# chemins. Passer par l'environnement plutôt que par une réécriture du source
# du script évite d'avoir à ré-échapper des chemins Windows dans du code
# Python généré.
_ENV_CANDIDATE_DIR = "CRUSH_SANDBOX_CANDIDATE"
_ENV_SRC_DIR = "CRUSH_SANDBOX_SRC"
_ENV_DEPS_DIRS = "CRUSH_SANDBOX_DEPS"
# Jeton a usage unique signant le verdict, et chemin ou le deposer. Le banc
# les retire de l'environnement avant d'importer la candidate : sans cela,
# le code juge pourrait signer son propre verdict.
_ENV_NONCE = "CRUSH_SANDBOX_NONCE"
_ENV_VERDICT = "CRUSH_SANDBOX_VERDICT"


# ── Résultats ─────────────────────────────────────────────────────────────────


@dataclass
class SandboxTestResult:
    """Verdict du test sandbox d'une skill candidate."""

    passed: bool
    # "import" | "instantiate" | "system_prompt" | "tools" | "ok"
    # | "sandbox_env" | "timeout" | "parse" | "sandbox_error"
    layer_failed: str
    notes: str
    # Vrai quand l'échec vient de la machine (dépendances introuvables) et non
    # du code de la candidate. Un appelant ne doit pas en conclure que la skill
    # est mauvaise : rien n'a pu être vérifié.
    environment_error: bool = False


# Couches d'échec qui incriminent l'environnement d'exécution, pas la candidate.
_ENVIRONMENT_LAYERS = frozenset({"sandbox_env", "sandbox_error"})


@dataclass
class LabScanResult:
    """Trace d'un scan polling (run du Lab sur le Kernel)."""

    events_examined: int
    candidates_generated: int
    sandbox_passed: int
    sandbox_failed: int
    skipped_already_handled: int
    errors: list[str]


# ── Chemins portables ─────────────────────────────────────────────────────────


def _dependency_dirs() -> list[Path]:
    """Répertoires de dépendances de l'interpréteur courant.

    Une version antérieure composait `.venv/lib/pythonX.Y/site-packages` à la
    main : c'est la disposition POSIX, alors que Windows installe dans
    `.venv/Lib/site-packages`. Le chemin n'existait donc pas sur la machine de
    développement, la sandbox n'y trouvait aucune dépendance, et TOUTE
    candidate y était rejetée — masquant en permanence les vraies régressions
    du Lab. `sysconfig` répond pour la disposition réellement en place, quelle
    que soit la plateforme et que l'on soit en venv ou sur un Python système.
    """
    paths = sysconfig.get_paths()
    dirs: list[Path] = []
    # purelib et platlib coïncident en venv mais divergent sur le Python
    # système de Debian ; les deux comptent.
    for key in ("purelib", "platlib"):
        raw = paths.get(key)
        if not raw:
            continue
        candidate = Path(raw)
        if candidate not in dirs:
            dirs.append(candidate)
    return dirs


def _source_dir() -> Path:
    """Racine des sources Crush à exposer à la sandbox."""
    return PROJECT_ROOT / "src"


def _missing_dirs(dirs: list[Path]) -> list[Path]:
    return [d for d in dirs if not d.is_dir()]


def _host_mount(path: Path) -> str:
    """Chemin hôte tel que le CLI Docker l'attend à gauche d'un `-v`.

    Seul ce côté du `-v` dépend de la plateforme hôte. La forme à slashs lève
    l'ambiguïté de l'antislash dans un argument déjà découpé par des
    deux-points, et laisse les chemins POSIX inchangés.
    """
    return path.resolve().as_posix()


def _environment_failure(missing: list[Path], mode: str) -> SandboxTestResult:
    """Verdict explicite quand la machine, et non la candidate, est en faute."""
    return SandboxTestResult(
        passed=False,
        layer_failed="sandbox_env",
        notes=(
            f"ENVIRONNEMENT SANDBOX INCOMPLET ({mode}) — la candidate n'est pas "
            "en cause, aucun test n'a pu être joué. Répertoires introuvables : "
            f"{[str(d) for d in missing]}"
        ),
        environment_error=True,
    )


# ── Test générique sandbox ────────────────────────────────────────────────────

# Script Python générique exécuté dans la sandbox. Importé puis joué avec
# `python /workspace/_skill_sandbox_test.py`. Exit 0 = test vert.
_SANDBOX_TEST_SCRIPT = textwrap.dedent(
    '''
    """Test générique d'une skill candidate. Exit 0 si tout passe, ≠ 0 sinon."""

    import importlib.util
    import json
    import os
    import sys
    import traceback
    from pathlib import Path

    # Les valeurs par défaut sont les points de montage du conteneur Docker.
    # Le repli local, lui, tourne sur l'hôte et les surcharge : un chemin de
    # dépendances n'a pas la même forme sous Debian et sous Windows.
    SKILL_DIR = Path(os.environ.get("CRUSH_SANDBOX_CANDIDATE", "/workspace/candidate"))
    SRC_DIR = os.environ.get("CRUSH_SANDBOX_SRC", "/crush_src")
    DEPS_DIRS = [
        p
        for p in os.environ.get("CRUSH_SANDBOX_DEPS", "/crush_deps").split(os.pathsep)
        if p
    ]
    SKILL_PY = SKILL_DIR / "skill.py"
    SKILL_YAML = SKILL_DIR / "skill.yaml"


    # Jeton a usage unique, retire de l'environnement AVANT que la candidate ne
    # soit importee. Le verdict ne vaut que s'il le porte : une candidate qui
    # ecrirait un faux verdict ne peut plus le signer. Sans cela, un
    # `sys.stdout.write('{"ok": true}')` suivi d'un `os._exit(0)` suffisait a
    # obtenir un vert sans qu'aucune verification ne tourne.
    _JETON = os.environ.pop("CRUSH_SANDBOX_NONCE", "")
    _VERDICT = os.environ.pop("CRUSH_SANDBOX_VERDICT", "")


    def _rendre(layer: str, ok: bool, message: str) -> None:
        """Ecrit le verdict la ou l'orchestrateur l'attend, signe du jeton.

        Sur un fichier plutot que sur stdout : la sortie standard est partagee
        avec le code juge, qui peut y ecrire ce qu'il veut.
        """
        charge = json.dumps({"layer": layer, "ok": ok, "notes": message, "nonce": _JETON})
        if _VERDICT:
            try:
                Path(_VERDICT).write_text(charge, encoding="utf-8")
            except OSError:
                pass
        # Aussi sur stdout : sous Docker, le fichier meurt avec le conteneur.
        sys.stdout.write(charge)


    def _fail(layer: str, message: str) -> None:
        _rendre(layer, False, message)
        sys.exit(1)


    def _ok(layer: str, message: str = "") -> None:
        _rendre(layer, True, message)


    # 1) Rendre `skills.base` résolvable AVANT d'importer skill.py — la
    # candidate fait `from skills.base import SkillBase` au top, donc le
    # sys.path doit déjà contenir la racine du repo. (Ce sys.path.insert
    # arrivait après exec_module dans une version antérieure ; tous les
    # skills réels étaient alors rejetés à la couche import.)
    for _dep in reversed(DEPS_DIRS):
        sys.path.insert(0, _dep)
    sys.path.insert(0, SRC_DIR)
    try:
        from crush.capabilities.skills.base import SkillBase  # lazy: sandbox path
    except Exception as exc:
        # Couche distincte de "import" : un environnement de sandbox incomplet
        # et une candidate fautive appellent des actions opposées — réparer la
        # machine, ou corriger la skill. Les confondre fait rejeter en boucle
        # des skills saines et masque les vraies régressions.
        absents = [p for p in [SRC_DIR, *DEPS_DIRS] if not Path(p).is_dir()]
        _fail(
            "sandbox_env",
            "ENVIRONNEMENT SANDBOX INCOMPLET — la candidate n'est pas en cause. "
            f"SkillBase inimportable via {sys.executable!r} : {exc!r}. "
            f"src={SRC_DIR!r} deps={DEPS_DIRS!r} ; chemins inexistants : {absents!r}",
        )


    # 2) Import du skill.py de la candidate.
    if not SKILL_PY.exists():
        _fail("import", f"skill.py introuvable dans {SKILL_DIR}")

    try:
        spec = importlib.util.spec_from_file_location("candidate_skill", SKILL_PY)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:
        _fail("import", f"import a échoué : {exc!r}\\n{traceback.format_exc()[:600]}")

    skill_class = None
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if (
            isinstance(attr, type)
            and issubclass(attr, SkillBase)
            and attr is not SkillBase
            and attr.__module__ == module.__name__
        ):
            skill_class = attr
            break

    if skill_class is None:
        _fail("import", "aucune classe SkillBase trouvée dans skill.py")


    # 3) Charger les métadonnées si présentes
    metadata = {}
    if SKILL_YAML.exists():
        try:
            import yaml
            with SKILL_YAML.open() as f:
                metadata = yaml.safe_load(f) or {}
        except Exception as exc:
            _fail("import", f"skill.yaml illisible : {exc!r}")


    # 4) Instancier la classe
    try:
        skill = skill_class(metadata=metadata)
    except Exception as exc:
        _fail("instantiate", f"instantiation a échoué : {exc!r}")


    # 5) get_system_prompt() retourne une chaîne non-vide
    try:
        prompt = skill.get_system_prompt()
    except Exception as exc:
        _fail("system_prompt", f"get_system_prompt() a levé : {exc!r}")

    if not isinstance(prompt, str) or not prompt.strip():
        _fail("system_prompt", f"get_system_prompt() doit retourner str non-vide, "
              f"reçu {type(prompt).__name__} de longueur {len(prompt or '')}")


    # 6) Si get_tools() retourne quelque chose, chaque tool doit être valide
    try:
        tools = skill.get_tools()
    except Exception as exc:
        _fail("tools", f"get_tools() a levé : {exc!r}")

    if tools:
        for i, tool in enumerate(tools):
            for attr in ("name", "description", "input_schema"):
                if not hasattr(tool, attr):
                    _fail("tools", f"tool[{i}] manque l'attribut '{attr}'")
            if not isinstance(tool.name, str) or not tool.name.strip():
                _fail("tools", f"tool[{i}].name doit être str non-vide")
            if not isinstance(tool.description, str) or not tool.description.strip():
                _fail("tools", f"tool[{i}].description doit être str non-vide")
            if not isinstance(tool.input_schema, dict):
                _fail("tools", f"tool[{i}].input_schema doit être dict")


    _ok("ok", f"skill '{skill.name}' validée (prompt={len(prompt)} chars, "
        f"tools={len(tools)})")
    '''
).strip()


# ── Lab ───────────────────────────────────────────────────────────────────────


def _refus_execution_hote(raison: str) -> SandboxTestResult:
    """Verdict rendu quand on refuse de lancer la candidate hors conteneur.

    Le skill.py d'une candidate est ecrit par un LLM. L'executer sur l'hote lui
    donne le .env, le reseau, et l'ecriture dans skills_data/installed/ — d'ou
    le code repart en exec_module dans le processus Crush au rechargement du
    registre. La validation humaine, seule vraie barriere du dispositif, devient
    contournable par le code meme qu'elle est censee arbitrer.

    On refuse donc plutot que d'executer, et le statut reste un echec : rien n'a
    ete verifie, donc rien ne doit pouvoir etre promu.
    """
    return SandboxTestResult(
        passed=False,
        layer_failed="sandbox_env",
        notes=(
            f"Test refuse : {raison}. La candidate n'est PAS en cause, rien n'a ete "
            "verifie. Le banc d'essai exige un conteneur : installez Docker et mettez "
            "DOCKER_ENABLED=true dans .env. A defaut, SKILL_SANDBOX_ALLOW_HOST_EXEC=true "
            "autorise l'execution directe sur la machine — cela revient a accorder au "
            "code genere les droits du service, y compris la lecture du .env."
        ),
        environment_error=True,
    )


class SkillLab:
    """Pilote du cycle Génération → Sandbox → Validation humaine → Installation.

    Le Lab ne stocke pas d'état lui-même : il lit/écrit le SkillLifecycle (SQL)
    et manipule les dossiers sur disque (candidates/ ↔ installed/).
    """

    def __init__(
        self,
        kernel: MemoryKernel,
        lifecycle: SkillLifecycle,
        synthesizer: SkillSynthesizer,
        *,
        candidates_dir: Path = SKILLS_CANDIDATES_DIR,
        installed_dir: Path = SKILLS_INSTALLED_DIR,
        registry_reload: callable | None = None,
    ) -> None:
        self._kernel = kernel
        self._lifecycle = lifecycle
        self._synthesizer = synthesizer
        self._candidates_dir = Path(candidates_dir)
        self._installed_dir = Path(installed_dir)
        # Callable optionnel pour recharger le SkillRegistry après promotion.
        # Injecté par main.py via skill_registry.reload.
        self._registry_reload = registry_reload

    # ── Polling Kernel ────────────────────────────────────────────────────────

    async def scan_kernel(self) -> LabScanResult:
        """Scanne les events `skill_candidate_proposal` non encore traités et
        déclenche la pipeline pour chacun.

        Idempotent : `lifecycle.has_been_proposed_for_event(event_id)` évite de
        re-générer pour un event déjà vu. Cap dur sur le nombre d'events
        examinés (`_MAX_EVENTS_PER_SCAN`) et de candidates générées
        (`_MAX_CANDIDATES_PER_SCAN`) pour borner les appels LLM.
        """
        result = LabScanResult(
            events_examined=0,
            candidates_generated=0,
            sandbox_passed=0,
            sandbox_failed=0,
            skipped_already_handled=0,
            errors=[],
        )

        events = self._fetch_skill_candidate_events(limit=_MAX_EVENTS_PER_SCAN)
        result.events_examined = len(events)

        for event in events:
            if result.candidates_generated >= _MAX_CANDIDATES_PER_SCAN:
                logger.info(
                    "SkillLab: cap appels LLM atteint",
                    cap=_MAX_CANDIDATES_PER_SCAN,
                )
                break
            event_id = event["id"]
            if self._lifecycle.has_been_proposed_for_event(event_id):
                result.skipped_already_handled += 1
                continue
            try:
                outcome = await self.propose_from_event(event_id, event)
                result.candidates_generated += 1
                if outcome and outcome.status == SkillStatus.SANDBOXED_PASS:
                    result.sandbox_passed += 1
                elif outcome and outcome.status == SkillStatus.SANDBOXED_FAIL:
                    result.sandbox_failed += 1
            except Exception as exc:  # noqa: BLE001 — best-effort par event
                logger.warning(
                    "SkillLab: scan échec sur event",
                    event_id=event_id,
                    error=str(exc),
                )
                result.errors.append(f"{event_id}: {exc}")

        logger.info(
            "SkillLab scan terminé",
            examined=result.events_examined,
            generated=result.candidates_generated,
            passed=result.sandbox_passed,
            failed=result.sandbox_failed,
            skipped=result.skipped_already_handled,
            errors=len(result.errors),
        )
        return result

    def _fetch_skill_candidate_events(self, limit: int) -> list[dict]:
        """Récupère les N events `skill_candidate_proposal` les plus récents.

        Retourne des dicts {id, content, metadata_json, created_at}.
        """
        import sqlite3

        with sqlite3.connect(self._kernel.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, content, metadata_json, created_at "
                "FROM events WHERE type = ? "
                "ORDER BY created_at DESC LIMIT ?",
                ("skill_candidate_proposal", limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Pipeline depuis un event ──────────────────────────────────────────────

    async def propose_from_event(
        self, event_id: str, event_payload: dict | None = None
    ) -> SkillRecord | None:
        """Pipeline complète depuis un event Kernel `skill_candidate_proposal`.

        Wrapper qui fetch l'event, le convertit en trajectoire, puis délègue à
        `propose_from_trajectory(trajectory, source_event_id=event_id)`.
        """
        if event_payload is None:
            evt = self._kernel.get_event(event_id)
            if evt is None:
                logger.warning("SkillLab: event introuvable", event_id=event_id)
                return None
            event_payload = {
                "id": evt.id,
                "content": evt.content,
                "metadata_json": evt.metadata_json,
            }
        trajectory = self._event_to_trajectory(event_payload)
        return await self.propose_from_trajectory(trajectory, source_event_id=event_id)

    async def propose_from_trajectory(
        self,
        trajectory: dict,
        source_event_id: str | None = None,
    ) -> SkillRecord | None:
        """API publique : pipeline depuis une trajectoire arbitraire (tool, signal).

        C'est le SEUL point d'entrée pour créer une skill candidate. Aucun
        chemin alternatif ne doit court-circuiter ce gate (cf. CDC §7.3 anti-
        pattern : "Ne pas installer une skill sans test vert en sandbox").

        Étapes :
        1. Génère la skill candidate via le synthesizer (écrit dans
           candidates_dir/{name}/ — ZONE TAMPON, pas installed/).
        2. Enregistre la candidate dans le lifecycle SQL (source_event_id pour
           idempotence du polling).
        3. Test sandbox → SANDBOXED_PASS ou SANDBOXED_FAIL.
        4. Renvoie le SkillRecord final, ou None si la génération a échoué.

        La promotion vers installed/ exige une action humaine explicite via
        SkillLab.promote() (typiquement endpoint POST /api/skills/lab/{name}/promote).
        """
        # 1) Génère la candidate dans la zone tampon (jamais dans installed/).
        try:
            skill_name = await self._synthesizer.propose_skill_candidate(
                trajectory, target_dir=self._candidates_dir
            )
        except Exception as exc:  # noqa: BLE001 — synthèse foireuse, on log
            logger.warning(
                "SkillLab: génération candidate échouée",
                source_event_id=source_event_id,
                error=str(exc),
            )
            return None

        # 2) Enregistre dans le lifecycle (status=CANDIDATE par défaut).
        self._lifecycle.create_candidate(name=skill_name, source_event_id=source_event_id)

        # 3) Test sandbox — le gate critique.
        return await self.test_in_sandbox(skill_name)

    @staticmethod
    def _event_to_trajectory(event_payload: dict) -> dict:
        """Convertit un event skill_candidate_proposal en trajectoire pour le synthesizer."""
        meta: dict = {}
        if event_payload.get("metadata_json"):
            try:
                meta = json.loads(event_payload["metadata_json"]) or {}
            except (TypeError, json.JSONDecodeError):
                pass
        return {
            "task_description": event_payload.get("content", "")[:600],
            "result": meta.get("from_lesson_evt", ""),
            "messages": [],
            "tool_calls": [],
        }

    # ── Test sandbox (gate test-vert-sinon-rejet) ────────────────────────────

    async def test_in_sandbox(self, skill_name: str) -> SkillRecord | None:
        """Lance le test générique en sandbox Docker. Met à jour le lifecycle.

        Si Docker indisponible → fallback exécution directe avec ATTENTION
        loguée. C'est le compromis du MVP : sans Docker, on garde un test
        déterministe utile (l'isolation est moins forte).
        """
        cand_dir = self._candidates_dir / skill_name
        if not (cand_dir / "skill.py").exists():
            logger.warning("SkillLab: candidate introuvable", name=skill_name)
            return None

        try:
            result = await self._run_sandbox_test(cand_dir)
        except Exception as exc:  # noqa: BLE001
            result = SandboxTestResult(
                passed=False,
                layer_failed="sandbox_error",
                notes=f"Erreur infrastructure sandbox : {exc!r}",
                environment_error=True,
            )

        record = self._lifecycle.mark_sandbox_result(
            name=skill_name,
            passed=result.passed,
            notes=f"[{result.layer_failed}] {result.notes}",
        )
        if result.environment_error:
            # Le statut reste un échec — on n'installe jamais une skill non
            # vérifiée — mais l'exploitant doit réparer sa machine, pas jeter
            # la candidate. D'où le niveau error et le message séparé.
            logger.error(
                "SkillLab sandbox INEXPLOITABLE — dépendances introuvables",
                name=skill_name,
                layer=result.layer_failed,
                notes=result.notes,
            )
        else:
            logger.info(
                "SkillLab sandbox",
                name=skill_name,
                passed=result.passed,
                layer=result.layer_failed,
            )
        return record

    async def _run_sandbox_test(self, cand_dir: Path) -> SandboxTestResult:
        """Crée un container Docker temporaire, monte candidates/{name}/
        en read-only, exécute le test générique, parse la sortie JSON."""

        if not settings.docker_enabled:
            if not settings.skill_sandbox_allow_host_exec:
                return _refus_execution_hote("Docker est desactive (DOCKER_ENABLED=false)")
            return await self._run_direct_test(cand_dir)

        # Vérifie que Docker est joignable
        if not await DockerExecutor.is_available():
            logger.warning(
                "SkillLab: Docker indisponible, fallback test direct",
                cand_dir=str(cand_dir),
            )
            if not settings.skill_sandbox_allow_host_exec:
                return _refus_execution_hote("Docker est active mais injoignable")
            return await self._run_direct_test(cand_dir)

        # Container ad-hoc : workspace tmpfs + candidate montée RO + source crush montée RO
        container_name = f"crush-skill-lab-{uuid.uuid4().hex[:8]}"
        # Signature du verdict. Neuve a chaque test : un jeton rejoue
        # laisserait une candidate resservir le vert d'une autre.
        jeton = uuid.uuid4().hex
        cand_abs = cand_dir.resolve()
        crush_root = _source_dir()
        # Côté HÔTE uniquement : ces chemins sont calculés sur la machine qui
        # lance docker. Ce qu'ils deviennent dans le conteneur, ce sont les
        # constantes _CONTAINER_* — POSIX, car le conteneur est Linux quelle
        # que soit la plateforme hôte.
        host_deps = [d for d in _dependency_dirs() if d.is_dir()]
        if not host_deps:
            return _environment_failure(_dependency_dirs(), mode="docker")

        if os.name == "nt":
            # Les roues installées sous Windows embarquent des binaires .pyd :
            # montées telles quelles dans un conteneur Linux, les paquets
            # compilés ne se chargeront pas. Le test reste jouable pour les
            # dépendances pur-Python, mais l'échec éventuel est imputable à
            # l'hôte, pas à la candidate.
            logger.warning(
                "SkillLab: dépendances Windows montées dans un conteneur Linux",
                deps=[str(d) for d in host_deps],
            )

        # Crée le script de test dans un tmpdir local et le mount aussi
        script_path = cand_abs / "_skill_sandbox_test.py"
        script_path.write_text(_SANDBOX_TEST_SCRIPT, encoding="utf-8")

        try:
            # Un montage par répertoire de dépendances : sur un Python système
            # Debian, purelib et platlib sont deux dossiers distincts.
            deps_mounts: list[str] = []
            deps_in_container: list[str] = []
            for index, host_dir in enumerate(host_deps):
                target = f"{_CONTAINER_DEPS_DIR}/{index}"
                deps_mounts += ["-v", f"{_host_mount(host_dir)}:{target}:ro"]
                deps_in_container.append(target)

            # Sur une installation packagée il n'y a pas de `src/` : monter un
            # chemin hôte inexistant ferait créer un dossier vide par Docker,
            # et crush se résout de toute façon depuis les dépendances.
            src_mount: list[str] = []
            if crush_root.is_dir():
                src_mount = [
                    "-v",
                    f"{_host_mount(crush_root)}:{_CONTAINER_SRC_DIR}:ro",
                ]

            cmd = [
                "docker",
                "run",
                "--rm",
                "--name",
                container_name,
                f"--memory={settings.docker_memory_limit}",
                f"--cpus={settings.docker_cpu_limit}",
                "--network",
                "none",  # pas de réseau pour le test sandbox
                "--read-only",
                "--tmpfs",
                "/tmp:rw,size=50m",
                "--security-opt",
                "no-new-privileges",
                "--cap-drop",
                "ALL",
                "-v",
                f"{_host_mount(cand_abs)}:{_CONTAINER_CANDIDATE_DIR}:ro",
                *src_mount,
                *deps_mounts,
                "-e",
                f"{_ENV_CANDIDATE_DIR}={_CONTAINER_CANDIDATE_DIR}",
                "-e",
                f"{_ENV_SRC_DIR}={_CONTAINER_SRC_DIR}",
                # Séparateur ":" en dur : c'est celui du conteneur Linux, pas
                # celui de l'hôte (";" sous Windows).
                "-e",
                f"{_ENV_DEPS_DIRS}={':'.join(deps_in_container)}",
                "-e",
                f"{_ENV_NONCE}={jeton}",
                # Le conteneur ne monte que src/ : la remontee vers
                # pyproject.toml n'y aboutit pas, et l'import de n'importe quel
                # module crush echouait avant meme la premiere verification.
                # /tmp est le tmpfs du conteneur, inscriptible et jetable.
                "-e",
                "CRUSH_PROJECT_ROOT=/tmp",
                "-w",
                "/workspace",
                settings.docker_base_image,
                "python",
                f"{_CONTAINER_CANDIDATE_DIR}/_skill_sandbox_test.py",
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=_SANDBOX_TIMEOUT
                )
            except TimeoutError:
                # Tue le container
                killer = await asyncio.create_subprocess_exec(
                    "docker",
                    "kill",
                    container_name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.communicate()
                return SandboxTestResult(
                    passed=False,
                    layer_failed="timeout",
                    notes=f"timeout après {_SANDBOX_TIMEOUT}s",
                )
        finally:
            script_path.unlink(missing_ok=True)

        return self._parse_sandbox_output(proc.returncode, stdout, stderr, nonce=jeton)

    async def _run_direct_test(self, cand_dir: Path) -> SandboxTestResult:
        """Fallback : exécute le test dans un subprocess Python local (pas Docker).

        Moins isolé qu'une vraie sandbox mais préserve le gate test-vert
        comme garde-fou MVP quand Docker n'est pas disponible (typique en CI
        ou en dev local sans Docker daemon).
        """
        cand_abs = cand_dir.resolve()
        jeton = uuid.uuid4().hex
        # Le verdict se depose a cote du dossier candidate, jamais dedans :
        # ce dossier appartient au code juge, qui pourrait le reecrire.
        verdict_path = cand_abs.parent / f".verdict-{jeton}.json"
        crush_root = _source_dir()
        deps_dirs = [d for d in _dependency_dirs() if d.is_dir()]

        # Seules les dépendances sont exigées d'avance. Une racine `src/`
        # absente n'est pas rédhibitoire — sur une installation packagée,
        # crush vit dans les site-packages. Si SkillBase reste introuvable,
        # c'est le script qui le dira, avec le détail des chemins essayés.
        if not deps_dirs:
            return _environment_failure(_missing_dirs(_dependency_dirs()), mode="direct")

        script_path = cand_abs / "_skill_sandbox_test.py"
        # Le script est écrit tel quel : les chemins passent par
        # l'environnement. Les injecter par réécriture du source obligeait à
        # ré-échapper des antislashs Windows dans du code Python généré.
        script_path.write_text(_SANDBOX_TEST_SCRIPT, encoding="utf-8")
        env = {
            **os.environ,
            _ENV_CANDIDATE_DIR: str(cand_abs),
            _ENV_SRC_DIR: str(crush_root),
            # Ici hôte et sous-processus tournent sur la même plateforme :
            # os.pathsep est le bon séparateur des deux côtés.
            _ENV_DEPS_DIRS: os.pathsep.join(str(d) for d in deps_dirs),
            _ENV_NONCE: jeton,
            # Hors du dossier de la candidate : sous Docker celui-ci est monte
            # en lecture seule, et en local il appartient au code juge.
            _ENV_VERDICT: str(verdict_path),
        }

        try:
            proc = await asyncio.create_subprocess_exec(
                # sys.executable, jamais "python" : sous `uv run` le "python"
                # du PATH est l'interpréteur de base, sans les dépendances du
                # projet, et sur Debian il peut n'exister que "python3".
                sys.executable,
                str(script_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=_SANDBOX_TIMEOUT
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return SandboxTestResult(
                    passed=False,
                    layer_failed="timeout",
                    notes=f"timeout après {_SANDBOX_TIMEOUT}s (direct)",
                )
        finally:
            script_path.unlink(missing_ok=True)
            verdict_path.unlink(missing_ok=True)

        return self._parse_sandbox_output(
            proc.returncode, stdout, stderr, verdict_path=verdict_path, nonce=jeton
        )

    @staticmethod
    def _parse_sandbox_output(
        returncode: int | None,
        stdout: bytes,
        stderr: bytes,
        verdict_path: Path | None = None,
        nonce: str = "",
    ) -> SandboxTestResult:
        """Lit le verdict du banc d'essai, et refuse tout ce qu'il n'a pas signé.

        Le verdict transitait par le stdout du processus qui importe la
        candidate — donc par un canal que le code jugé partage. Il suffisait
        d'écrire le JSON attendu puis d'appeler `os._exit(0)` pour obtenir un
        vert sans qu'aucune des six vérifications ne tourne, et la promotion
        s'ouvrait. Le verdict arrive désormais par un fichier que
        l'orchestrateur nomme, et porte un jeton retiré de l'environnement
        avant l'import de la candidate.

        Ce n'est pas une frontière étanche : la candidate est importée dans le
        processus du banc, et Python laisse introspecter les globales de
        `__main__`. C'est une élévation du coût de l'attaque, pas une
        garantie — la garantie, c'est le conteneur.
        """
        err = stderr.decode("utf-8", errors="replace").strip()
        out = stdout.decode("utf-8", errors="replace").strip()

        brut = ""
        if verdict_path is not None:
            try:
                brut = verdict_path.read_text(encoding="utf-8").strip()
            except OSError:
                brut = ""
        if not brut:
            # Repli sur stdout : sous Docker, un fichier écrit dans le
            # conteneur ne survit pas à sa destruction. Le canal importe peu
            # une fois le verdict signé — c'est la signature qui protège, pas
            # le tuyau.
            brut = out

        if not brut:
            # Aucun verdict rendu : le banc n'est pas allé au bout. Un code de
            # sortie non nul sans verdict désigne l'infrastructure (image
            # Docker absente, interpréteur introuvable), pas la candidate.
            return SandboxTestResult(
                passed=False,
                layer_failed="sandbox_error",
                notes=(
                    f"Le banc d'essai n'a rendu aucun verdict (rc={returncode}). "
                    f"La candidate n'est PAS en cause : rien n'a pu être vérifié. "
                    f"stdout={out[:160]!r} stderr={err[:240]!r}"
                ),
                environment_error=True,
            )

        try:
            payload = json.loads(brut)
        except json.JSONDecodeError:
            return SandboxTestResult(
                passed=False,
                layer_failed="sandbox_error",
                notes=f"verdict illisible (rc={returncode}) : {brut[:200]!r}",
                environment_error=True,
            )
        if not isinstance(payload, dict):
            return SandboxTestResult(
                passed=False,
                layer_failed="sandbox_error",
                notes=f"verdict non-dict : {payload!r}",
                environment_error=True,
            )

        if nonce and str(payload.get("nonce", "")) != nonce:
            # Signature absente ou fausse : le verdict ne vient pas du banc.
            # On ne peut rien en conclure sur la candidate, mais on ne peut
            # surtout pas la déclarer verte.
            return SandboxTestResult(
                passed=False,
                layer_failed="sandbox_error",
                notes=(
                    "Verdict non signé par le banc d'essai — rejeté. Le fichier de "
                    "verdict a été écrit par autre chose que le harnais de test. "
                    "Aucune installation possible."
                ),
                environment_error=True,
            )

        ok = bool(payload.get("ok"))
        layer = str(payload.get("layer", "?"))
        notes = str(payload.get("notes", ""))[:600]
        if not ok and not notes:
            notes = err[:400] or out[:400]
        return SandboxTestResult(
            passed=ok,
            layer_failed=layer,
            notes=notes,
            environment_error=not ok and layer in _ENVIRONMENT_LAYERS,
        )

    # ── Promotion / Rejet ────────────────────────────────────────────────────

    def promote(self, skill_name: str) -> SkillRecord | None:
        """Validation humaine accordée : déplace candidate → installed,
        marque ACTIVE dans le lifecycle, recharge le SkillRegistry.

        Refuse si la skill n'est pas en SANDBOXED_PASS — on n'installe JAMAIS
        une skill qui n'a pas passé son test sandbox (CDC §7 anti-pattern :
        "Ne pas installer une skill sans test vert en sandbox").
        """
        record = self._lifecycle.get(skill_name)
        if record is None:
            logger.warning("SkillLab.promote: skill inconnue", name=skill_name)
            return None
        if record.status != SkillStatus.SANDBOXED_PASS:
            logger.warning(
                "SkillLab.promote: refusé — status non SANDBOXED_PASS",
                name=skill_name,
                status=record.status.value,
            )
            return None

        cand_dir = self._candidates_dir / skill_name
        installed_dir = self._installed_dir / skill_name
        if not cand_dir.exists():
            logger.error("SkillLab.promote: candidate disparue du disque", name=skill_name)
            return None
        if installed_dir.exists():
            # Collision : on refuse plutôt que d'écraser une skill installée
            logger.error(
                "SkillLab.promote: collision avec skill installée",
                name=skill_name,
            )
            return None

        installed_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(cand_dir), str(installed_dir))
        # Nettoie le fichier test résiduel s'il existe
        (installed_dir / "_skill_sandbox_test.py").unlink(missing_ok=True)

        promoted = self._lifecycle.promote(skill_name)

        if self._registry_reload is not None:
            try:
                self._registry_reload()
            except Exception as exc:  # noqa: BLE001
                logger.warning("SkillRegistry.reload() échec", error=str(exc))

        logger.info("Skill promue et installée", name=skill_name)
        return promoted

    def reject(
        self,
        skill_name: str,
        reason: str = "",
        delete_files: bool = False,
    ) -> SkillRecord | None:
        """Validation humaine refusée : marque REJECTED dans le lifecycle.

        delete_files=False par défaut → la candidate reste sur disque (audit).
        delete_files=True → supprime physiquement le dossier candidates/{name}/.
        """
        record = self._lifecycle.reject(skill_name, reason=reason)
        if delete_files:
            cand_dir = self._candidates_dir / skill_name
            if cand_dir.exists():
                shutil.rmtree(cand_dir)
                logger.info("SkillLab: candidate supprimée du disque", name=skill_name)
        return record


__all__ = [
    "LabScanResult",
    "SandboxTestResult",
    "SkillLab",
]
