# Copyright (C) 2026 Max Ea
# This file is part of CRUSH-OS,   .
 

"""Conformité statique + runtime des implémentations aux Protocols kernel.

CDC §F.1.3bis — sans type-checker, `kernel/contracts.py` n'est que de la
documentation : les Protocols structurels NE sont JAMAIS vérifiés au
runtime par CPython. mypy compare les signatures au point d'assignation
TYPÉE — c'est ici qu'on l'oblige à faire le travail.

Mécanisme :
  - une assignation explicite `_x: Protocol = ConcreteImpl(...)` par
    couple (Protocol, implémentation) — mypy compare alors les signatures
    et plante si un paramètre, un type de retour, un nom ou une arity
    diffère.
  - mypy est scopé sur ce fichier + `src/crush/kernel/` dans
    `pyproject.toml::tool.mypy.files` — le reste du codebase n'est PAS
    typé strictement (chantier séparé, hors-périmètre).

Le test pytest associé NE INSTANCIE PAS les classes : il fait l'assignation
via `typing.cast` après un check `if False`, ce qui est ANALYSÉ par mypy
mais éliminé au runtime. C'est volontaire — la plupart des providers LLM
ont besoin d'une clé API à l'instantiation, et on ne veut pas que la
suite de tests dépende d'une .env complète. La validation runtime
(`isinstance` au boot bootstrap.build()) est faite côté `bootstrap.py`
GATE F1bis-b, sur les instances RÉELLES.

Périmètre couvert en Phase F (12 couples statiques + 1 runtime) :
  - L1 / Providers / LLM : Anthropic, Mistral, Gemini, OpenAI, Ollama (5)
  - L1 / Providers / Memory : MemoryStore (MemoryKernel), FTSIndex,
    VectorIndex, SessionStore, TopicStore, MemoryIndex (6)
  - L2 / Engine / tracking : UsageTracker (mypy statique + runtime check, 1)

Hors-périmètre (BACKLOG Phase G "fermer conformité Protocols stricte") :
ToolRegistry et SkillRegistry — divergences PURE VARIANCE entre Protocol
(`kernel.contracts.Tool` / `Skill`) et ABC (`capabilities.tools.base.Tool`
/ `capabilities.skills.base.SkillBase`). Les attributs/méthodes sont
structurellement identiques, mais mypy distingue les types nominaux.
Documentés en BACKLOG.md, à reprendre en G en alignant `*tools: Tool`
sur le Protocol (l'ABC l'implémente structurellement).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from crush.kernel import contracts

if TYPE_CHECKING:
    # Imports pour le check statique. Les types sont résolus par mypy
    # mais le code n'est jamais exécuté → pas besoin de clé API pour le LLM.
    from crush.engine.tracking import UsageTracker as _ConcreteUsageTracker
    from crush.providers.llm.api import (
        AnthropicProvider,
        GeminiProvider,
        MistralProvider,
        OpenAIProvider,
    )
    from crush.providers.llm.local import OllamaProvider
    from crush.providers.memory.index import MemoryIndex as _ConcreteMemoryIndex
    from crush.providers.memory.kernel import MemoryKernel as _ConcreteMemoryStore
    from crush.providers.memory.search import FTSIndex as _ConcreteFTSIndex
    from crush.providers.memory.search import VectorIndex as _ConcreteVectorIndex
    from crush.providers.memory.sessions import SessionStore as _ConcreteSessionStore
    from crush.providers.memory.topics import TopicStore as _ConcreteTopicStore


def test_conformance_static() -> None:
    """Assignation typée par couple (Protocol, implémentation).

    Au runtime, le code dans `if False:` est éliminé — pas d'instantiation.
    mypy le visite normalement et vérifie chaque assignation.
    """
    if False:  # noqa: SIM108 — bloc analysé par mypy, jamais exécuté
        # ── L1 — Providers / LLM (5 couples) ────────────────────────────────
        _a: contracts.LLMProvider = cast("AnthropicProvider", None)
        _m: contracts.LLMProvider = cast("MistralProvider", None)
        _g: contracts.LLMProvider = cast("GeminiProvider", None)
        _open: contracts.LLMProvider = cast("OpenAIProvider", None)
        _o: contracts.LLMProvider = cast("OllamaProvider", None)

        # ── L1 — Providers / Memory (6 couples) ─────────────────────────────
        _store: contracts.MemoryStore = cast("_ConcreteMemoryStore", None)
        _sess: contracts.SessionStore = cast("_ConcreteSessionStore", None)
        _top: contracts.TopicStore = cast("_ConcreteTopicStore", None)
        _mi: contracts.MemoryIndex = cast("_ConcreteMemoryIndex", None)
        _fts: contracts.FTSIndex = cast("_ConcreteFTSIndex", None)
        _vi: contracts.VectorIndex = cast("_ConcreteVectorIndex", None)

        # ── L2 — Engine / tracking (1 couple) ───────────────────────────────
        _ut: contracts.UsageTracker = cast("_ConcreteUsageTracker", None)

    assert True  # mypy a tranché statiquement — 12 couples couverts


def test_conformance_runtime_check() -> None:
    """Ceinture-bretelles runtime : Protocols `@runtime_checkable` valident
    l'EXISTENCE des méthodes (pas les signatures).

    On évite d'instancier les LLM providers (clé API requise). UsageTracker
    n'a pas de dépendance externe — bon proxy pour l'isinstance check.
    """
    from crush.engine.tracking import UsageTracker

    assert isinstance(UsageTracker(), contracts.UsageTracker)
