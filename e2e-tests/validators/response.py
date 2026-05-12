"""Validates /chat/completions HTTP responses against OpenAI schema."""

from __future__ import annotations

from typing import Any

import jsonschema

# Minimal OpenAI chat completion response schema.
# Extend as needed — focus on fields the gateway must always return.
_CHAT_COMPLETION_SCHEMA = {
    "type": "object",
    "required": ["id", "object", "choices", "model"],
    "properties": {
        "id": {"type": "string"},
        "object": {"type": "string", "enum": ["chat.completion"]},
        "created": {"type": "integer"},
        "model": {"type": "string"},
        "choices": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["index", "message", "finish_reason"],
                "properties": {
                    "index": {"type": "integer"},
                    "message": {
                        "type": "object",
                        "required": ["role", "content"],
                        "properties": {
                            "role": {"type": "string", "enum": ["assistant"]},
                            "content": {},  # string or null (tool_calls case)
                        },
                    },
                    "finish_reason": {"type": ["string", "null"]},
                },
            },
        },
        "usage": {
            "type": "object",
            "properties": {
                "prompt_tokens": {"type": "integer"},
                "completion_tokens": {"type": "integer"},
                "total_tokens": {"type": "integer"},
            },
        },
    },
}

_STREAMING_CHUNK_SCHEMA = {
    "type": "object",
    "required": ["id", "object", "choices"],
    "properties": {
        "id": {"type": "string"},
        "object": {"type": "string", "enum": ["chat.completion.chunk"]},
        "choices": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["index", "delta"],
                "properties": {
                    "index": {"type": "integer"},
                    "delta": {"type": "object"},
                    "finish_reason": {"type": ["string", "null"]},
                },
            },
        },
    },
}


class ResponseValidator:
    @staticmethod
    def assert_ok(response: Any) -> None:
        """Assert HTTP 200 and valid JSON."""
        assert response.status_code == 200, (
            f"Expected HTTP 200, got {response.status_code}. Body: {response.text[:500]}"
        )

    @staticmethod
    def assert_schema(data: dict) -> None:
        try:
            jsonschema.validate(data, _CHAT_COMPLETION_SCHEMA)
        except jsonschema.ValidationError as exc:
            raise AssertionError(f"Response schema validation failed: {exc.message}") from exc

    @staticmethod
    def assert_has_content(data: dict) -> str:
        content = data["choices"][0]["message"]["content"]
        assert content, "Response message content is empty"
        return content

    @staticmethod
    def assert_finish_reason(data: dict, expected: str = "stop") -> None:
        reason = data["choices"][0].get("finish_reason")
        assert reason == expected, f"Expected finish_reason='{expected}', got '{reason}'"

    @staticmethod
    def assert_tool_calls(data: dict) -> list[dict]:
        calls = data["choices"][0]["message"].get("tool_calls", [])
        assert calls, "Expected at least one tool_call in response"
        return calls

    @staticmethod
    def assert_streaming_chunk(chunk: dict) -> None:
        try:
            jsonschema.validate(chunk, _STREAMING_CHUNK_SCHEMA)
        except jsonschema.ValidationError as exc:
            raise AssertionError(f"Streaming chunk schema invalid: {exc.message}") from exc

    @staticmethod
    def assert_stream_assembled(text: str) -> None:
        assert text.strip(), "Assembled streaming response text is empty"

    @classmethod
    def full_check(cls, response: Any) -> dict:
        """Run all non-streaming assertions; return parsed JSON body."""
        cls.assert_ok(response)
        data = response.json()
        cls.assert_schema(data)
        cls.assert_has_content(data)
        return data
