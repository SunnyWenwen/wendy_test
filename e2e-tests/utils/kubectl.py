"""Kubernetes log capture via the official Python client.

Fetches recent logs from the LiteLLM and ai-proxy-multi pods after each
request so tests can assert on what actually hit the upstream.

Usage::

    logs = await KubectlClient().capture_logs(component="litellm", since_seconds=10)
    assert "bedrock" in logs.raw
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Literal

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

from config import settings

Component = Literal["litellm", "ai-proxy"]


@dataclass
class PodLogs:
    component: str
    pod_name: str
    raw: str
    lines: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.lines = self.raw.splitlines()

    # ── Assertions ────────────────────────────────────────────────────────────

    def assert_contains(self, pattern: str, *, regex: bool = False) -> None:
        if regex:
            assert re.search(pattern, self.raw), (
                f"[{self.component}/{self.pod_name}] Pattern /{pattern}/ not found in logs"
            )
        else:
            assert pattern in self.raw, (
                f"[{self.component}/{self.pod_name}] '{pattern}' not found in logs"
            )

    def assert_not_contains(self, pattern: str) -> None:
        assert pattern not in self.raw, (
            f"[{self.component}/{self.pod_name}] Unexpected pattern '{pattern}' found in logs"
        )

    def assert_no_error(self) -> None:
        error_patterns = [r"\bERROR\b", r"\bException\b", r"5\d{2} Internal"]
        for pat in error_patterns:
            matches = re.findall(pat, self.raw, re.IGNORECASE)
            assert not matches, (
                f"[{self.component}/{self.pod_name}] Error pattern '{pat}' found {len(matches)} time(s)"
            )

    def find_model_routing(self, model_id: str) -> bool:
        return model_id in self.raw

    def extract_upstream_request(self) -> str | None:
        """Try to extract the upstream JSON request body from structured log lines."""
        for line in self.lines:
            if "upstream" in line.lower() and "{" in line:
                start = line.index("{")
                return line[start:]
        return None


class KubectlClient:
    """Wraps the Kubernetes Python client to capture pod logs synchronously."""

    def __init__(self) -> None:
        try:
            if settings.KUBECONFIG:
                k8s_config.load_kube_config(config_file=settings.KUBECONFIG)
            else:
                k8s_config.load_kube_config()
        except Exception:
            # Fall back to in-cluster config (when running inside a pod)
            k8s_config.load_incluster_config()
        self._core = k8s_client.CoreV1Api()

    def _get_pod_name(self, label_selector: str) -> str:
        pods = self._core.list_namespaced_pod(
            namespace=settings.K8S_NAMESPACE,
            label_selector=label_selector,
        )
        if not pods.items:
            raise RuntimeError(
                f"No pods found in namespace '{settings.K8S_NAMESPACE}' "
                f"matching label '{label_selector}'"
            )
        # Prefer running pods
        running = [p for p in pods.items if p.status.phase == "Running"]
        target = running[0] if running else pods.items[0]
        return target.metadata.name

    def _fetch_logs(self, pod_name: str, since_seconds: int) -> str:
        return self._core.read_namespaced_pod_log(
            name=pod_name,
            namespace=settings.K8S_NAMESPACE,
            tail_lines=settings.LOG_TAIL_LINES,
            since_seconds=since_seconds,
        )

    async def capture_logs(
        self,
        component: Component = "litellm",
        *,
        since_seconds: int = 30,
        delay: float | None = None,
    ) -> PodLogs:
        """Capture recent logs for the given component asynchronously."""
        await asyncio.sleep(delay if delay is not None else settings.LOG_CAPTURE_DELAY)

        label = (
            settings.LITELLM_POD_LABEL
            if component == "litellm"
            else settings.AI_PROXY_POD_LABEL
        )
        pod_name = await asyncio.to_thread(self._get_pod_name, label)
        raw = await asyncio.to_thread(self._fetch_logs, pod_name, since_seconds)
        return PodLogs(component=component, pod_name=pod_name, raw=raw)

    async def capture_all(self, *, since_seconds: int = 30) -> dict[Component, PodLogs]:
        """Capture logs from both litellm and ai-proxy concurrently."""
        litellm_logs, proxy_logs = await asyncio.gather(
            self.capture_logs("litellm", since_seconds=since_seconds),
            self.capture_logs("ai-proxy", since_seconds=since_seconds),
        )
        return {"litellm": litellm_logs, "ai-proxy": proxy_logs}
