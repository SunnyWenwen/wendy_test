"""Claude Code agent simulator.

Claude Code sends requests to CCR (Claude Code Router) in Anthropic API format
(/v1/messages).  CCR's AnthropicTransformer converts these to a *unified*
internal format (UnifiedChatRequest) before forwarding to LiteLLM.

Key differences from vanilla OpenAI Chat Completions format:

  reasoning field
    CCR sends:  "reasoning": {"effort": "low"|"medium"|"high", "enabled": true}
    OpenAI:     "reasoning_effort": "low"|"medium"|"high"
    → LiteLLM translates CCR format to reasoning_effort before sending upstream.

    budget_tokens → effort mapping (from CCR source getThinkLevel()):
      ≤ 0     → "none"
      ≤ 1024  → "low"
      ≤ 8192  → "medium"
      > 8192  → "high"

  thinking in assistant messages (multi-turn only)
    CCR sends:  messages[n].thinking = {"content": "...", "signature": "..."}
    OpenAI:     no equivalent
    → Preserved when Claude Code replays a previous thinking-enabled turn.

  cache_control on messages (prompt caching)
    CCR sends:  messages[n].cache_control = {"type": "ephemeral"}
    OpenAI:     no equivalent (Anthropic-specific)

  Fields CCR does NOT forward (not in UnifiedChatRequest):
    frequency_penalty, presence_penalty, logprobs, top_logprobs,
    n, stop, seed, response_format, user, service_tier, stream_options

Source: github.com/musistudio/claude-code-router
  packages/core/src/types/llm.ts          — UnifiedChatRequest
  packages/core/src/transformer/anthropic.transformer.ts
  packages/core/src/utils/thinking.ts     — getThinkLevel()
"""

from __future__ import annotations

from typing import Any

from config import settings
from agents.base import AgentClient

# Reasoning level → CCR unified format.
# CCR sends {"reasoning": {"effort": "...", "enabled": true}} to LiteLLM.
# LiteLLM then converts this to {"reasoning_effort": "..."} for the upstream.
_REASONING_MAP: dict[str, dict[str, Any]] = {
    "none":   {"effort": "none",   "enabled": False},
    "low":    {"effort": "low",    "enabled": True},
    "medium": {"effort": "medium", "enabled": True},
    "high":   {"effort": "high",   "enabled": True},
}


class ClaudeCodeClient(AgentClient):
    """Simulates HTTP requests that CCR sends to LiteLLM (UnifiedChatRequest format)."""

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
        }

    def reasoning_payload(
        self,
        model: str,
        user_message: str,
        effort: str = "medium",
        *,
        max_tokens: int = 1024,
    ) -> dict[str, Any]:
        """CCR unified format: 'reasoning' dict, NOT OpenAI 'reasoning_effort' string.

        LiteLLM receives this and translates to reasoning_effort before upstream call.
        """
        return {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant. Think carefully."},
                {"role": "user", "content": user_message},
            ],
            "max_tokens": max_tokens,
            "reasoning": _REASONING_MAP[effort],
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
        }

    def multi_turn_with_thinking_payload(
        self,
        model: str,
        user_message: str,
        prior_thinking_content: str,
        prior_thinking_signature: str,
        prior_assistant_text: str,
        *,
        effort: str = "medium",
        max_tokens: int = 512,
    ) -> dict[str, Any]:
        """Multi-turn where a previous assistant turn had thinking content.

        CCR passes thinking back in the assistant message as:
          messages[n].thinking = {"content": "...", "signature": "..."}
        This is a CCR-specific field that has no OpenAI equivalent.
        """
        return {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant. Think carefully."},
                {"role": "user", "content": "First question: what is 5 + 3?"},
                {
                    "role": "assistant",
                    "content": prior_assistant_text,
                    "thinking": {
                        "content": prior_thinking_content,
                        "signature": prior_thinking_signature,
                    },
                },
                {"role": "user", "content": user_message},
            ],
            "max_tokens": max_tokens,
            "reasoning": _REASONING_MAP[effort],
        }

    def cached_messages_payload(
        self,
        model: str,
        long_system_prompt: str,
        user_message: str,
        *,
        max_tokens: int = 256,
    ) -> dict[str, Any]:
        """Includes cache_control on the system message (Anthropic prompt caching).

        CCR preserves cache_control from the original Anthropic request.
        LiteLLM may or may not forward this depending on the upstream provider.
        """
        return {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": long_system_prompt,
                    "cache_control": {"type": "ephemeral"},
                },
                {"role": "user", "content": user_message},
            ],
            "max_tokens": max_tokens,
        }
