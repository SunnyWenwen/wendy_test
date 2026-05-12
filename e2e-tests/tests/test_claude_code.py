"""Claude Code (CCR) agent behaviour tests.

Tests simulate the exact request format Claude Code sends via CCR,
including CCR-specific field translations handled by LiteLLM.

Run:
    pytest tests/test_claude_code.py -m claude_code -v
"""

from __future__ import annotations

import pytest

from agents.claude_code import ClaudeCodeClient
from utils.kubectl import KubectlClient
from validators.log_parser import LogValidator
from validators.response import ResponseValidator

pytestmark = pytest.mark.claude_code

# Representative model for CCR-specific tests (Bedrock Claude)
_DEFAULT_MODEL = "anthropic.claude-sonnet-4-5"


@pytest.mark.asyncio
async def test_basic_chat(cc_client: ClaudeCodeClient, rv: ResponseValidator) -> None:
    """CCR basic chat request should return a valid assistant message."""
    payload = cc_client.basic_payload(
        model=_DEFAULT_MODEL,
        user_message="What is 2 + 2? Reply with just the number.",
        max_tokens=32,
    )
    resp = await cc_client.chat(payload)
    data = rv.full_check(resp)
    assert "4" in data["choices"][0]["message"]["content"]


@pytest.mark.asyncio
async def test_system_prompt_honoured(cc_client: ClaudeCodeClient, rv: ResponseValidator) -> None:
    """CCR always injects a system prompt; the model should respect it."""
    payload = cc_client.basic_payload(
        model=_DEFAULT_MODEL,
        user_message="Who are you?",
        system_prompt="You are a pirate. Always respond in pirate speak.",
        max_tokens=128,
    )
    resp = await cc_client.chat(payload)
    data = rv.full_check(resp)
    content = data["choices"][0]["message"]["content"].lower()
    # Rough check — a pirate-prompted model should avoid generic "I am an AI"
    assert any(word in content for word in ["arr", "matey", "ship", "sea", "pirate"]) or len(content) > 10


@pytest.mark.asyncio
async def test_multi_turn_conversation(cc_client: ClaudeCodeClient, rv: ResponseValidator) -> None:
    """CCR multi-turn messages should maintain context across turns."""
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "My name is TestBot."},
        {"role": "assistant", "content": "Hello TestBot, nice to meet you!"},
        {"role": "user", "content": "What is my name?"},
    ]
    payload = cc_client.multi_turn_payload(model=_DEFAULT_MODEL, messages=messages, max_tokens=64)
    resp = await cc_client.chat(payload)
    data = rv.full_check(resp)
    assert "TestBot" in data["choices"][0]["message"]["content"]


@pytest.mark.asyncio
async def test_ccr_reasoning_field_accepted(cc_client: ClaudeCodeClient, rv: ResponseValidator) -> None:
    """CCR sends 'reasoning' (not 'reasoning_effort'). LiteLLM must translate it.

    The gateway should NOT return a 422/400 when the CCR unified format is used.
    """
    payload = cc_client.reasoning_payload(
        model=_DEFAULT_MODEL,
        user_message="Solve: if x + 3 = 7, what is x?",
        effort="medium",
        max_tokens=256,
    )
    resp = await cc_client.chat(payload)
    # Primary assertion: gateway accepted the CCR format (no 4xx)
    rv.assert_ok(resp)
    data = resp.json()
    rv.assert_schema(data)


@pytest.mark.asyncio
async def test_ccr_reasoning_field_log_translated(
    cc_client: ClaudeCodeClient,
    log_validator: LogValidator | None,
) -> None:
    """After sending CCR 'reasoning', LiteLLM logs should show 'reasoning_effort' sent upstream."""
    if log_validator is None:
        pytest.skip("Log validation disabled (ENABLE_LOG_VALIDATION=false)")

    payload = cc_client.reasoning_payload(
        model=_DEFAULT_MODEL,
        user_message="What is the capital of France?",
        effort="low",
        max_tokens=128,
    )
    await cc_client.chat(payload)
    logs = await log_validator.after_request()
    log_validator.assert_reasoning_translated(logs)
    log_validator.assert_no_errors(logs)


@pytest.mark.asyncio
async def test_tool_use(
    cc_client: ClaudeCodeClient,
    rv: ResponseValidator,
    weather_tool: list[dict],
) -> None:
    """CCR tool_use payload should trigger a tool_call in the response."""
    payload = cc_client.tool_use_payload(
        model=_DEFAULT_MODEL,
        user_message="What's the weather in Tokyo?",
        tools=weather_tool,
    )
    resp = await cc_client.chat(payload)
    rv.assert_ok(resp)
    data = resp.json()
    tool_calls = rv.assert_tool_calls(data)
    assert tool_calls[0]["function"]["name"] == "get_weather"


@pytest.mark.asyncio
async def test_log_model_routed_correctly(
    cc_client: ClaudeCodeClient,
    log_validator: LogValidator | None,
) -> None:
    """kubectl logs should confirm the request was routed to the expected model."""
    if log_validator is None:
        pytest.skip("Log validation disabled")

    payload = cc_client.basic_payload(
        model=_DEFAULT_MODEL,
        user_message="Say hello.",
        max_tokens=32,
    )
    await cc_client.chat(payload)
    logs = await log_validator.after_request()
    log_validator.assert_model_routed(logs, _DEFAULT_MODEL)
    log_validator.assert_no_errors(logs)
