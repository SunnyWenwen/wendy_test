"""SSE streaming end-to-end tests.

Verifies both agent clients produce correct streaming behaviour through the
full gateway stack (API6 → LiteLLM → upstream).

Run:
    pytest tests/test_streaming.py -m streaming -v
"""

from __future__ import annotations

import pytest

from agents.claude_code import ClaudeCodeClient
from agents.kilo import KiloClient
from config import settings
from validators.response import ResponseValidator

pytestmark = pytest.mark.streaming

_MODEL_BEDROCK = settings.DEFAULT_BEDROCK_MODEL
_MODEL_VERTEX = settings.DEFAULT_VERTEX_MODEL


@pytest.mark.asyncio
async def test_cc_streaming_bedrock(cc_client: ClaudeCodeClient, rv: ResponseValidator) -> None:
    """Claude Code streaming via Bedrock must yield multiple chunks."""
    payload = cc_client.basic_payload(
        model=_MODEL_BEDROCK,
        user_message="List the planets in our solar system, one per line.",
        max_tokens=128,
    )
    chunks, text = await cc_client.collect_stream(payload)
    assert len(chunks) > 1, "Expected >1 chunks from Bedrock streaming"
    rv.assert_stream_assembled(text)
    assert len(text) > 20


@pytest.mark.asyncio
async def test_cc_streaming_vertex(cc_client: ClaudeCodeClient, rv: ResponseValidator) -> None:
    """Claude Code streaming via Vertex AI must yield multiple chunks."""
    payload = cc_client.basic_payload(
        model=_MODEL_VERTEX,
        user_message="List the planets in our solar system, one per line.",
        max_tokens=128,
    )
    chunks, text = await cc_client.collect_stream(payload)
    assert len(chunks) > 1, "Expected >1 chunks from Vertex streaming"
    rv.assert_stream_assembled(text)


@pytest.mark.asyncio
async def test_kilo_streaming_bedrock(kilo_client: KiloClient, rv: ResponseValidator) -> None:
    """Kilo streaming with stream_options.include_usage=true must include usage in final chunk."""
    payload = kilo_client.streaming_payload(
        model=_MODEL_BEDROCK,
        user_message="Count from 1 to 5.",
        max_tokens=64,
    )
    chunks, text = await kilo_client.collect_stream(payload)
    assert len(chunks) > 1
    rv.assert_stream_assembled(text)

    # Check usage appears somewhere in the chunks
    usage_found = any(c.get("usage") for c in chunks)
    assert usage_found, "Expected usage info in streaming chunks when stream_options.include_usage=true"


@pytest.mark.asyncio
async def test_streaming_chunk_schema(kilo_client: KiloClient, rv: ResponseValidator) -> None:
    """Every SSE chunk must conform to the OpenAI streaming chunk schema."""
    payload = kilo_client.basic_payload(
        model=_MODEL_BEDROCK,
        user_message="Write a haiku.",
        max_tokens=64,
    )
    chunks, _ = await kilo_client.collect_stream(payload)
    for chunk in chunks:
        rv.assert_streaming_chunk(chunk)


@pytest.mark.asyncio
async def test_streaming_finish_reason(kilo_client: KiloClient) -> None:
    """The last non-empty chunk must have finish_reason='stop'."""
    payload = kilo_client.basic_payload(
        model=_MODEL_BEDROCK,
        user_message="Say hi.",
        max_tokens=32,
    )
    chunks, _ = await kilo_client.collect_stream(payload)
    finish_reasons = [
        c["choices"][0].get("finish_reason")
        for c in chunks
        if c.get("choices")
    ]
    assert "stop" in finish_reasons, (
        f"Expected 'stop' finish_reason in stream, got: {finish_reasons}"
    )
