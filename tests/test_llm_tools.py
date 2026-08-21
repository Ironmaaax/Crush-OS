# Copyright (C) 2026 Max Ea
# Copyright (C) 2026 Maxime Song — modifications de ce fork
# This file is part of CRUSH-OS,   .


from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── OllamaProvider — chat-only ────────────────────────────────────────────────


def test_ollama_supports_tools_true() -> None:
    """OllamaProvider supporte les outils (function calling Ollama) : supports_tools=True."""
    from crush.providers.llm.local import OllamaProvider

    provider = OllamaProvider()
    assert provider.supports_tools is True


# ── AnthropicProvider — régression ───────────────────────────────────────────


def test_anthropic_supports_tools_true() -> None:
    """AnthropicProvider annonce le support des outils (régression)."""
    with patch("crush.providers.llm.api.anthropic.AsyncAnthropic"):
        from crush.providers.llm.api import AnthropicProvider

        provider = AnthropicProvider()
        assert provider.supports_tools is True


# ── MistralProvider ───────────────────────────────────────────────────────────


def test_mistral_supports_tools_true() -> None:
    """MistralProvider annonce le support des outils."""
    with patch("crush.providers.llm.api.AsyncOpenAI"):
        from crush.providers.llm.api import MistralProvider

        provider = MistralProvider()
        assert provider.supports_tools is True


@pytest.mark.asyncio
async def test_mistral_tool_loop_executes_tool() -> None:
    """tool_loop Mistral : l'outil est exécuté et la synthèse LLM est retournée."""
    with patch("crush.providers.llm.api.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        # Réponse 1 : appel d'outil
        tc_mock = MagicMock()
        tc_mock.id = "call_01"
        tc_mock.function.name = "get_weather"
        tc_mock.function.arguments = json.dumps({"city": "Paris"})

        resp1 = MagicMock()
        resp1.choices[0].finish_reason = "tool_calls"
        resp1.choices[0].message.content = None
        resp1.choices[0].message.tool_calls = [tc_mock]

        # Réponse 2 : synthèse finale
        resp2 = MagicMock()
        resp2.choices[0].finish_reason = "stop"
        resp2.choices[0].message.content = "Il fait 25°C à Paris."
        resp2.choices[0].message.tool_calls = None

        mock_client.chat.completions.create = AsyncMock(side_effect=[resp1, resp2])

        from crush.providers.llm.api import MistralProvider

        provider = MistralProvider()
        executed: list[str] = []

        async def mock_executor(name: str, inputs: dict) -> str:
            executed.append(name)
            assert name == "get_weather"
            assert inputs.get("city") == "Paris"
            return "Ensoleillé, 25°C"

        result = await provider.tool_loop(
            messages=[{"role": "user", "content": "Quel temps à Paris ?"}],
            system="Tu es Crush.",
            tools=[
                {
                    "name": "get_weather",
                    "description": "Retourne la météo d'une ville.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "city": {"type": "string", "description": "Nom de la ville"}
                        },
                        "required": ["city"],
                    },
                }
            ],
            tool_executor=mock_executor,
        )

        assert "get_weather" in executed
        assert "25" in result or "Paris" in result
        assert mock_client.chat.completions.create.call_count == 2


@pytest.mark.asyncio
async def test_mistral_stream_with_capture_detects_tool() -> None:
    """stream_with_capture Mistral : un tool call delta peuple ToolCapture.calls."""
    with patch("crush.providers.llm.api.AsyncOpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        # Chunk texte
        chunk_text = MagicMock()
        chunk_text.choices = [MagicMock()]
        chunk_text.choices[0].delta.content = "Je vérifie..."
        chunk_text.choices[0].delta.tool_calls = None
        chunk_text.choices[0].finish_reason = None

        # Chunk tool_call
        tc_delta = MagicMock()
        tc_delta.index = 0
        tc_delta.id = "call_42"
        tc_delta.function.name = "get_weather"
        tc_delta.function.arguments = '{"city":"Lyon"}'

        chunk_tool = MagicMock()
        chunk_tool.choices = [MagicMock()]
        chunk_tool.choices[0].delta.content = None
        chunk_tool.choices[0].delta.tool_calls = [tc_delta]
        chunk_tool.choices[0].finish_reason = "tool_calls"

        async def _fake_chunks(*_args: object, **_kw: object) -> AsyncIterator[object]:
            for c in [chunk_text, chunk_tool]:
                yield c

        mock_client.chat.completions.create = AsyncMock(return_value=_fake_chunks())

        from crush.providers.llm.api import MistralProvider

        provider = MistralProvider()
        stream, capture = provider.stream_with_capture(
            messages=[{"role": "user", "content": "Météo Lyon ?"}],
            system="Tu es Crush.",
            tools=[
                {
                    "name": "get_weather",
                    "description": "Météo",
                    "input_schema": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                }
            ],
        )

        text_chunks: list[str] = []
        async for chunk in stream:
            text_chunks.append(chunk)

        assert capture.calls, "ToolCapture.calls doit contenir au moins un appel"
        assert capture.calls[0][1] == "get_weather"
        assert capture.calls[0][2] == {"city": "Lyon"}
        assert "Je vérifie..." in text_chunks


# ── GeminiProvider ────────────────────────────────────────────────────────────


def test_gemini_supports_tools_true() -> None:
    """GeminiProvider annonce le support des outils."""
    import google.genai  # noqa: F401 — force le chargement avant patch (idem)

    with patch("google.genai.Client"):
        from crush.providers.llm.api import GeminiProvider

        provider = GeminiProvider()
        assert provider.supports_tools is True


@pytest.mark.asyncio
async def test_gemini_tool_loop_executes_tool() -> None:
    """tool_loop Gemini : l'outil est exécuté et la synthèse LLM est retournée."""
    # NB : pré-charger google.genai pour éviter la fragilité de mock.patch
    # quand le namespace google a déjà été chargé partiellement (google.auth
    # via Calendar/Gmail Tool) — sinon `patch("google.genai.Client")` ne
    # capture pas correctement la classe selon l'ordre des tests.
    import google.genai  # noqa: F401 — force le chargement avant patch

    with patch("google.genai.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        # Réponse 1 : function call
        fc_mock = MagicMock()
        fc_mock.name = "get_weather"
        fc_mock.args = {"city": "Lyon"}

        part_fc = MagicMock()
        part_fc.function_call = fc_mock
        part_fc.text = None

        content1 = MagicMock()
        content1.parts = [part_fc]

        cand1 = MagicMock()
        cand1.content = content1

        resp1 = MagicMock()
        resp1.candidates = [cand1]
        resp1.function_calls = [fc_mock]
        resp1.text = None

        # Réponse 2 : texte final
        part_text = MagicMock()
        part_text.function_call = None
        part_text.text = "Il fait 22°C à Lyon."

        content2 = MagicMock()
        content2.parts = [part_text]

        cand2 = MagicMock()
        cand2.content = content2

        resp2 = MagicMock()
        resp2.candidates = [cand2]
        resp2.function_calls = None
        resp2.text = "Il fait 22°C à Lyon."

        mock_client.aio.models.generate_content = AsyncMock(side_effect=[resp1, resp2])

        from crush.providers.llm.api import GeminiProvider

        provider = GeminiProvider()
        executed: list[str] = []

        async def mock_executor(name: str, inputs: dict) -> str:
            executed.append(name)
            assert name == "get_weather"
            assert inputs.get("city") == "Lyon"
            return "Nuageux, 22°C"

        result = await provider.tool_loop(
            messages=[{"role": "user", "content": "Météo Lyon ?"}],
            system="Tu es Crush.",
            tools=[
                {
                    "name": "get_weather",
                    "description": "Retourne la météo.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                }
            ],
            tool_executor=mock_executor,
        )

        assert "get_weather" in executed
        assert "22" in result or "Lyon" in result
        assert mock_client.aio.models.generate_content.call_count == 2


@pytest.mark.asyncio
async def test_gemini_stream_with_capture_detects_tool() -> None:
    """stream_with_capture Gemini : chunk.function_calls peuple ToolCapture.calls."""
    # NB : pré-charger google.genai pour éviter la fragilité de mock.patch
    # quand le namespace google a déjà été chargé partiellement (google.auth
    # via Calendar/Gmail Tool) — sinon `patch("google.genai.Client")` ne
    # capture pas correctement la classe selon l'ordre des tests.
    import google.genai  # noqa: F401 — force le chargement avant patch

    with patch("google.genai.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        fc_mock = MagicMock()
        fc_mock.name = "search_web"
        fc_mock.args = {"query": "Python asyncio"}

        # Chunk texte
        chunk_text = MagicMock()
        chunk_text.text = "Je cherche..."
        chunk_text.function_calls = None

        # Chunk final avec function_call
        chunk_fc = MagicMock()
        chunk_fc.text = None
        chunk_fc.function_calls = [fc_mock]

        async def _fake_stream(*_args: object, **_kw: object) -> AsyncIterator[object]:
            for c in [chunk_text, chunk_fc]:
                yield c

        mock_client.aio.models.generate_content_stream = AsyncMock(return_value=_fake_stream())

        from crush.providers.llm.api import GeminiProvider

        provider = GeminiProvider()
        stream, capture = provider.stream_with_capture(
            messages=[{"role": "user", "content": "Cherche Python asyncio"}],
            system="Tu es Crush.",
            tools=[
                {
                    "name": "search_web",
                    "description": "Recherche web",
                    "input_schema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                }
            ],
        )

        text_chunks: list[str] = []
        async for chunk in stream:
            text_chunks.append(chunk)

        assert capture.calls, "ToolCapture.calls doit contenir au moins un appel"
        assert capture.calls[0][1] == "search_web"
        assert capture.calls[0][2] == {"query": "Python asyncio"}
        assert "Je cherche..." in text_chunks


# ── GeminiProvider — comptabilisation de la consommation ────────────────────
#
# RÉGRESSION : GeminiProvider était le seul provider API sans tracker. Aucun
# appel Gemini n'apparaissait dans memory_data/conso/, donc ni le tableau de
# bord ni le BudgetGuard ne voyaient passer la dépense — alors que Gemini est
# le backend actif et l'essentiel de la facture.


class _TrackerEspion:
    def __init__(self) -> None:
        self.entrees: list[object] = []

    def track(self, entry: object) -> None:
        self.entrees.append(entry)


@pytest.mark.asyncio
async def test_gemini_stream_comptabilise_la_consommation() -> None:
    """Le flux conversationnel pousse une UsageEntry chiffrée vers le tracker."""
    import google.genai  # noqa: F401 — force le chargement avant patch (idem)

    with patch("google.genai.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        chunk_texte = MagicMock()
        chunk_texte.text = "Bonsoir."
        chunk_texte.function_calls = None
        # Les compteurs n'arrivent qu'en fin de flux : explicitement absents ici.
        chunk_texte.usage_metadata = None
        chunk_texte.candidates = []

        usage = SimpleNamespace(
            prompt_token_count=3_000,
            candidates_token_count=100,
            thoughts_token_count=400,
        )
        chunk_fin = MagicMock()
        chunk_fin.text = None
        chunk_fin.function_calls = None
        chunk_fin.usage_metadata = usage
        chunk_fin.candidates = []

        async def _fake_stream(*_a: object, **_k: object) -> AsyncIterator[object]:
            for c in [chunk_texte, chunk_fin]:
                yield c

        mock_client.aio.models.generate_content_stream = AsyncMock(return_value=_fake_stream())

        from crush.providers.llm.api import GeminiProvider

        tracker = _TrackerEspion()
        provider = GeminiProvider(model="gemini-3-flash-preview", tracker=tracker)
        stream, _ = provider.stream_with_capture(
            messages=[{"role": "user", "content": "Bonsoir"}],
            system="Tu es Crush.",
        )
        async for _ in stream:
            pass

        assert len(tracker.entrees) == 1, "un échange = une entrée de consommation"
        entree = tracker.entrees[0]
        assert entree.provider == "gemini"  # type: ignore[attr-defined]
        assert entree.input_tokens == 3_000  # type: ignore[attr-defined]
        # Les tokens de raisonnement sont facturés en sortie et comptés à part
        # par l'API : 100 + 400.
        assert entree.output_tokens == 500  # type: ignore[attr-defined]
        assert entree.cost_usd == pytest.approx(3_000 / 1e6 * 0.50 + 500 / 1e6 * 3.00)
        assert entree.context == "conversation"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_gemini_sans_tracker_ne_casse_pas() -> None:
    """Sans tracker injecté, le flux se déroule normalement."""
    import google.genai  # noqa: F401 — force le chargement avant patch (idem)

    with patch("google.genai.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        chunk = MagicMock()
        chunk.text = "Bonsoir."
        chunk.function_calls = None
        chunk.candidates = []

        async def _fake_stream(*_a: object, **_k: object) -> AsyncIterator[object]:
            yield chunk

        mock_client.aio.models.generate_content_stream = AsyncMock(return_value=_fake_stream())

        from crush.providers.llm.api import GeminiProvider

        provider = GeminiProvider()
        stream, _ = provider.stream_with_capture(
            messages=[{"role": "user", "content": "Bonsoir"}],
            system="Tu es Crush.",
        )
        assert [c async for c in stream] == ["Bonsoir."]


# ── GeminiProvider — signatures de raisonnement (Gemini 3.x) ─────────────────
#
# RÉGRESSION : avec un modèle Gemini 3.x, tout usage d'outil échouait en
#   400 INVALID_ARGUMENT — "Function call is missing a thought_signature
#   in functionCall parts."
# La passe 1 (streaming avec capture) marchait, la passe 2 (synthèse) non :
# elle reconstruit les parties depuis des blocs Anthropic, où la signature
# n'existe pas. Elle est désormais mise de côté passe 1 et réattachée passe 2.


def _gemini_part(
    *,
    text: str | None = None,
    name: str | None = None,
    args: dict | None = None,
    signature: bytes | None = None,
) -> MagicMock:
    """Part Gemini réaliste : le SDK expose bien `thought_signature` sur le Part."""
    part = MagicMock()
    part.text = text
    if name is None:
        part.function_call = None
    else:
        fc = MagicMock()
        fc.name = name
        fc.args = args or {}
        part.function_call = fc
    part.thought_signature = signature
    return part


def _gemini_chunk(parts: list[MagicMock], *, text: str | None = None) -> MagicMock:
    chunk = MagicMock()
    chunk.text = text
    candidate = MagicMock()
    candidate.content.parts = parts
    chunk.candidates = [candidate]
    chunk.function_calls = [p.function_call for p in parts if p.function_call]
    return chunk


@pytest.mark.asyncio
async def test_gemini_capture_memorise_la_thought_signature() -> None:
    """Passe 1 : la signature portée par le Part doit être conservée."""
    with patch("google.genai.Client") as mock_cls:
        client = MagicMock()
        mock_cls.return_value = client

        chunk = _gemini_chunk(
            [_gemini_part(name="memory_search", args={"query": "prénom"}, signature=b"SIG-42")]
        )

        async def _stream(*_a: object, **_k: object) -> AsyncIterator[object]:
            yield chunk

        client.aio.models.generate_content_stream = AsyncMock(return_value=_stream())

        from crush.providers.llm.api import GeminiProvider

        provider = GeminiProvider()
        stream, capture = provider.stream_with_capture(
            messages=[{"role": "user", "content": "Cherche mon prénom"}],
            system="Tu es un assistant.",
            tools=[{"name": "memory_search", "description": "", "input_schema": {}}],
        )
        async for _ in stream:
            pass

        assert capture.calls, "l'appel d'outil doit être capturé"
        call_id = capture.calls[0][0]
        assert provider._thought_signatures[call_id] == b"SIG-42"


@pytest.mark.asyncio
async def test_gemini_synthese_reattache_la_signature() -> None:
    """Passe 2 : la Part reconstruite doit reporter la signature de la passe 1."""
    with patch("google.genai.Client") as mock_cls:
        mock_cls.return_value = MagicMock()

        from crush.providers.llm.api import GeminiProvider

        provider = GeminiProvider()
        provider._thought_signatures["call_abc"] = b"SIG-42"

        contents = provider._messages_to_gemini([
            {"role": "user", "content": "Cherche mon prénom"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "call_abc", "name": "memory_search", "input": {}}
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call_abc", "content": "Max"}
                ],
            },
        ])

        parts = [p for c in contents for p in c.parts if p.function_call is not None]
        assert parts, "la partie functionCall doit être reconstruite"
        assert parts[0].thought_signature == b"SIG-42"


@pytest.mark.asyncio
async def test_gemini_sans_signature_bascule_en_texte() -> None:
    """Signature absente : le tour est raconté en texte, pas en functionCall.

    RÉGRESSION. Renvoyer une partie `functionCall` avec `thought_signature=None`
    faisait rejeter la requête entière :

        400 INVALID_ARGUMENT — "Function call is missing a thought_signature in
        functionCall parts […] `default_api:execute_script`, position 2."

    Le cas se produit dès qu'un tour comporte PLUSIEURS appels et que le flux
    n'a pas porté de signature sur chacun — vu en production sitôt le
    raisonnement réactivé. L'exigence ne pesant que sur les parties
    `functionCall`, on décrit l'aller-retour en clair.
    """
    with patch("google.genai.Client") as mock_cls:
        mock_cls.return_value = MagicMock()

        from crush.providers.llm.api import GeminiProvider

        provider = GeminiProvider()
        contents = provider._messages_to_gemini([
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "inconnu", "name": "weather", "input": {}}
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "inconnu", "content": "22°C"}
                ],
            },
        ])

        parts = [p for c in contents for p in c.parts]
        assert not [p for p in parts if p.function_call is not None], (
            "aucune partie functionCall ne doit partir sans signature"
        )
        texte = "\n".join(p.text or "" for p in parts)
        assert "weather" in texte, "le modèle doit savoir quel outil a été appelé"
        assert "22°C" in texte, "…et ce qu'il a renvoyé"


@pytest.mark.asyncio
async def test_gemini_signature_partielle_bascule_tout_le_tour() -> None:
    """Un seul appel signé sur deux ne suffit pas : tout le tour passe en texte.

    Panacher ferait diverger le nombre de `functionCall` et de
    `functionResponse`, que l'API refuse tout autant.
    """
    with patch("google.genai.Client") as mock_cls:
        mock_cls.return_value = MagicMock()

        from crush.providers.llm.api import GeminiProvider

        provider = GeminiProvider()
        provider._remember_signature("call_1", b"SIG-1")  # le second reste sans
        contents = provider._messages_to_gemini([
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "call_1", "name": "weather", "input": {}},
                    {"type": "tool_use", "id": "call_2", "name": "execute_script", "input": {}},
                ],
            },
        ])
        parts = [p for c in contents for p in c.parts]
        assert not [p for p in parts if p.function_call is not None]


def test_gemini_borne_le_nombre_de_signatures() -> None:
    """Le provider vit autant que le process : le dictionnaire doit être borné."""
    with patch("google.genai.Client") as mock_cls:
        mock_cls.return_value = MagicMock()

        from crush.providers.llm.api import GeminiProvider

        provider = GeminiProvider()
        limite = GeminiProvider._MAX_REMEMBERED_SIGNATURES
        for i in range(limite + 25):
            provider._remember_signature(f"call_{i}", b"x")
        assert len(provider._thought_signatures) <= limite
        # Les plus récentes survivent, les plus anciennes sont évincées.
        assert f"call_{limite + 24}" in provider._thought_signatures
        assert "call_0" not in provider._thought_signatures
