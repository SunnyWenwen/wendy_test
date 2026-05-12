"""Shared pytest fixtures for the LLM Gateway E2E test suite."""

from __future__ import annotations

import pytest

from agents.claude_code import ClaudeCodeClient
from agents.kilo import KiloClient
from config import all_models, models_with_capability
from config import settings
from utils.kubectl import KubectlClient
from validators.log_parser import LogValidator
from validators.response import ResponseValidator


# ── Agent clients ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def cc_client() -> ClaudeCodeClient:
    return ClaudeCodeClient()


@pytest.fixture(scope="session")
def kilo_client() -> KiloClient:
    return KiloClient()


# ── Kubernetes / log tooling ──────────────────────────────────────────────────

@pytest.fixture(scope="session")
def kubectl() -> KubectlClient | None:
    """Returns a KubectlClient if log validation is enabled, else None."""
    if not settings.ENABLE_LOG_VALIDATION:
        return None
    try:
        return KubectlClient()
    except Exception as exc:
        pytest.skip(f"kubectl unavailable (ENABLE_LOG_VALIDATION=true but k8s unreachable): {exc}")


@pytest.fixture(scope="session")
def log_validator(kubectl: KubectlClient | None) -> LogValidator | None:
    if kubectl is None:
        return None
    return LogValidator(kubectl)


# ── Response validator ────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def rv() -> ResponseValidator:
    return ResponseValidator()


# ── Model parametrization helpers ─────────────────────────────────────────────

def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Auto-parametrize fixtures that declare a model dependency."""
    if "all_model" in metafunc.fixturenames:
        params = [(alias, cfg) for alias, cfg in all_models()]
        metafunc.parametrize(
            "all_model",
            params,
            ids=[alias for alias, _ in params],
        )

    if "bedrock_model" in metafunc.fixturenames:
        from config import load_models
        registry = load_models()
        params = list(registry.get("bedrock", {}).items())
        metafunc.parametrize(
            "bedrock_model",
            params,
            ids=[alias for alias, _ in params],
        )

    if "vertex_model" in metafunc.fixturenames:
        from config import load_models
        registry = load_models()
        params = list(registry.get("vertex", {}).items())
        metafunc.parametrize(
            "vertex_model",
            params,
            ids=[alias for alias, _ in params],
        )

    if "reasoning_model" in metafunc.fixturenames:
        params = models_with_capability("reasoning")
        metafunc.parametrize(
            "reasoning_model",
            params,
            ids=[alias for alias, _ in params],
        )

    if "tool_use_model" in metafunc.fixturenames:
        params = models_with_capability("tool_use")
        metafunc.parametrize(
            "tool_use_model",
            params,
            ids=[alias for alias, _ in params],
        )

    if "streaming_model" in metafunc.fixturenames:
        params = models_with_capability("streaming")
        metafunc.parametrize(
            "streaming_model",
            params,
            ids=[alias for alias, _ in params],
        )


# ── Shared tool definitions ───────────────────────────────────────────────────

@pytest.fixture(scope="session")
def weather_tool() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get current weather for a city.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string", "description": "City name"},
                        "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
                    },
                    "required": ["city"],
                },
            },
        }
    ]
