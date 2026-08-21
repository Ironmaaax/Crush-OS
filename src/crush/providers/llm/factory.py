# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .


from __future__ import annotations

from crush.kernel.contracts import UsageTracker
from crush.kernel.settings import settings
from crush.providers.llm.api import get_api_provider
from crush.providers.llm.base import LLMProvider
from crush.providers.llm.local import OllamaProvider


def get_llm_provider(tracker: UsageTracker | None = None) -> LLMProvider:
    """Instancie le provider LLM selon LLM_PROVIDER dans .env."""
    if settings.llm_provider == "local":
        return OllamaProvider()
    return get_api_provider(settings.api_backend, tracker=tracker)


def create_background_llm(tracker: UsageTracker | None = None) -> LLMProvider:
    """Provider léger et indépendant pour les tâches background (consolidation, auto_dream).

    Instance séparée = client HTTP distinct = aucune contention avec le provider principal.
    max_tokens=500 suffit largement pour les réponses de mémorisation.
    """
    if settings.llm_provider == "local":
        return OllamaProvider()
    if settings.api_backend == "anthropic":
        return get_api_provider("anthropic", max_tokens=500, tracker=tracker)
    return get_api_provider(settings.api_backend, tracker=tracker)


def create_voice_llm(tracker: UsageTracker | None = None) -> LLMProvider:
    """Provider du pipeline vocal in-house (voice_gateway, mission worker).

    Suit API_BACKEND comme le provider principal — aucune dépendance Anthropic
    forcée. Le modèle voix dédié (VOICE_ANTHROPIC_MODEL) n'est utilisé que
    lorsque le backend actif est Anthropic ; les autres backends utilisent leur
    modèle standard configuré.
    """
    if settings.llm_provider == "local":
        return OllamaProvider()
    if settings.api_backend == "anthropic":
        return get_api_provider(
            "anthropic",
            max_tokens=4096,
            model=settings.voice_anthropic_model,
            tracker=tracker,
        )
    # low_latency : coupe le raisonnement prealable des modeles Gemini 2.5+,
    # qui s'ecoule avant le premier token et se paie en attente a l'oral.
    return get_api_provider(
        settings.api_backend, max_tokens=4096, tracker=tracker, low_latency=True
    )


def create_reflective_llm(tracker: UsageTracker | None = None) -> LLMProvider | None:
    """Jumeau raisonnant du provider vocal, pour les questions complexes.

    Même backend, même modèle que `create_voice_llm` — seul le budget de
    raisonnement change. Deux instances plutôt qu'un réglage modifié à la volée :
    le budget est figé dans la configuration de requête, et le muter entre deux
    appels concurrents ferait répondre l'un avec le réglage de l'autre.

    Retourne None quand la réflexion sélective n'a pas de sens : drapeau baissé,
    modèle local (pas de budget de raisonnement à piloter), ou backend dont le
    provider ignore le paramètre. L'appelant retombe alors sur la voie rapide.
    """
    if not settings.reflection_enabled or settings.llm_provider == "local":
        return None
    if settings.api_backend != "gemini":
        return None
    return get_api_provider(
        settings.api_backend,
        max_tokens=4096,
        tracker=tracker,
        thinking_budget=settings.reflection_thinking_budget,
    )
