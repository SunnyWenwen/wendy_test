from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).parent


def load_models() -> dict:
    with open(CONFIG_DIR / "models.yaml") as f:
        return yaml.safe_load(f)


def all_models() -> list[tuple[str, dict]]:
    """Return [(alias, model_cfg), ...] for every model in the registry."""
    registry = load_models()
    result = []
    for _upstream, models in registry.items():
        for alias, cfg in models.items():
            result.append((alias, cfg))
    return result


def models_with_capability(capability: str) -> list[tuple[str, dict]]:
    return [(alias, cfg) for alias, cfg in all_models() if capability in cfg.get("capabilities", [])]
