"""Locate the Kata runtime registered with the local Docker daemon.

The runtime path is configurable: ``install_prerequisites.sh`` accepts
``--kata_path``, so the shim is not always below ``/opt/kata``. The Docker
daemon configuration is the authoritative source; the historical paths are
kept as fallbacks.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

DOCKER_DAEMON_CONFIG = Path("/etc/docker/daemon.json")
DEFAULT_SHIM = Path("/opt/kata/runtime-rs/bin/containerd-shim-kata-v2")
DEFAULT_CONFIG = Path("/etc/kata-containers/runtime-rs/configuration.toml")
DEFAULT_CONFIG_TEMPLATE = Path(
    "/opt/kata/share/defaults/kata-containers/runtime-rs/configuration-qemu-runtime-rs.toml"
)
RUNTIME_NAME = "kata"


def _registered_runtime() -> Dict[str, str]:
    """Return the registered 'kata' runtime entry from daemon.json."""
    try:
        raw = DOCKER_DAEMON_CONFIG.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        config = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(config, dict):
        return {}
    runtimes = config.get("runtimes")
    if not isinstance(runtimes, dict):
        return {}
    entry = runtimes.get(RUNTIME_NAME)
    if not isinstance(entry, dict):
        return {}
    runtime_type = entry.get("runtimeType")
    options = entry.get("options")
    config_path = options.get("ConfigPath") if isinstance(options, dict) else None
    result = {}
    if isinstance(runtime_type, str) and runtime_type.strip():
        result["runtimeType"] = runtime_type.strip()
    if isinstance(config_path, str) and config_path.strip():
        result["ConfigPath"] = config_path.strip()
    return result


def kata_runtime_paths() -> Dict[str, Path]:
    """Return the shim, configuration and default-template paths for Kata.

    Output: mapping with 'shim', 'config' and 'default_config' paths.
    Input: the local Docker daemon configuration, read from disk.
    """
    registered = _registered_runtime()
    shim = Path(registered.get("runtimeType") or DEFAULT_SHIM)
    config = Path(registered.get("ConfigPath") or DEFAULT_CONFIG)
    default_config = DEFAULT_CONFIG_TEMPLATE
    if shim != DEFAULT_SHIM:
        default_config = (
            shim.parent.parent.parent
            / "share"
            / "defaults"
            / "kata-containers"
            / "runtime-rs"
            / "configuration-qemu-runtime-rs.toml"
        )
    return {
        "shim": shim,
        "config": config,
        "default_config": default_config,
    }
