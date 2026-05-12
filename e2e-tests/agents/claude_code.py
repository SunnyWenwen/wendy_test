"""Claude Code agent simulator.

Claude Code sends requests through CCR (Claude Code Router/Relay) which
produces a *unified* internal format before hitting LiteLLM.  The key
differences from vanilla OpenAI format observed so far:

  - Uses `reasoning` (dict with `effort` key) instead of `reasoning_effort`
    (string).  LiteLLM translates this before forwarding to the upstream.
  - May include `anthropic_beta` header hints forwarded as extra body fields.
  - System prompt is always provided as the first message with role="system".

This simulator replicates those characteristics so tests exercise the same
code path as a real Claude Code session.

NOTE: As CCR's full format spec becomes clearer, extend _CCR_EXTRA_FIELDS and
the payload builders below rather than touching the test files.
"""

from __future__ import annotations

from typing import Any

from config import settings
from agents.base import AgentClient

# Fields that CCR adds on top of standard OpenAI payload.
# Extend this dict when new CCR-specific fields are discovered.
_CCR_EXTRA_FIELDS: dict[str, Any] = {}

# Reasoning level → CCR unified format mapping.
# CCR uses {"reasoning": {"effort": "<level>"}} instead of
# the OpenAI {"reasoning_effort": "<level>"}.
_REASONING_EFFORT_MAP = {
    "low": {"effort": "low"},
    "medium": {"effort": "medium"},
    "high": {"effort": "high"},
}


class ClaudeCodeClient(AgentClient):
    """Simulates the HTTP requests Claude Code makes via CCR → LiteLLM → Gateway."""

    def __init__(self) -> None:
        super().__init__(extra_headers={"User-Agent": settings.CCR_USER_AGENT})

    # ── Payload builders ──────────────────────────────────────────────────────

    def basic_payload(
        self,
        model: str,
        user_message: str,
        system_prompt: str = "You are a helpful assistant.",
        *,
        max_tokens: int = 256,
        temperature: float = 1.0,
    ) -> dict[str, Any]:
        return {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            **_CCR_EXTRA_FIELDS,
        }

    def reasoning_payload(
        self,
        model: str,
        user_message: str,
        effort: str = "medium",
        *,
        max_tokens: int = 1024,
    ) -> dict[str, Any]:
        """CCR sends reasoning as a nested dict; LiteLLM converts to reasoning_effort."""
        return {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant. Think carefully."},
                {"role": "user", "content": user_message},
            ],
            "max_tokens": max_tokens,
            # CCR unified format — NOT standard OpenAI reasoning_effort
            "reasoning": _REASONING_EFFORT_MAP[effort],
            **_CCR_EXTRA_FIELDS,
        }

    def tool_use_payload(
        self,
        model: str,
        user_message: str,
        tools: list[dict[str, Any]],
        tool_choice: str | dict = "auto",
        *,
        max_tokens: int = 512,
    ) -> dict[str, Any]:
        return {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant with access to tools."},
                {"role": "user", "content": user_message},
            ],
            "tools": tools,
            "tool_choice": tool_choice,
            "max_tokens": max_tokens,
            **_CCR_EXTRA_FIELDS,
        }

    def multi_turn_payload(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 512,
    ) -> dict[str, Any]:
        return {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            **_CCR_EXTRA_FIELDS,
        }
