"""
Configuration entry point for sndbx.

The aiset runtime owns parsing, validation, overlays and persistence. This
module keeps the public ``load_config`` shape used by the application and tests.
"""

from __future__ import annotations

import os
from typing import Any, Dict


class ConfigError(Exception):
    """Configuration loading error."""


def load_env_file(path: str) -> Dict[str, str]:
    """Load a simple KEY=VALUE .env file into a dictionary."""
    env_vars: Dict[str, str] = {}
    if not os.path.exists(path):
        return env_vars
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env_vars[key.strip()] = value.strip()
    return env_vars


def load_config(root_dir: str = ".") -> Dict[str, Any]:
    """Load the root configuration and every per-image overlay.

    Returns ``{"env": ..., "config": ..., "settings": runtime}``. The returned
    config is the merged runtime view: the root wins every conflict, overlays
    supply keys the root does not define.
    """
    from settings.runtime import get_runtime

    runtime = get_runtime(root_dir)
    return {
        "env": runtime.env,
        "config": runtime.config,
        "settings": runtime,
    }
