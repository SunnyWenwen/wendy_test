"""Kilo agent behaviour tests.

Kilo uses standard OpenAI-compatible payloads (no CCR layer).
Tests cover both fixture-based payloads (captured from real Kilo sessions)
and programmatically built payloads.

Run:
    pytest tests/test_kilo.py -m kilo -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.kilo import KiloClient
from utils.kubectl import KubectlClient
from validators.log_parser import LogValidator
from validators.response import ResponseValidator

pytestmark = pytest.mark.kilo

_DEFAULT_MODEL = "anthropic.claude-sonnet-4-5"
_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "kilo_payloads"


@pytest.mark.asyncio
async def test_basic_chat(kilo_client: KiloClient, rv: ResponseValidator) -> None:
    """Kilo basic chat should return a valid assistant message."""
    payload = kilo_client.basic_payload(
        model=_DEFAULT_MODEL,
        user_message="What is 2 + 2? Reply with just the number.",
        max_tokens=32,
    )
    resp = await kilo_client.chat(payload)
    data = rv.full_check(resp)
    assert "4" in data["choices"][0]["message"]["content"]


@pytest.mark.asyncio
async def test_openai_compatible_format_accepted(kilo_client: KiloClient, rv: ResponseValidator) -> None:
    """Gateway must accept standard OpenAI payload without modification."""
    payload = {
        "model": _DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": "You are a concise assistant."},
            {"role": "user", "content": "Name a colour."},
        ],
        "max_tokens": 16,
        "temperature": 0.5,
    }
    resp = await kilo_client.chat(payload)
    rv.full_check(resp)


@pytest.mark.asyncio
async def test_tool_use(
    kilo_client: KiloClient,
    rv: ResponseValidator,
    weather_tool: list[dict],
) -> None:
    """Kilo tool_use should result in a tool_call response."""
    payload = kilo_client.tool_use_payload(
        model=_DEFAULT_MODEL,
        user_message="What's the weather in Paris?",
        tools=weather_tool,
    )
    resp = await kilo_client.chat(payload)
    rv.assert_ok(resp)
    data = resp.json()
    tool_calls = rv.assert_tool_calls(data)
    assert tool_calls[0]["function"]["name"] == "get_weather"


@pytest.mark.asyncio
async def test_streaming(kilo_client: KiloClient, rv: ResponseValidator) -> None:
    """Kilo streaming=true should yield multiple SSE chunks and assemble valid text."""
    payload = kilo_client.basic_payload(
        model=_DEFAULT_MODEL,
        user_message="Count from 1 to 5.",
        max_tokens=64,
    )
    chunks, text = await kilo_client.collect_stream(payload)
    assert len(chunks) > 1, "Expected multiple SSE chunks for streaming response"
    rv.assert_stream_assembled(text)
    for chunk in chunks:
        rv.assert_streaming_chunk(chunk)


@pytest.mark.asyncio
async def test_reasoning_effort_standard(kilo_client: KiloClient, rv: ResponseValidator) -> None:
    """Kilo sends standard OpenAI reasoning_effort (not CCR format). Must be accepted."""
    payload = kilo_client.reasoning_payload(
        model=_DEFAULT_MODEL,
        user_message="If a train travels 60 km/h for 2 hours, how far does it go?",
        reasoning_effort="medium",
        max_tokens=256,
    )
    resp = await kilo_client.chat(payload)
    rv.assert_ok(resp)
    data = resp.json()
    rv.assert_schema(data)


@pytest.mark.asyncio
async def test_fixture_basic_chat(kilo_client: KiloClient, rv: ResponseValidator) -> None:
    """Use a real captured Kilo payload fixture (if available)."""
    fixture_path = _FIXTURES_DIR / "basic_chat.json"
    if not fixture_path.exists():
        pytest.skip("Kilo fixture 'basic_chat.json' not yet added — provide real captured payloads")

    payload = kilo_client.from_fixture("basic_chat", override_model=_DEFAULT_MODEL)
    resp = await kilo_client.chat(payload)
    rv.full_check(resp)


@pytest.mark.asyncio
async def test_log_no_errors_kilo(
    kilo_client: KiloClient,
    log_validator: LogValidator | None,
) -> None:
    """kubectl logs should show no errors after a standard Kilo request."""
    if log_validator is None:
        pytest.skip("Log validation disabled")

    payload = kilo_client.basic_payload(
        model=_DEFAULT_MODEL,
        user_message="Say hello.",
        max_tokens=32,
    )
    await kilo_client.chat(payload)
    logs = await log_validator.after_request()
    log_validator.assert_no_errors(logs)
    log_validator.assert_model_routed(logs, _DEFAULT_MODEL)
