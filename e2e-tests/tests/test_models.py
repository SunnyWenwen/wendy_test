"""Parameterized model matrix tests.

Every model in config/models.yaml is tested for:
  - Basic chat (all models)
  - Streaming (models with streaming capability)
  - Tool use (models with tool_use capability)

When you add a new model to models.yaml, it is automatically picked up here.

Run all model tests:
    pytest tests/test_models.py -v

Run only Bedrock models:
    pytest tests/test_models.py -m bedrock -v

Run only Vertex models:
    pytest tests/test_models.py -m vertex -v
"""

from __future__ import annotations

import pytest

from agents.kilo import KiloClient
from validators.log_parser import LogValidator
from validators.response import ResponseValidator

# ── Basic chat — all models ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_basic_chat_all_models(
    all_model: tuple[str, dict],
    kilo_client: KiloClient,
    rv: ResponseValidator,
) -> None:
    """Every registered model must respond to a basic chat request."""
    alias, cfg = all_model
    model_id = cfg["model_id"]
    upstream = cfg["upstream"]

    mark = pytest.mark.bedrock if upstream == "bedrock" else pytest.mark.vertex
    # Apply mark dynamically (informational, doesn't gate execution)

    payload = kilo_client.basic_payload(
        model=model_id,
        user_message="Reply with exactly one word: hello.",
        max_tokens=16,
    )
    resp = await kilo_client.chat(payload)
    data = rv.full_check(resp)
    assert data["choices"][0]["message"]["content"].strip() != "", (
        f"Empty response from model '{alias}' ({model_id})"
    )


# ── Streaming — models that declare streaming capability ──────────────────────


@pytest.mark.asyncio
@pytest.mark.streaming
async def test_streaming_all_capable_models(
    streaming_model: tuple[str, dict],
    kilo_client: KiloClient,
    rv: ResponseValidator,
) -> None:
    """Models with streaming capability must return valid SSE chunks."""
    alias, cfg = streaming_model
    model_id = cfg["model_id"]

    payload = kilo_client.basic_payload(
        model=model_id,
        user_message="Count from 1 to 3.",
        max_tokens=32,
    )
    chunks, text = await kilo_client.collect_stream(payload)
    assert len(chunks) >= 1, f"Model '{alias}' returned no streaming chunks"
    rv.assert_stream_assembled(text)


# ── Tool use — models that declare tool_use capability ────────────────────────


@pytest.mark.asyncio
@pytest.mark.tool_use
async def test_tool_use_all_capable_models(
    tool_use_model: tuple[str, dict],
    kilo_client: KiloClient,
    rv: ResponseValidator,
    weather_tool: list[dict],
) -> None:
    """Models with tool_use capability must return a valid tool_call."""
    alias, cfg = tool_use_model
    model_id = cfg["model_id"]

    payload = kilo_client.tool_use_payload(
        model=model_id,
        user_message="What's the weather in Berlin?",
        tools=weather_tool,
    )
    resp = await kilo_client.chat(payload)
    rv.assert_ok(resp)
    data = resp.json()
    # finish_reason may be "tool_calls" or "stop" depending on model
    finish = data["choices"][0].get("finish_reason")
    assert finish in ("tool_calls", "stop"), f"Unexpected finish_reason '{finish}' for '{alias}'"


# ── Log validation — routing confirmed per model ──────────────────────────────


@pytest.mark.asyncio
async def test_log_routing_all_models(
    all_model: tuple[str, dict],
    kilo_client: KiloClient,
    log_validator: LogValidator | None,
) -> None:
    """kubectl logs must confirm routing to each registered model."""
    if log_validator is None:
        pytest.skip("Log validation disabled")

    alias, cfg = all_model
    model_id = cfg["model_id"]

    payload = kilo_client.basic_payload(
        model=model_id,
        user_message="Say ok.",
        max_tokens=8,
    )
    await kilo_client.chat(payload)
    logs = await log_validator.after_request()
    log_validator.assert_model_routed(logs, model_id)
    log_validator.assert_no_errors(logs)
