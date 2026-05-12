"""Base HTTP client shared by all agent simulators."""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

import httpx

from config import settings


class AgentClient:
    """Thin async HTTP wrapper around the LLM Gateway /chat/completions endpoint."""

    def __init__(self, extra_headers: dict[str, str] | None = None) -> None:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.GATEWAY_API_KEY}",
        }
        if extra_headers:
            headers.update(extra_headers)
        self._headers = headers
        self._base_url = settings.GATEWAY_BASE_URL

    # ── Non-streaming ─────────────────────────────────────────────────────────

    async def chat(self, payload: dict[str, Any]) -> httpx.Response:
        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=settings.REQUEST_TIMEOUT,
        ) as client:
            return await client.post(
                settings.CHAT_COMPLETIONS_PATH,
                headers=self._headers,
                json=payload,
            )

    # ── Streaming (SSE) ───────────────────────────────────────────────────────

    async def chat_stream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Yield parsed SSE data chunks. Caller is responsible for consuming all chunks."""
        stream_payload = {**payload, "stream": True}
        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=settings.REQUEST_TIMEOUT,
        ) as client:
            async with client.stream(
                "POST",
                settings.CHAT_COMPLETIONS_PATH,
                headers=self._headers,
                json=stream_payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    yield json.loads(data)

    async def collect_stream(self, payload: dict[str, Any]) -> tuple[list[dict], str]:
        """Collect all streaming chunks; return (chunks, assembled_text)."""
        chunks: list[dict] = []
        text_parts: list[str] = []
        async for chunk in self.chat_stream(payload):
            chunks.append(chunk)
            for choice in chunk.get("choices", []):
                delta = choice.get("delta", {})
                if content := delta.get("content"):
                    text_parts.append(content)
        return chunks, "".join(text_parts)
