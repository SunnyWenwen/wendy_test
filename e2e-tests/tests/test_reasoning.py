"""Extended thinking / reasoning tests.

Two code paths exercised:
  1. Claude Code (CCR) path: sends {"reasoning": {"effort": "..."}} —
     LiteLLM must translate to {"reasoning_effort": "..."} before sending to upstream.
  2. Kilo path: sends {"reasoning_effort": "..."} directly (standard OpenAI format).

Only models with the 'reasoning' capability are included (see models.yaml).

Run:
    pytest tests/test_reasoning.py -m reasoning -v
"""

from __future__ import annotations

import pytest

from agents.claude_code import ClaudeCodeClient
from agents.kilo import KiloClient
from config import settings
from utils.kubectl import KubectlClient
from validators.log_parser import LogValidator
from validators.response import ResponseValidator

pytestmark = pytest.mark.reasoning

_DEFAULT_MODEL = settings.DEFAULT_BEDROCK_MODEL
_REASONING_MODEL = settings.DEFAULT_REASONING_MODEL

_REASONING_QUESTION = (
    "A snail is at the bottom of a 10-metre well. Each day it climbs 3 metres "
    "and each night it slips back 2 metres. How many days does it take to reach the top?"
)


# ── CCR reasoning path ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cc_reasoning_low(cc_client: ClaudeCodeClient, rv: ResponseValidator) -> None:
    payload = cc_client.reasoning_payload(
        model=_DEFAULT_MODEL,
        user_message=_REASONING_QUESTION,
        effort="low",
        max_tokens=512,
    )
    resp = await cc_client.chat(payload)
    rv.assert_ok(resp)
    data = resp.json()
    rv.assert_schema(data)
    content = rv.assert_has_content(data)
    assert "8" in content, f"Expected answer '8 days' in response, got: {content[:200]}"


@pytest.mark.asyncio
async def test_cc_reasoning_medium(cc_client: ClaudeCodeClient, rv: ResponseValidator) -> None:
    payload = cc_client.reasoning_payload(
        model=_DEFAULT_MODEL,
        user_message=_REASONING_QUESTION,
        effort="medium",
        max_tokens=1024,
    )
    resp = await cc_client.chat(payload)
    rv.assert_ok(resp)
    rv.assert_schema(resp.json())


@pytest.mark.asyncio
async def test_cc_reasoning_high(cc_client: ClaudeCodeClient, rv: ResponseValidator) -> None:
    payload = cc_client.reasoning_payload(
        model=_REASONING_MODEL,
        user_message=_REASONING_QUESTION,
        effort="high",
        max_tokens=2048,
    )
    resp = await cc_client.chat(payload)
    rv.assert_ok(resp)
    rv.assert_schema(resp.json())


# ── Kilo reasoning path ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_kilo_reasoning_effort(kilo_client: KiloClient, rv: ResponseValidator) -> None:
    """Kilo sends reasoning_effort directly; must pass through unchanged."""
    payload = kilo_client.reasoning_payload(
        model=_DEFAULT_MODEL,
        user_message=_REASONING_QUESTION,
        reasoning_effort="medium",
        max_tokens=1024,
    )
    resp = await kilo_client.chat(payload)
    rv.assert_ok(resp)
    rv.assert_schema(resp.json())


# ── Parameterised — all reasoning-capable models ──────────────────────────────

@pytest.mark.asyncio
@pytest.mark.slow
async def test_reasoning_all_capable_models(
    reasoning_model: tuple[str, dict],
    kilo_client: KiloClient,
    rv: ResponseValidator,
) -> None:
    """Every reasoning-capable model must handle reasoning_effort without error."""
    alias, cfg = reasoning_model
    payload = kilo_client.reasoning_payload(
        model=cfg["model_id"],
        user_message="What is 17 × 23?",
        reasoning_effort="low",
        max_tokens=256,
    )
    resp = await kilo_client.chat(payload)
    rv.assert_ok(resp)
    data = resp.json()
    rv.assert_schema(data)
    content = rv.assert_has_content(data)
    assert "391" in content, f"Wrong answer from model '{alias}': {content[:200]}"


# ── Log validation — confirm translation ──────────────────────────────────────

@pytest.mark.asyncio
async def test_ccr_reasoning_translated_in_logs(
    cc_client: ClaudeCodeClient,
    log_validator: LogValidator | None,
) -> None:
    """LiteLLM logs must show reasoning_effort (not the CCR reasoning dict) after translation."""
    if log_validator is None:
        pytest.skip("Log validation disabled")

    payload = cc_client.reasoning_payload(
        model=_DEFAULT_MODEL,
        user_message="What is 5 factorial?",
        effort="low",
        max_tokens=128,
    )
    await cc_client.chat(payload)
    logs = await log_validator.after_request()
    log_validator.assert_reasoning_translated(logs)
    log_validator.assert_no_errors(logs)
