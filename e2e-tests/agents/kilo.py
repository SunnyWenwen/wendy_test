"""Kilo agent simulator.

Kilo connects to the LLM Gateway using the "OpenAI Compatible" provider
setting, meaning it sends standard OpenAI /chat/completions payloads with
no intermediate translation layer (unlike Claude Code which goes through CCR).

Place real captured Kilo payloads under fixtures/kilo_payloads/ and load
them via load_fixture_payload() to keep simulator in sync with reality.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from config import settings
from agents.base import AgentClient

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "kilo_payloads"


def load_fixture_payload(name: str) -> dict[str, Any]:
    """Load a captured Kilo payload from fixtures/kilo_payloads/<name>.json."""
    path = _FIXTURES_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Kilo fixture not found: {path}")
    return json.loads(path.read_text())


class KiloClient(AgentClient):
    """Simulates the HTTP requests Kilo makes using its OpenAI Compatible provider."""

    def __init__(self) -> None:
        super().__init__(extra_headers={"User-Agent": settings.KILO_USER_AGENT})

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

    def streaming_payload(
        self,
        model: str,
        user_message: str,
        *,
        max_tokens: int = 256,
    ) -> dict[str, Any]:
        return {
            "model": model,
            "messages": [{"role": "user", "content": user_message}],
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

    def tool_use_payload(
        self,
        model: str,
        user_message: str,
        tools: list[dict[str, Any]],
        *,
        max_tokens: int = 512,
    ) -> dict[str, Any]:
        return {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant with tools."},
                {"role": "user", "content": user_message},
            ],
            "tools": tools,
            "tool_choice": "auto",
            "max_tokens": max_tokens,
        }

    def reasoning_payload(
        self,
        model: str,
        user_message: str,
        reasoning_effort: str = "medium",
        *,
        max_tokens: int = 1024,
    ) -> dict[str, Any]:
        """Standard OpenAI reasoning_effort field (Kilo does not use CCR unified format)."""
        return {
            "model": model,
            "messages": [{"role": "user", "content": user_message}],
            "reasoning_effort": reasoning_effort,
            "max_tokens": max_tokens,
        }

    def from_fixture(self, name: str, *, override_model: str | None = None) -> dict[str, Any]:
        """Build payload from a captured fixture file, optionally overriding model."""
        payload = load_fixture_payload(name)
        if override_model:
            payload["model"] = override_model
        return payload
