"""Runtime configuration loaded from environment variables or .env file."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=False)


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Required environment variable '{key}' is not set. Check .env or environment.")
    return val


# ── LLM Gateway ──────────────────────────────────────────────────────────────
GATEWAY_BASE_URL: str = os.getenv("GATEWAY_BASE_URL", "http://testhost")
GATEWAY_API_KEY: str = os.getenv("GATEWAY_API_KEY", "XXXX")
CHAT_COMPLETIONS_PATH: str = "/chat/completions"

# ── Default models used in single-model tests ────────────────────────────────
# These should match model_id values in config/models.yaml.
# Override via environment variables when running against a different deployment.
DEFAULT_BEDROCK_MODEL: str = os.getenv("DEFAULT_BEDROCK_MODEL", "claude-sonnet-4.5")
DEFAULT_VERTEX_MODEL: str = os.getenv("DEFAULT_VERTEX_MODEL", "gemini-3.1-flash-lite-preview")
DEFAULT_REASONING_MODEL: str = os.getenv("DEFAULT_REASONING_MODEL", "claude-sonnet-4.6")

# ── Kubernetes (for log validation) ──────────────────────────────────────────
K8S_NAMESPACE: str = os.getenv("K8S_NAMESPACE", "llm-gateway")
LITELLM_POD_LABEL: str = os.getenv("LITELLM_POD_LABEL", "app=litellm")
AI_PROXY_POD_LABEL: str = os.getenv("AI_PROXY_POD_LABEL", "app=ai-proxy-multi")
KUBECONFIG: str | None = os.getenv("KUBECONFIG")           # None → use in-cluster config
LOG_TAIL_LINES: int = int(os.getenv("LOG_TAIL_LINES", "200"))
LOG_CAPTURE_DELAY: float = float(os.getenv("LOG_CAPTURE_DELAY", "2.0"))  # seconds to wait before fetching logs

# ── Test behaviour ────────────────────────────────────────────────────────────
REQUEST_TIMEOUT: float = float(os.getenv("REQUEST_TIMEOUT", "120.0"))
ENABLE_LOG_VALIDATION: bool = os.getenv("ENABLE_LOG_VALIDATION", "true").lower() == "true"

# ── Agent identity headers (forwarded by CCR / Kilo) ─────────────────────────
CCR_USER_AGENT: str = "claude-code/1.0 CCR/1.0"
KILO_USER_AGENT: str = "Kilo/1.0 OpenAI-Compatible"
