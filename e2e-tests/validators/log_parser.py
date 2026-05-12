"""Higher-level log assertions that combine kubectl capture + domain knowledge."""

from __future__ import annotations

import re
from typing import Any

from utils.kubectl import KubectlClient, PodLogs


class LogValidator:
    """Runs after an API call to assert on LiteLLM / ai-proxy log output."""

    def __init__(self, kubectl: KubectlClient) -> None:
        self._kubectl = kubectl

    async def after_request(self, *, since_seconds: int = 30) -> dict[str, PodLogs]:
        return await self._kubectl.capture_all(since_seconds=since_seconds)

    # ── Model routing assertions ──────────────────────────────────────────────

    @staticmethod
    def assert_model_routed(logs: dict[str, PodLogs], model_id: str) -> None:
        """Assert the expected model_id appears somewhere in the LiteLLM logs."""
        litellm_log = logs["litellm"]
        assert litellm_log.find_model_routing(model_id), (
            f"Model '{model_id}' not found in LiteLLM logs. "
            f"Routing may have failed or model alias is wrong."
        )

    @staticmethod
    def assert_upstream_reached(logs: dict[str, PodLogs], upstream: str) -> None:
        """Assert the upstream provider name appears in ai-proxy or LiteLLM logs."""
        combined = logs["litellm"].raw + logs["ai-proxy"].raw
        assert upstream.lower() in combined.lower(), (
            f"Upstream '{upstream}' not found in any pod logs."
        )

    # ── CCR-specific assertions ───────────────────────────────────────────────

    @staticmethod
    def assert_reasoning_translated(logs: dict[str, PodLogs]) -> None:
        """Assert LiteLLM translated CCR 'reasoning' field to 'reasoning_effort'."""
        litellm_log = logs["litellm"]
        # After translation, the upstream request should contain reasoning_effort
        # and NOT the raw CCR 'reasoning' dict format
        litellm_log.assert_contains("reasoning_effort")

    # ── Error absence ─────────────────────────────────────────────────────────

    @staticmethod
    def assert_no_errors(logs: dict[str, PodLogs]) -> None:
        for component, pod_log in logs.items():
            pod_log.assert_no_error()

    # ── Usage / token tracking ────────────────────────────────────────────────

    @staticmethod
    def extract_token_usage(logs: dict[str, PodLogs]) -> dict[str, int] | None:
        pattern = r"prompt_tokens[\":\s]+(\d+).*?completion_tokens[\":\s]+(\d+)"
        match = re.search(pattern, logs["litellm"].raw, re.DOTALL)
        if match:
            return {
                "prompt_tokens": int(match.group(1)),
                "completion_tokens": int(match.group(2)),
            }
        return None
