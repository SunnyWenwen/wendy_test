"""Tool / function calling E2E tests.

Tests the full tool_use flow: request with tools → tool_call response →
re-submit with tool result → final assistant answer.

Run:
    pytest tests/test_tool_use.py -m tool_use -v
"""

from __future__ import annotations

import json

import pytest

from agents.claude_code import ClaudeCodeClient
from agents.kilo import KiloClient
from validators.response import ResponseValidator

pytestmark = pytest.mark.tool_use

_MODEL = "anthropic.claude-sonnet-4-5"


@pytest.mark.asyncio
async def test_kilo_single_tool_call(
    kilo_client: KiloClient,
    rv: ResponseValidator,
    weather_tool: list[dict],
) -> None:
    """Kilo: model should call get_weather when asked about weather."""
    payload = kilo_client.tool_use_payload(
        model=_MODEL,
        user_message="What is the weather in Tokyo right now?",
        tools=weather_tool,
    )
    resp = await kilo_client.chat(payload)
    rv.assert_ok(resp)
    data = resp.json()
    tool_calls = rv.assert_tool_calls(data)
    call = tool_calls[0]
    assert call["function"]["name"] == "get_weather"
    args = json.loads(call["function"]["arguments"])
    assert "tokyo" in args.get("city", "").lower()


@pytest.mark.asyncio
async def test_kilo_full_tool_round_trip(
    kilo_client: KiloClient,
    rv: ResponseValidator,
    weather_tool: list[dict],
) -> None:
    """Full tool round-trip: tool_call → inject result → final answer."""
    # Step 1: Get tool call
    payload = kilo_client.tool_use_payload(
        model=_MODEL,
        user_message="What is the weather in London?",
        tools=weather_tool,
    )
    resp1 = await kilo_client.chat(payload)
    rv.assert_ok(resp1)
    data1 = resp1.json()
    tool_calls = rv.assert_tool_calls(data1)
    call = tool_calls[0]
    call_id = call["id"]

    # Step 2: Submit tool result
    messages_with_result = [
        {"role": "system", "content": "You are a helpful assistant with tools."},
        {"role": "user", "content": "What is the weather in London?"},
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps({"city": "London", "temperature": "15°C", "condition": "cloudy"}),
        },
    ]
    payload2 = {
        "model": _MODEL,
        "messages": messages_with_result,
        "tools": weather_tool,
        "max_tokens": 128,
    }
    resp2 = await kilo_client.chat(payload2)
    data2 = rv.full_check(resp2)
    content = data2["choices"][0]["message"]["content"]
    assert content and len(content) > 5, "Expected a substantive final answer after tool result"


@pytest.mark.asyncio
async def test_cc_tool_use(
    cc_client: ClaudeCodeClient,
    rv: ResponseValidator,
    weather_tool: list[dict],
) -> None:
    """Claude Code (CCR) tool_use should also work through the gateway."""
    payload = cc_client.tool_use_payload(
        model=_MODEL,
        user_message="What is the weather in Sydney?",
        tools=weather_tool,
    )
    resp = await cc_client.chat(payload)
    rv.assert_ok(resp)
    data = resp.json()
    tool_calls = rv.assert_tool_calls(data)
    assert tool_calls[0]["function"]["name"] == "get_weather"


@pytest.mark.asyncio
async def test_tool_choice_none(kilo_client: KiloClient, rv: ResponseValidator, weather_tool: list[dict]) -> None:
    """tool_choice='none' should prevent the model from calling tools."""
    payload = {
        "model": _MODEL,
        "messages": [{"role": "user", "content": "What is the weather in Rome?"}],
        "tools": weather_tool,
        "tool_choice": "none",
        "max_tokens": 64,
    }
    resp = await kilo_client.chat(payload)
    rv.assert_ok(resp)
    data = resp.json()
    tool_calls = data["choices"][0]["message"].get("tool_calls", [])
    assert not tool_calls, "Expected no tool_calls when tool_choice='none'"
    # Model should respond in text instead
    assert data["choices"][0]["message"]["content"]
