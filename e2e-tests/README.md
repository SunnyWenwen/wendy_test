# LLM Gateway E2E Test Suite

End-to-end integration tests for the self-hosted LLM Gateway stack:

```
Claude Code / Kilo → API6 (ai-proxy-multi) → LiteLLM → AWS Bedrock / Vertex AI
```

## Quick Start

```bash
cd e2e-tests
python3 -m pip install -e ".[dev]"
cp .env.example .env        # fill in GATEWAY_BASE_URL, GATEWAY_API_KEY, etc.
make smoke                  # connectivity checks first
make test                   # full suite (skips slow)
```

## Architecture

```
e2e-tests/
├── agents/
│   ├── base.py             # shared async HTTP client
│   ├── claude_code.py      # CCR format simulator (reasoning dict, system prompt, etc.)
│   └── kilo.py             # Kilo OpenAI-compatible simulator + fixture loader
├── config/
│   ├── models.yaml         # model registry (add new models here)
│   └── settings.py         # env-based config
├── fixtures/
│   ├── kilo_payloads/      # real captured Kilo request bodies (.json)
│   └── cc_payloads/        # real captured CCR request bodies (.json)
├── validators/
│   ├── response.py         # OpenAI response schema assertions
│   └── log_parser.py       # kubectl log capture + assertions
├── utils/
│   └── kubectl.py          # Kubernetes Python client wrapper
└── tests/
    ├── conftest.py          # shared fixtures, auto-parametrization
    ├── test_connectivity.py # gateway reachability
    ├── test_claude_code.py  # CCR-specific behaviour
    ├── test_kilo.py         # Kilo OpenAI-compatible behaviour
    ├── test_models.py       # all models × basic / streaming / tool_use
    ├── test_streaming.py    # SSE streaming
    ├── test_tool_use.py     # function calling round-trip
    └── test_reasoning.py    # extended thinking / reasoning_effort
```

## Adding a New Model

1. Open `config/models.yaml` and add an entry under the correct upstream:

```yaml
bedrock:
  claude-new-model:
    model_id: "anthropic.claude-new-model-id"
    upstream: bedrock
    capabilities: [streaming, tool_use, reasoning, vision, long_ctx]
```

2. Run the model matrix tests:

```bash
make models
```

The new model is automatically picked up by parametrised tests — no test code changes needed.

## Providing Real Agent Payloads

Replace `.json.example` files in `fixtures/` with real captured payloads:

- **Kilo**: Capture outgoing HTTP requests from Kilo using mitmproxy or browser devtools when Kilo uses the OpenAI Compatible provider.
- **CCR**: Capture requests from the CCR pod to LiteLLM via `kubectl logs`.

```bash
# Capture CCR → LiteLLM traffic
kubectl logs -n llm-gateway -l app=litellm --since=1m | grep "request_body"
```

## Key Design Decisions

### CCR Unified Format
Claude Code sends `"reasoning": {"effort": "medium"}` (CCR format) instead of
the OpenAI `"reasoning_effort": "medium"`. LiteLLM translates this before
forwarding to Bedrock. Tests in `test_reasoning.py` and `test_claude_code.py`
verify both that:
1. The gateway accepts the CCR format (no 4xx).
2. LiteLLM logs show `reasoning_effort` was sent to the upstream (log validation).

When new CCR-specific fields are discovered, add them to `agents/claude_code.py`:
```python
_CCR_EXTRA_FIELDS: dict[str, Any] = {
    "new_ccr_field": "value",
}
```

### Log Validation
After each request, the test suite optionally fetches recent pod logs via the
Kubernetes Python client and asserts on:
- Model routing (correct model_id appeared in LiteLLM logs)
- Upstream reached (bedrock/vertex string in logs)
- No ERROR/Exception log lines

Disable log validation for faster local runs:
```bash
ENABLE_LOG_VALIDATION=false make test
# or
make no-logs
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GATEWAY_BASE_URL` | `http://localhost:4000` | LLM Gateway base URL |
| `GATEWAY_API_KEY` | `` | API key for the gateway |
| `K8S_NAMESPACE` | `llm-gateway` | Kubernetes namespace |
| `LITELLM_POD_LABEL` | `app=litellm` | Pod label selector for LiteLLM |
| `AI_PROXY_POD_LABEL` | `app=ai-proxy-multi` | Pod label selector for ai-proxy |
| `KUBECONFIG` | (default) | Path to kubeconfig |
| `ENABLE_LOG_VALIDATION` | `true` | Toggle kubectl log assertions |
| `LOG_TAIL_LINES` | `200` | Lines to tail from pods |
| `LOG_CAPTURE_DELAY` | `2.0` | Seconds to wait before fetching logs |
| `REQUEST_TIMEOUT` | `120.0` | HTTP request timeout (seconds) |

## CI Integration

```yaml
# Example GitHub Actions step
- name: Run LLM Gateway E2E tests
  working-directory: e2e-tests
  env:
    GATEWAY_BASE_URL: ${{ secrets.LLM_GATEWAY_DEV_URL }}
    GATEWAY_API_KEY: ${{ secrets.LLM_GATEWAY_API_KEY }}
    ENABLE_LOG_VALIDATION: "false"   # no k8s access in CI
  run: |
    pip install -e ".[dev]"
    make smoke
    make test
```
