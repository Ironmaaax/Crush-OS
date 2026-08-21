# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .


from __future__ import annotations

import asyncio
import json as _json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Any, TypeVar

import anthropic
from google.genai import types as _t
from loguru import logger
from openai import AsyncOpenAI

from crush.kernel.contracts import UsageTracker
from crush.kernel.schemas import ToolCapture, UsageEntry, calculate_cost
from crush.kernel.settings import settings
from crush.providers.llm.base import LLMProvider

_MAX_TOOL_ITERATIONS = 20
_MAX_ANTHROPIC_RETRIES = 3
_ANTHROPIC_RETRY_STATUS = {429, 500, 502, 503, 529}

_T = TypeVar("_T")


def _is_retryable_anthropic(exc: BaseException) -> bool:
    """Vrai si l'erreur Anthropic est transitoire et vaut une nouvelle tentative.

    Regroupe le triplet (connexion, rate-limit, statut 5xx/429) qui était
    dupliqué en trois `except` identiques sur les trois sites de retry.
    """
    if isinstance(exc, anthropic.APIConnectionError | anthropic.RateLimitError):
        return True
    return isinstance(exc, anthropic.APIStatusError) and exc.status_code in _ANTHROPIC_RETRY_STATUS

# CYCLE 1 (CDC §C.1.3) — bouclé : aucun import depuis `crush.engine.*`.
# Le tracker est reçu par constructeur (DI), typé via le Protocol
# `crush.kernel.contracts.UsageTracker`. Câblage dans `bootstrap.build()`.


# ── Helpers de conversion de format ──────────────────────────────────────────


def _claude_tools_to_openai(tools: list[dict]) -> list[dict]:
    """Convertit le schéma d'outils Claude (input_schema) vers le format OpenAI function calling."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for t in tools
    ]


def _messages_to_openai(messages: list[dict]) -> list[dict]:
    """Convertit les messages Anthropic (tool_use / tool_result) vers le format OpenAI.

    Nécessaire pour la passe de synthèse où agent.py injecte des blocs Anthropic
    dans l'historique avant un appel complete() sans outils (Mistral).
    """
    result: list[dict] = []
    for msg in messages:
        role: str = msg["role"]
        content: Any = msg.get("content", "")

        if isinstance(content, str):
            result.append({"role": role, "content": content})
            continue

        has_tool_use = any(b.get("type") == "tool_use" for b in content)
        has_tool_result = any(b.get("type") == "tool_result" for b in content)

        if has_tool_use:
            text = " ".join(b["text"] for b in content if b.get("type") == "text" and b.get("text"))
            tool_calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {
                        "name": b["name"],
                        "arguments": _json.dumps(b.get("input", {})),
                    },
                }
                for b in content
                if b.get("type") == "tool_use"
            ]
            result.append(
                {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": tool_calls,
                }
            )
        elif has_tool_result:
            # Chaque tool_result devient un message "tool" séparé (rôle OpenAI)
            for block in content:
                if block.get("type") == "tool_result":
                    result.append(
                        {
                            "role": "tool",
                            "tool_call_id": block["tool_use_id"],
                            "content": block.get("content", ""),
                        }
                    )
        else:
            text = " ".join(b.get("text", "") for b in content if b.get("type") == "text")
            result.append({"role": role, "content": text})

    return result


def _anthropic_extract_text(content: object) -> str:
    parts: list[str] = []
    for block in content:  # type: ignore[union-attr]
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "".join(parts)


async def _anthropic_retry(coro_factory: Callable[[], Awaitable[_T]]) -> _T:
    for attempt in range(_MAX_ANTHROPIC_RETRIES):
        try:
            return await coro_factory()
        except anthropic.APIError as e:
            if not _is_retryable_anthropic(e) or attempt + 1 >= _MAX_ANTHROPIC_RETRIES:
                raise
            await asyncio.sleep(min(2**attempt, 8))
    raise AssertionError("unreachable")  # pragma: no cover


# ── Providers ─────────────────────────────────────────────────────────────────


class AnthropicProvider(LLMProvider):
    """Provider Anthropic Claude via SDK officiel."""

    def __init__(
        self,
        max_tokens: int = 2048,
        model: str | None = None,
        tracker: UsageTracker | None = None,
    ) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value()
        )
        self._model = model or settings.anthropic_model
        self._max_tokens = max_tokens
        self._tracker = tracker

    def set_tracker(self, tracker: UsageTracker) -> None:
        """Injection post-construction (utilisé pour providers créés par factory)."""
        self._tracker = tracker

    @property
    def supports_tools(self) -> bool:
        return True

    async def complete(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        stream: bool = False,
        context: str = "",
    ) -> str | AsyncIterator[str]:
        kwargs: dict = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        if stream:
            return self._stream(kwargs)

        response = await _anthropic_retry(lambda: self._client.messages.create(**kwargs))
        text = _anthropic_extract_text(response.content)
        logger.debug("Anthropic complete", model=self._model, tokens=response.usage.output_tokens)
        cost = calculate_cost(
            "anthropic",
            self._model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        if self._tracker is not None:
            self._tracker.track(
                UsageEntry(
                    timestamp=datetime.now().isoformat(),
                    provider="anthropic",
                    model=self._model,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    cost_usd=cost,
                    context=context,
                )
            )
        return text

    async def _stream(self, kwargs: dict) -> AsyncIterator[str]:
        for attempt in range(_MAX_ANTHROPIC_RETRIES):
            emitted = False
            try:
                async with self._client.messages.stream(**kwargs) as stream:
                    async for chunk in stream.text_stream:
                        emitted = True
                        yield chunk
                return
            except anthropic.APIError as e:
                # Un chunk deja `yield` est parti chez l'appelant : on ne peut pas
                # le reprendre. Rejouer la requete reemettrait le debut de la
                # reponse par-dessus, et l'utilisateur lirait le texte en double.
                # On ne retente donc que si rien n'a encore ete emis.
                if (
                    emitted
                    or not _is_retryable_anthropic(e)
                    or attempt + 1 >= _MAX_ANTHROPIC_RETRIES
                ):
                    raise
                await asyncio.sleep(min(2**attempt, 8))

    def stream_with_capture(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
    ) -> tuple[AsyncIterator[str], ToolCapture]:
        """Stream les tokens texte ET capture les tool_use blocks après épuisement.

        Le ToolCapture est populé dès que l'itérateur retourné est entièrement consommé.
        """
        capture = ToolCapture()
        kwargs: dict = {
            "model": self._model,
            "max_tokens": 4096,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        return self._stream_capturing(kwargs, capture), capture

    async def _stream_capturing(self, kwargs: dict, capture: ToolCapture) -> AsyncIterator[str]:
        """Stream text via raw events + peuple capture dès que chaque bloc tool_use est complet.

        Traite tous les événements en une passe — pas de get_final_message() séparé.
        capture.calls est peuplé dès content_block_stop pour chaque outil, ce qui permet
        à _pipe() de démarrer la task outil aussitôt que le stream texte est épuisé.
        """
        _input: dict[int, str] = {}
        _meta: dict[int, tuple[str, str]] = {}

        for attempt in range(_MAX_ANTHROPIC_RETRIES):
            emitted = False
            try:
                async with self._client.messages.stream(**kwargs) as s:
                    async for event in s:
                        if event.type == "content_block_delta":
                            delta = event.delta
                            if delta.type == "text_delta":
                                emitted = True
                                yield delta.text
                            elif delta.type == "input_json_delta" and delta.partial_json:
                                _input[event.index] = (
                                    _input.get(event.index, "") + delta.partial_json
                                )
                        elif event.type == "content_block_start":
                            cb = event.content_block
                            if cb.type == "tool_use":
                                _meta[event.index] = (cb.id, cb.name)
                                _input[event.index] = ""
                        elif event.type == "content_block_stop":
                            if event.index in _meta:
                                tool_id, tool_name = _meta[event.index]
                                raw = _input.get(event.index, "{}")
                                try:
                                    tool_input = _json.loads(raw)
                                except _json.JSONDecodeError:
                                    tool_input = {}
                                capture.calls.append((tool_id, tool_name, tool_input))
                        elif event.type == "message_delta":
                            sr = getattr(event.delta, "stop_reason", None)
                            if sr:
                                capture.stop_reason = sr
                return
            except anthropic.APIError as e:
                # Idem _stream : les accumulateurs d'outils se vident proprement,
                # mais le texte deja `yield` est irrecuperable. Pas de retry une
                # fois le premier token parti.
                if (
                    emitted
                    or not _is_retryable_anthropic(e)
                    or attempt + 1 >= _MAX_ANTHROPIC_RETRIES
                ):
                    raise
                _input.clear()
                _meta.clear()
                capture.calls.clear()
                await asyncio.sleep(min(2**attempt, 8))

    async def tool_loop(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
        tool_executor: Callable[[str, dict], Awaitable[str]],
        context: str = "",
    ) -> str:
        """Boucle tool use : appels non-streaming jusqu'à stop_reason != tool_use."""
        current = list(messages)

        for iteration in range(_MAX_TOOL_ITERATIONS):
            # `msgs=current` : lie la valeur de l'itération courante au moment de
            # la définition. `current` est rebindé en fin de boucle, et la lambda
            # est rejouée par _anthropic_retry — sans ce défaut d'argument, ruff
            # B023 signale la capture tardive.
            response = await _anthropic_retry(
                lambda msgs=current: self._client.messages.create(
                    model=self._model,
                    max_tokens=4096,
                    system=system,
                    messages=msgs,
                    tools=tools,
                )
            )
            cost = calculate_cost(
                "anthropic",
                self._model,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
            if self._tracker is not None:
                self._tracker.track(
                    UsageEntry(
                        timestamp=datetime.now().isoformat(),
                        provider="anthropic",
                        model=self._model,
                        input_tokens=response.usage.input_tokens,
                        output_tokens=response.usage.output_tokens,
                        cost_usd=cost,
                        context=context,
                    )
                )

            if response.stop_reason != "tool_use":
                text = "".join(
                    block.text
                    for block in response.content
                    if hasattr(block, "text") and block.text
                )
                logger.debug("Tool loop done", iterations=iteration + 1)
                return text

            # Sépare le contenu assistant et collecte les appels
            assistant_content = []
            tool_calls: list[tuple[str, str, dict]] = []  # (id, name, input)

            for block in response.content:
                if block.type == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    assistant_content.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        }
                    )
                    tool_calls.append((block.id, block.name, block.input))

            # Exécution parallèle de tous les outils
            results = await asyncio.gather(
                *(tool_executor(name, inputs) for _, name, inputs in tool_calls)
            )
            logger.debug("Tools called", names=[n for _, n, _ in tool_calls])

            tool_results = [
                {"type": "tool_result", "tool_use_id": tool_id, "content": result}
                for (tool_id, _, _), result in zip(tool_calls, results, strict=True)
            ]

            current = current + [
                {"role": "assistant", "content": assistant_content},
                {"role": "user", "content": tool_results},
            ]

        logger.warning("Tool loop max iterations reached", max=_MAX_TOOL_ITERATIONS)
        return "Je n'ai pas pu terminer — trop d'étapes."

    async def health_check(self) -> bool:
        try:
            await self._client.messages.create(
                model=self._model,
                max_tokens=1,
                system="ping",
                messages=[{"role": "user", "content": "ping"}],
            )
            return True
        except Exception as e:
            logger.error("Anthropic health check failed", error=str(e))
            return False


class MistralProvider(LLMProvider):
    """Provider Mistral via l'API OpenAI-compatible.

    Supporte le function calling natif (supports_tools=True) via le format OpenAI.
    Les messages Anthropic (tool_use/tool_result) sont convertis automatiquement
    par _messages_to_openai() avant chaque appel, ce qui permet à agent.synthesize()
    de fonctionner sans modification de core/.
    """

    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            api_key=settings.mistral_api_key.get_secret_value(),
            base_url="https://api.mistral.ai/v1",
        )
        self._model = settings.mistral_model

    @property
    def supports_tools(self) -> bool:
        return True

    async def complete(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        stream: bool = False,
        context: str = "",
    ) -> str | AsyncIterator[str]:
        full_messages = [{"role": "system", "content": system}, *_messages_to_openai(messages)]

        if stream:
            return self._stream(full_messages)

        kwargs: dict = {"model": self._model, "messages": full_messages}
        if tools:
            kwargs["tools"] = _claude_tools_to_openai(tools)
            kwargs["tool_choice"] = "auto"

        response = await self._client.chat.completions.create(**kwargs)
        text = response.choices[0].message.content or ""
        logger.debug("Mistral complete", model=self._model)
        return text

    async def _stream(self, messages: list[dict]) -> AsyncIterator[str]:
        stream = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            stream=True,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    def stream_with_capture(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
    ) -> tuple[AsyncIterator[str], ToolCapture]:
        """Stream + capture des tool calls Mistral (OpenAI streaming avec tool_call deltas).

        ToolCapture.calls est peuplé à l'épuisement du stream ; la passe de synthèse
        appelle ensuite complete() avec l'historique Anthropic converti via _messages_to_openai.
        """
        capture = ToolCapture()
        full_messages = [{"role": "system", "content": system}, *_messages_to_openai(messages)]
        openai_tools = _claude_tools_to_openai(tools) if tools else None
        return self._stream_capturing(full_messages, openai_tools, capture), capture

    async def _stream_capturing(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        capture: ToolCapture,
    ) -> AsyncIterator[str]:
        """Stream texte + accumule les tool_call deltas ; peuple capture à la fin."""
        kwargs: dict = {"model": self._model, "messages": messages, "stream": True}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        _calls: dict[int, dict] = {}  # index → {id, name, arguments}

        stream = await self._client.chat.completions.create(**kwargs)
        async for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta

            if delta.content:
                yield delta.content

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in _calls:
                        _calls[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc.id:
                        _calls[idx]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            _calls[idx]["name"] += tc.function.name
                        if tc.function.arguments:
                            _calls[idx]["arguments"] += tc.function.arguments

            if choice.finish_reason:
                capture.stop_reason = choice.finish_reason

        for idx in sorted(_calls.keys()):
            call = _calls[idx]
            try:
                tool_input = _json.loads(call["arguments"]) if call["arguments"] else {}
            except _json.JSONDecodeError:
                tool_input = {}
            capture.calls.append((call["id"], call["name"], tool_input))

    async def tool_loop(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
        tool_executor: Callable[[str, dict], Awaitable[str]],
        context: str = "",
    ) -> str:
        """Boucle tool use Mistral (function calling OpenAI-compatible)."""
        current: list[dict] = [
            {"role": "system", "content": system},
            *_messages_to_openai(messages),
        ]
        openai_tools = _claude_tools_to_openai(tools)

        for iteration in range(_MAX_TOOL_ITERATIONS):
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=current,
                tools=openai_tools,
                tool_choice="auto",
            )
            choice = response.choices[0]

            if choice.finish_reason != "tool_calls":
                logger.debug("Mistral tool loop done", iterations=iteration + 1)
                return choice.message.content or ""

            tc_list = choice.message.tool_calls or []
            current.append(
                {
                    "role": "assistant",
                    "content": choice.message.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tc_list
                    ],
                }
            )

            parsed: list[tuple[str, str, dict]] = []
            for tc in tc_list:
                try:
                    inp = _json.loads(tc.function.arguments or "{}")
                except _json.JSONDecodeError:
                    inp = {}
                parsed.append((tc.id, tc.function.name, inp))

            results = await asyncio.gather(*(tool_executor(name, inp) for _, name, inp in parsed))
            logger.debug("Mistral tools called", names=[n for _, n, _ in parsed])

            for (tool_id, _, _), result in zip(parsed, results, strict=True):
                current.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "content": result,
                    }
                )

        logger.warning("Mistral tool loop max iterations reached", max=_MAX_TOOL_ITERATIONS)
        return "Je n'ai pas pu terminer — trop d'étapes."

    async def health_check(self) -> bool:
        try:
            await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
            return True
        except Exception as e:
            logger.error("Mistral health check failed", error=str(e))
            return False


class GeminiProvider(LLMProvider):
    """Provider Google Gemini via SDK google-genai.

    Supporte le function calling natif (supports_tools=True).
    La passe de synthèse post-outils passe par complete() qui convertit
    automatiquement les messages Anthropic (tool_use/tool_result) vers les
    Content/Part Gemini via _messages_to_gemini().

    Clé et modèle viennent de `Settings` (GOOGLE_API_KEY ou GEMINI_API_KEY,
    GEMINI_MODEL) comme pour tous les autres providers. Ce provider lisait
    auparavant `os.environ` en direct, en contournant Settings : une clé
    présente sous un nom et attendue sous l'autre passait inaperçue, et le
    modèle par défaut codé en dur (`gemini-2.0-flash`) est mort depuis, ce qui
    produisait un 404 NOT_FOUND opaque au premier appel.
    """

    def __init__(
        self,
        model: str | None = None,
        max_tokens: int = 4096,
        thinking_budget: int | None = None,
        tracker: UsageTracker | None = None,
    ) -> None:
        try:
            from google import genai
        except ImportError as exc:
            raise ImportError("google-genai requis : pip install google-genai") from exc
        api_key = settings.google_api_key.get_secret_value()
        self._client = genai.Client(api_key=api_key)
        self._model = model or settings.gemini_model
        self._max_tokens = max_tokens
        # None = comportement par défaut du modèle ; 0 = raisonnement coupé.
        self._thinking_budget = thinking_budget
        self._tracker = tracker
        # Signatures de raisonnement, indexées par identifiant d'appel d'outil.
        # Cf. `_remember_signature` pour le pourquoi.
        self._thought_signatures: dict[str, bytes] = {}

    def set_tracker(self, tracker: UsageTracker) -> None:
        """Injection post-construction (utilisé pour providers créés par factory)."""
        self._tracker = tracker

    @property
    def supports_tools(self) -> bool:
        return True

    # ── Comptabilisation ────────────────────────────────────────────────────
    #
    # Gemini renvoie ses compteurs dans `usage_metadata`, présent aussi bien
    # sur une réponse complète que sur le dernier chunk d'un flux. Les tokens
    # de raisonnement (`thoughts_token_count`) sont facturés au tarif SORTIE
    # mais comptés à part par l'API : les ignorer sous-estimerait la note dès
    # qu'on rallume le raisonnement.

    @staticmethod
    def _compteur(usage: Any, champ: str) -> int:  # noqa: ANN401 — type SDK non exporté
        """Lit un compteur de `usage_metadata`, 0 si absent ou non numérique.

        La comptabilité ne doit jamais faire tomber une réponse : un champ
        manquant sur une révision de modèle, ou un objet inattendu, se solde
        par un zéro, pas par une exception au milieu du flux.
        """
        valeur = getattr(usage, champ, None)
        return valeur if isinstance(valeur, int) and not isinstance(valeur, bool) else 0

    def _track(self, usage: Any, context: str) -> None:  # noqa: ANN401 — type SDK non exporté
        if usage is None or self._tracker is None:
            return
        entree = self._compteur(usage, "prompt_token_count")
        sortie = self._compteur(usage, "candidates_token_count")
        sortie += self._compteur(usage, "thoughts_token_count")
        if not entree and not sortie:
            return
        cost = calculate_cost(
            "gemini",
            self._model,
            input_tokens=entree,
            output_tokens=sortie,
        )
        self._tracker.track(
            UsageEntry(
                timestamp=datetime.now().isoformat(),
                provider="gemini",
                model=self._model,
                input_tokens=entree,
                output_tokens=sortie,
                cost_usd=cost,
                context=context,
            )
        )

    # ── Signatures de raisonnement (Gemini 3.x) ─────────────────────────────
    #
    # Depuis Gemini 3, un `functionCall` renvoyé au modèle DOIT reporter le
    # `thought_signature` qui l'accompagnait, sinon l'API refuse la requête :
    #
    #     400 INVALID_ARGUMENT — "Function call is missing a thought_signature
    #     in functionCall parts."
    #
    # Cette signature vit sur le `Part`, pas sur le `FunctionCall`. Or le flux
    # à deux passes du Gateway (streaming avec capture, puis synthèse) ne
    # transporte pas d'objet Gemini entre les deux : la passe 2 RECONSTRUIT
    # les parties depuis des blocs au format Anthropic (`tool_use`), où la
    # signature n'a pas sa place. On la met donc de côté ici, indexée par
    # l'identifiant d'appel que l'on fabrique, pour la réattacher passe 2.
    #
    # `tool_loop()` n'a pas ce problème : il garde les objets natifs.

    _MAX_REMEMBERED_SIGNATURES = 64

    def _remember_signature(self, call_id: str, signature: bytes) -> None:
        """Mémorise une signature, en bornant la croissance du dictionnaire.

        Un provider vit aussi longtemps que le process ; sans borne, chaque
        appel d'outil y laisserait une trace définitive.
        """
        if len(self._thought_signatures) >= self._MAX_REMEMBERED_SIGNATURES:
            oldest = next(iter(self._thought_signatures))
            del self._thought_signatures[oldest]
        self._thought_signatures[call_id] = signature

    @staticmethod
    def _iter_parts(chunk: Any) -> list[Any]:  # noqa: ANN401 — type SDK non exporté
        """Parties du premier candidat d'un chunk, ou liste vide.

        Les chunks de fin de flux peuvent n'avoir ni candidat ni contenu.
        """
        candidates = getattr(chunk, "candidates", None) or []
        if not candidates:
            return []
        content = getattr(candidates[0], "content", None)
        return list(getattr(content, "parts", None) or [])

    # ── Helpers internes ────────────────────────────────────────────────────

    def _json_schema_to_gemini(self, schema: dict) -> object:
        """Convertit un schéma JSON (format Claude) vers types.Schema Gemini (récursif)."""

        type_map: dict[str, Any] = {
            "string": _t.Type.STRING,
            "number": _t.Type.NUMBER,
            "integer": _t.Type.INTEGER,
            "boolean": _t.Type.BOOLEAN,
            "array": _t.Type.ARRAY,
            "object": _t.Type.OBJECT,
        }
        schema_type = type_map.get(schema.get("type", "string"), _t.Type.STRING)
        kwargs: dict = {"type": schema_type}

        if desc := schema.get("description"):
            kwargs["description"] = desc
        if props := schema.get("properties"):
            kwargs["properties"] = {k: self._json_schema_to_gemini(v) for k, v in props.items()}
        if req := schema.get("required"):
            kwargs["required"] = req
        if items := schema.get("items"):
            kwargs["items"] = self._json_schema_to_gemini(items)
        if enum := schema.get("enum"):
            kwargs["enum"] = enum

        return _t.Schema(**kwargs)

    def _claude_tools_to_gemini(self, tools: list[dict]) -> list[Any]:
        """Convertit le schéma d'outils Claude vers une liste de Tool Gemini."""

        declarations = [
            _t.FunctionDeclaration(
                name=t["name"],
                description=t.get("description", ""),
                parameters=self._json_schema_to_gemini(t.get("input_schema", {})),
            )
            for t in tools
        ]
        return [_t.Tool(function_declarations=declarations)]

    def _build_config(self, system: str, tools: list[dict] | None) -> object:
        """Construit GenerateContentConfig avec système et outils optionnels."""

        config_kwargs: dict = {
            "system_instruction": system,
            "max_output_tokens": self._max_tokens,
        }
        if tools:
            config_kwargs["tools"] = self._claude_tools_to_gemini(tools)

        # Les modèles Gemini 2.5+ « réfléchissent » avant de répondre, et le
        # font par défaut. Ce raisonnement s'écoule AVANT le premier token
        # visible : sur une réponse vocale, il se paie intégralement en attente
        # devant l'utilisateur. `thinking_budget=0` le désactive.
        #
        # On ne le coupe que là où la latence prime (la voix, cf.
        # `create_voice_llm`). Le chat écrit garde le comportement par défaut :
        # quelques secondes de plus y sont invisibles, et le raisonnement aide
        # sur les enchaînements d'outils.
        if self._thinking_budget is not None:
            config_kwargs["thinking_config"] = _t.ThinkingConfig(
                thinking_budget=self._thinking_budget
            )
        return _t.GenerateContentConfig(**config_kwargs)

    def _signatures_completes(self, messages: list[dict]) -> bool:
        """True si chaque `tool_use` de TOUTE la conversation porte sa signature.

        La décision se prend sur l'ensemble des messages, pas tour par tour :
        `agent.py` place les `tool_use` et les `tool_result` correspondants dans
        deux messages distincts. Trancher séparément produirait un
        `functionResponse` sans son `functionCall`, que l'API refuse au même
        titre.

        Le tout-ou-rien est délibéré. Gemini exige une signature sur CHAQUE
        partie `functionCall` renvoyée, et rejette la requête s'il en manque
        une :

            400 INVALID_ARGUMENT — "Function call is missing a thought_signature
            in functionCall parts. […] `default_api:execute_script`, position 2."

        Panacher — natif pour les appels signés, texte pour les autres — ferait
        diverger le nombre de `functionCall` et de `functionResponse`, ce que
        l'API refuse aussi.

        Deux situations mènent à une signature manquante : le modèle n'en émet
        pas (raisonnement coupé, ou modèle antérieur à Gemini 3), et l'éviction
        du cache borné de `_remember_signature` sur une longue session.
        """
        for msg in messages:
            content = msg.get("content", "")
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") != "tool_use":
                    continue
                if not self._thought_signatures.get(block.get("id", "")):
                    return False
        return True

    def _tour_outils_natif(self, content: list[dict], id_to_name: dict[str, str]) -> list[Any]:
        """Tour d'outils au format natif, signatures réattachées."""
        model_parts: list[Any] = []
        user_parts: list[Any] = []

        for block in content:
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                model_parts.append(_t.Part(text=block["text"]))
            elif btype == "tool_use":
                model_parts.append(
                    _t.Part(
                        function_call=_t.FunctionCall(
                            name=block["name"],
                            args=block.get("input", {}),
                        ),
                        thought_signature=self._thought_signatures.get(block["id"]),
                    )
                )
            elif btype == "tool_result":
                tool_name = id_to_name.get(block["tool_use_id"], block["tool_use_id"])
                user_parts.append(
                    _t.Part(
                        function_response=_t.FunctionResponse(
                            name=tool_name,
                            response={"result": block.get("content", "")},
                        )
                    )
                )

        sortie: list[Any] = []
        if model_parts:
            sortie.append(_t.Content(role="model", parts=model_parts))
        if user_parts:
            sortie.append(_t.Content(role="user", parts=user_parts))
        return sortie

    def _tour_outils_en_texte(
        self, content: list[dict], id_to_name: dict[str, str]
    ) -> list[Any]:
        """Même tour, raconté en texte plutôt qu'en parties `functionCall`.

        L'exigence de signature ne porte que sur les parties `functionCall`. En
        décrivant l'aller-retour d'outil en clair, on la contourne sans rien
        perdre : le modèle a besoin de SAVOIR ce qui a été appelé et ce que ça a
        renvoyé, pas de recevoir la structure d'origine. La passe de synthèse ne
        déclare d'ailleurs aucun outil — elle n'a rien à enchaîner.
        """
        lignes_modele: list[str] = []
        lignes_resultat: list[str] = []

        for block in content:
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                lignes_modele.append(str(block["text"]))
            elif btype == "tool_use":
                try:
                    args = _json.dumps(block.get("input", {}), ensure_ascii=False)
                except (TypeError, ValueError):
                    args = str(block.get("input", {}))
                lignes_modele.append(f"[outil appelé] {block['name']}({args})")
            elif btype == "tool_result":
                nom = id_to_name.get(block["tool_use_id"], block["tool_use_id"])
                lignes_resultat.append(f"[résultat de {nom}]\n{block.get('content', '')}")

        sortie: list[Any] = []
        if lignes_modele:
            sortie.append(
                _t.Content(role="model", parts=[_t.Part(text="\n".join(lignes_modele))])
            )
        if lignes_resultat:
            sortie.append(
                _t.Content(role="user", parts=[_t.Part(text="\n\n".join(lignes_resultat))])
            )
        return sortie

    def _messages_to_gemini(self, messages: list[dict]) -> list[Any]:
        """Convertit les messages (format Anthropic ou simple) vers des Content Gemini.

        Gère la passe de synthèse où agent.py injecte des blocs tool_use / tool_result
        dans l'historique. Construit la map id→name en amont pour les FunctionResponse
        qui nécessitent le nom de la fonction, pas l'id.
        """

        # Passe 1 : map tool_use_id → tool_name pour résoudre les tool_result
        id_to_name: dict[str, str] = {}
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_use":
                        id_to_name[block["id"]] = block["name"]

        # Décision unique pour toute la conversion : natif si chaque appel
        # d'outil de l'historique porte sa signature, texte sinon.
        natif = self._signatures_completes(messages)

        result: list[Any] = []
        for msg in messages:
            role = "model" if msg["role"] == "assistant" else "user"
            content: Any = msg.get("content", "")

            if isinstance(content, str):
                result.append(_t.Content(role=role, parts=[_t.Part(text=content)]))
                continue

            has_tool_use = any(b.get("type") == "tool_use" for b in content)
            has_tool_result = any(b.get("type") == "tool_result" for b in content)

            if has_tool_use or has_tool_result:
                if natif:
                    result.extend(self._tour_outils_natif(content, id_to_name))
                else:
                    result.extend(self._tour_outils_en_texte(content, id_to_name))
            else:
                text = " ".join(b.get("text", "") for b in content if b.get("type") == "text")
                result.append(_t.Content(role=role, parts=[_t.Part(text=text)]))

        return result

    # ── Interface publique ───────────────────────────────────────────────────

    async def complete(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        stream: bool = False,
        context: str = "",
    ) -> str | AsyncIterator[str]:
        contents = self._messages_to_gemini(messages)
        config = self._build_config(system, tools)

        if stream:
            return self._stream(contents, config, context)

        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=contents,
            config=config,
        )
        self._track(getattr(response, "usage_metadata", None), context)
        text: str = response.text or ""
        logger.debug("Gemini complete", model=self._model)
        return text

    async def _stream(
        self,
        contents: list[Any],
        config: object,
        context: str = "",
    ) -> AsyncIterator[str]:
        stream = await self._client.aio.models.generate_content_stream(
            model=self._model,
            contents=contents,
            config=config,
        )
        usage = None
        async for chunk in stream:
            if chunk.text:
                yield chunk.text
            # Les compteurs n'arrivent qu'en fin de flux, mais certains modèles
            # les répètent : on garde simplement le dernier vu.
            usage = getattr(chunk, "usage_metadata", None) or usage
        self._track(usage, context)

    def stream_with_capture(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
    ) -> tuple[AsyncIterator[str], ToolCapture]:
        """Stream + capture des function calls Gemini.

        ToolCapture.calls est peuplé dès que chunk.function_calls est non-vide
        (dernier chunk du stream). La synthèse passe par complete() avec conversion
        automatique de l'historique Anthropic via _messages_to_gemini.
        """
        capture = ToolCapture()
        contents = self._messages_to_gemini(messages)
        config = self._build_config(system, tools)
        return self._stream_capturing(contents, config, capture), capture

    async def _stream_capturing(
        self,
        contents: list[Any],
        config: object,
        capture: ToolCapture,
    ) -> AsyncIterator[str]:
        """Stream texte + capture les function_calls Gemini en fin de stream."""
        seen_keys: set[str] = set()
        usage = None
        stream = await self._client.aio.models.generate_content_stream(
            model=self._model,
            contents=contents,
            config=config,
        )
        async for chunk in stream:
            if chunk.text:
                yield chunk.text
            usage = getattr(chunk, "usage_metadata", None) or usage
            # On parcourt les Part plutôt que le raccourci `chunk.function_calls` :
            # celui-ci ne donne que les FunctionCall, alors que le
            # `thought_signature` exigé par Gemini 3.x est porté par le Part.
            found_in_parts = False
            for part in self._iter_parts(chunk):
                fc = getattr(part, "function_call", None)
                if fc is None or not fc.name:
                    continue
                found_in_parts = True
                if fc.name in seen_keys:
                    continue
                seen_keys.add(fc.name)
                call_id = f"call_{fc.name}_{uuid.uuid4().hex[:8]}"
                capture.calls.append((call_id, fc.name, dict(fc.args) if fc.args else {}))
                capture.stop_reason = "tool_use"
                signature = getattr(part, "thought_signature", None)
                if signature:
                    self._remember_signature(call_id, signature)

            # Repli sur le raccourci quand aucune Part n'a porté d'appel : couvre
            # les modèles antérieurs à Gemini 3, qui n'émettent pas de signature.
            if not found_in_parts and chunk.function_calls:
                for fc in chunk.function_calls:
                    if not fc.name or fc.name in seen_keys:
                        continue
                    seen_keys.add(fc.name)
                    call_id = f"call_{fc.name}_{uuid.uuid4().hex[:8]}"
                    capture.calls.append((call_id, fc.name, dict(fc.args) if fc.args else {}))
                    capture.stop_reason = "tool_use"

        # `stream_with_capture` ne reçoit pas de contexte — c'est la voie
        # conversationnelle du Gateway, on l'étiquette comme telle.
        self._track(usage, "conversation")

    async def tool_loop(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
        tool_executor: Callable[[str, dict], Awaitable[str]],
        context: str = "",
    ) -> str:
        """Boucle tool use Gemini (function calling natif)."""

        contents: list[Any] = self._messages_to_gemini(messages)
        config = self._build_config(system, tools)

        for iteration in range(_MAX_TOOL_ITERATIONS):
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=contents,
                config=config,
            )
            self._track(getattr(response, "usage_metadata", None), context)

            function_calls = response.function_calls or []
            if not function_calls:
                text: str = response.text or ""
                logger.debug("Gemini tool loop done", iterations=iteration + 1)
                return text

            # Ajoute le contenu modèle (avec function_call parts) à l'historique
            candidate = response.candidates[0]
            contents.append(candidate.content)

            tool_calls_data = [(fc.name, dict(fc.args) if fc.args else {}) for fc in function_calls]
            results = await asyncio.gather(
                *(tool_executor(name, inp) for name, inp in tool_calls_data)
            )
            logger.debug("Gemini tools called", names=[n for n, _ in tool_calls_data])

            function_responses = [
                _t.Part(
                    function_response=_t.FunctionResponse(
                        name=name,
                        response={"result": result},
                    )
                )
                for (name, _), result in zip(tool_calls_data, results, strict=True)
            ]
            contents.append(_t.Content(role="user", parts=function_responses))

        logger.warning("Gemini tool loop max iterations reached", max=_MAX_TOOL_ITERATIONS)
        return "Je n'ai pas pu terminer — trop d'étapes."

    async def health_check(self) -> bool:
        try:
            await self._client.aio.models.generate_content(
                model=self._model,
                contents="ping",
            )
            return True
        except Exception as e:
            logger.error("Gemini health check failed", error=str(e))
            return False


class OpenAIProvider(LLMProvider):
    """Provider OpenAI API via SDK officiel.

    Supporte le function calling natif (supports_tools=True) via le format OpenAI.
    Les messages Anthropic (tool_use/tool_result) sont convertis automatiquement
    par _messages_to_openai() avant chaque appel, ce qui permet à agent.synthesize()
    et au gateway double-passe de fonctionner sans modification de engine/.
    """

    def __init__(
        self,
        model: str | None = None,
        tracker: UsageTracker | None = None,
    ) -> None:
        self._client = AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value())
        self._model = model or settings.openai_model
        self._tracker = tracker

    def set_tracker(self, tracker: UsageTracker) -> None:
        """Injection post-construction (utilisé pour providers créés par factory)."""
        self._tracker = tracker

    @property
    def supports_tools(self) -> bool:
        return True

    def _track(self, response: Any, context: str) -> None:  # noqa: ANN401
        usage = getattr(response, "usage", None)
        if usage is None or self._tracker is None:
            return
        cost = calculate_cost(
            "openai",
            self._model,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
        )
        self._tracker.track(
            UsageEntry(
                timestamp=datetime.now().isoformat(),
                provider="openai",
                model=self._model,
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
                cost_usd=cost,
                context=context,
            )
        )

    async def complete(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        stream: bool = False,
        context: str = "",
    ) -> str | AsyncIterator[str]:
        full_messages = [{"role": "system", "content": system}, *_messages_to_openai(messages)]

        if stream:
            return self._stream(full_messages)

        kwargs: dict = {"model": self._model, "messages": full_messages}
        if tools:
            kwargs["tools"] = _claude_tools_to_openai(tools)
            kwargs["tool_choice"] = "auto"

        response = await self._client.chat.completions.create(**kwargs)
        text = response.choices[0].message.content or ""
        logger.debug("OpenAI complete", model=self._model)
        self._track(response, context)
        return text

    async def _stream(self, messages: list[dict]) -> AsyncIterator[str]:
        stream = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            stream=True,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    def stream_with_capture(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
    ) -> tuple[AsyncIterator[str], ToolCapture]:
        """Stream texte + capture des tool calls OpenAI (deltas streaming).

        ToolCapture.calls est peuplé à l'épuisement du stream ; la passe de synthèse
        appelle ensuite complete() avec l'historique Anthropic converti.
        """
        capture = ToolCapture()
        full_messages = [{"role": "system", "content": system}, *_messages_to_openai(messages)]
        openai_tools = _claude_tools_to_openai(tools) if tools else None
        return self._stream_capturing(full_messages, openai_tools, capture), capture

    async def _stream_capturing(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        capture: ToolCapture,
    ) -> AsyncIterator[str]:
        """Stream texte + accumule les tool_call deltas ; peuple capture à la fin."""
        kwargs: dict = {"model": self._model, "messages": messages, "stream": True}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        _calls: dict[int, dict] = {}  # index → {id, name, arguments}

        stream = await self._client.chat.completions.create(**kwargs)
        async for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta

            if delta.content:
                yield delta.content

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in _calls:
                        _calls[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc.id:
                        _calls[idx]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            _calls[idx]["name"] += tc.function.name
                        if tc.function.arguments:
                            _calls[idx]["arguments"] += tc.function.arguments

            if choice.finish_reason:
                capture.stop_reason = choice.finish_reason

        for idx in sorted(_calls.keys()):
            call = _calls[idx]
            try:
                tool_input = _json.loads(call["arguments"]) if call["arguments"] else {}
            except _json.JSONDecodeError:
                tool_input = {}
            capture.calls.append((call["id"], call["name"], tool_input))

    async def tool_loop(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
        tool_executor: Callable[[str, dict], Awaitable[str]],
        context: str = "",
    ) -> str:
        """Boucle tool use OpenAI (function calling natif)."""
        current: list[dict] = [
            {"role": "system", "content": system},
            *_messages_to_openai(messages),
        ]
        openai_tools = _claude_tools_to_openai(tools)

        for iteration in range(_MAX_TOOL_ITERATIONS):
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=current,
                tools=openai_tools,
                tool_choice="auto",
            )
            self._track(response, context)
            choice = response.choices[0]

            if choice.finish_reason != "tool_calls":
                logger.debug("OpenAI tool loop done", iterations=iteration + 1)
                return choice.message.content or ""

            tc_list = choice.message.tool_calls or []
            current.append(
                {
                    "role": "assistant",
                    "content": choice.message.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tc_list
                    ],
                }
            )

            parsed: list[tuple[str, str, dict]] = []
            for tc in tc_list:
                try:
                    inp = _json.loads(tc.function.arguments or "{}")
                except _json.JSONDecodeError:
                    inp = {}
                parsed.append((tc.id, tc.function.name, inp))

            results = await asyncio.gather(*(tool_executor(name, inp) for _, name, inp in parsed))
            logger.debug("OpenAI tools called", names=[n for _, n, _ in parsed])

            for (tool_id, _, _), result in zip(parsed, results, strict=True):
                current.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "content": result,
                    }
                )

        logger.warning("OpenAI tool loop max iterations reached", max=_MAX_TOOL_ITERATIONS)
        return "Je n'ai pas pu terminer — trop d'étapes."

    async def health_check(self) -> bool:
        try:
            await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
            return True
        except Exception as e:
            logger.error("OpenAI health check failed", error=str(e))
            return False


def get_api_provider(
    backend: str = "anthropic",
    max_tokens: int = 2048,
    model: str | None = None,
    tracker: UsageTracker | None = None,
    low_latency: bool = False,
    thinking_budget: int | None = None,
) -> LLMProvider:
    """Retourne le provider API selon le backend demandé.

    `tracker` est passé aux providers Anthropic, OpenAI et Gemini (qui
    poussent une UsageEntry vers le tracker). Mistral l'ignore pour l'instant.
    `model` surcharge le modèle par défaut du backend (None = modèle .env).
    `low_latency` privilégie le délai avant le premier mot sur la qualité du
    raisonnement — voulu pour la voix, où l'attente est subie en direct.
    `thinking_budget` fixe explicitement le budget de raisonnement Gemini et
    l'emporte sur `low_latency` : c'est ce que demande le jumeau raisonnant
    du provider vocal (cf. `create_reflective_llm`).
    """
    if backend == "gemini":
        budget: int | None
        if thinking_budget is not None:
            budget = thinking_budget
        elif low_latency:
            budget = 0
        else:
            budget = settings.gemini_thinking_budget
        return GeminiProvider(
            model=model,
            max_tokens=max_tokens,
            thinking_budget=budget,
            tracker=tracker,
        )
    if backend == "mistral":
        return MistralProvider()
    if backend == "openai":
        return OpenAIProvider(model=model, tracker=tracker)
    return AnthropicProvider(max_tokens=max_tokens, model=model, tracker=tracker)
