"""Connectivity smoke tests — verify the gateway is reachable and routing works.

Run these first before any functional tests:
    pytest tests/test_connectivity.py -m connectivity -v
"""

import pytest

from agents.claude_code import ClaudeCodeClient
from agents.kilo import KiloClient
from config import settings
from validators.response import ResponseValidator

pytestmark = pytest.mark.connectivity

# Use the lightest available model for connectivity checks
_SMOKE_MODEL_BEDROCK = settings.DEFAULT_BEDROCK_MODEL
_SMOKE_MODEL_VERTEX = settings.DEFAULT_VERTEX_MODEL


@pytest.mark.asyncio
async def test_gateway_reachable(cc_client: ClaudeCodeClient) -> None:
    """Gateway must respond to a minimal request without crashing."""
    payload = cc_client.basic_payload(
        model=_SMOKE_MODEL_BEDROCK,
        user_message="Say 'ok'.",
        max_tokens=16,
    )
    resp = await cc_client.chat(payload)
    ResponseValidator.assert_ok(resp)


@pytest.mark.asyncio
async def test_bedrock_route_available(cc_client: ClaudeCodeClient, rv: ResponseValidator) -> None:
    """At least one Bedrock model must respond successfully."""
    payload = cc_client.basic_payload(
        model=_SMOKE_MODEL_BEDROCK,
        user_message="Reply with a single word: hello.",
        max_tokens=16,
    )
    resp = await cc_client.chat(payload)
    data = rv.full_check(resp)
    assert data["choices"][0]["message"]["content"].strip() != ""


@pytest.mark.asyncio
async def test_vertex_route_available(cc_client: ClaudeCodeClient, rv: ResponseValidator) -> None:
    """At least one Vertex AI model must respond successfully."""
    payload = cc_client.basic_payload(
        model=_SMOKE_MODEL_VERTEX,
        user_message="Reply with a single word: hello.",
        max_tokens=16,
    )
    resp = await cc_client.chat(payload)
    data = rv.full_check(resp)
    assert data["choices"][0]["message"]["content"].strip() != ""


@pytest.mark.asyncio
async def test_auth_required(cc_client: ClaudeCodeClient) -> None:
    """Requests with an invalid API key must be rejected (401 or 403)."""
    import httpx
    from config import settings as s

    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer invalid-key-for-test",
    }
    async with httpx.AsyncClient(base_url=s.GATEWAY_BASE_URL, timeout=30) as client:
        resp = await client.post(
            s.CHAT_COMPLETIONS_PATH,
            headers=headers,
            json={
                "model": _SMOKE_MODEL_BEDROCK,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 8,
            },
        )
    assert resp.status_code in (401, 403), (
        f"Expected 401/403 for bad API key, got {resp.status_code}"
    )


@pytest.mark.asyncio
async def test_kilo_client_connectivity(kilo_client: KiloClient, rv: ResponseValidator) -> None:
    """Kilo OpenAI-compatible client must also reach the gateway."""
    payload = kilo_client.basic_payload(
        model=_SMOKE_MODEL_BEDROCK,
        user_message="Say 'ok'.",
        max_tokens=16,
    )
    resp = await kilo_client.chat(payload)
    rv.full_check(resp)
