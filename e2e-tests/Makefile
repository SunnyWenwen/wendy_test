.PHONY: install test smoke cc kilo models streaming tools reasoning logs clean

PYTHON ?= python3
PYTEST  = $(PYTHON) -m pytest

install:
	$(PYTHON) -m pip install -e ".[dev]"

# ── Test targets ──────────────────────────────────────────────────────────────

# Full suite (skip slow)
test:
	$(PYTEST) tests/ -v --ignore=tests/test_models.py -m "not slow"

# Connectivity smoke tests only (run these first)
smoke:
	$(PYTEST) tests/test_connectivity.py -m connectivity -v

# Claude Code / CCR tests
cc:
	$(PYTEST) tests/test_claude_code.py -m claude_code -v

# Kilo tests
kilo:
	$(PYTEST) tests/test_kilo.py -m kilo -v

# All models matrix
models:
	$(PYTEST) tests/test_models.py -v

# Only Bedrock models
bedrock:
	$(PYTEST) tests/test_models.py -m bedrock -v

# Only Vertex models
vertex:
	$(PYTEST) tests/test_models.py -m vertex -v

# Streaming tests
streaming:
	$(PYTEST) tests/test_streaming.py -m streaming -v

# Tool use tests
tools:
	$(PYTEST) tests/test_tool_use.py -m tool_use -v

# Reasoning tests
reasoning:
	$(PYTEST) tests/test_reasoning.py -m reasoning -v

# All reasoning-capable models (slow)
reasoning-all:
	$(PYTEST) tests/test_reasoning.py -m "reasoning and slow" -v

# Disable log validation (faster, no k8s access required)
no-logs:
	ENABLE_LOG_VALIDATION=false $(PYTEST) tests/ -v -m "not slow"

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache
