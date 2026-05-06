#!/usr/bin/env python3
"""Image lifecycle hooks for mcp_toolbox_1.

Starts sshd and lightweight TCP bridges for stdio-based MCP servers.
"""

import json
import os
import subprocess
import sys
from pathlib import Path


def _load_context() -> dict:
    """Parse SNDBX_CONTEXT_JSON and return dict context."""
    raw = os.getenv("SNDBX_CONTEXT_JSON", "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _ensure_sshd() -> None:
    """Configure and start key-only sshd for root user."""
    cfg_path = Path("/etc/ssh/sshd_config")
    text = cfg_path.read_text(encoding="utf-8", errors="ignore")

    replacements = [
        ("#PermitRootLogin prohibit-password", "PermitRootLogin prohibit-password"),
        ("PermitRootLogin yes", "PermitRootLogin prohibit-password"),
        ("#PasswordAuthentication yes", "PasswordAuthentication no"),
        ("PasswordAuthentication yes", "PasswordAuthentication no"),
    ]
    for old, new in replacements:
        text = text.replace(old, new)

    if "PermitRootLogin" not in text:
        text += "\nPermitRootLogin prohibit-password\n"
    if "PasswordAuthentication" not in text:
        text += "\nPasswordAuthentication no\n"

    cfg_path.write_text(text, encoding="utf-8")
    Path("/run/sshd").mkdir(parents=True, exist_ok=True)
    subprocess.run(["pkill", "-x", "sshd"], check=False)
    subprocess.run(["/usr/sbin/sshd"], check=True)


def _is_port_listening(port: int) -> bool:
    """Return True when TCP port is listening in the VM."""
    result = subprocess.run(["ss", "-ltn"], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        return False
    needle = f":{port} "
    return needle in result.stdout


def _start_stdio_tcp_bridge(listen_port: int, backend_cmd: str, tag: str) -> None:
    """Start socat TCP listener that spawns stdio backend per client.

    input: listen port, backend shell command, process tag
    output: starts background process if absent
    """
    marker = f"sndbx-mcp-{tag}-{listen_port}"
    if _is_port_listening(listen_port):
        return

    log_path = Path("/tmp") / f"{marker}.log"
    socat_cmd = [
        "socat",
        f"TCP-LISTEN:{listen_port},fork,reuseaddr",
        f"SYSTEM:{backend_cmd},stderr",
    ]
    with log_path.open("ab") as logf:
        subprocess.Popen(socat_cmd, stdout=logf, stderr=logf)


def _start_default_mcp_backends() -> None:
    """Start default MCP backend bridges inside the VM.

    Ports:
    - 9011: filesystem server
    - 9012: bash shell server
    - 9013: git server
    """
    Path("/root/shared").mkdir(parents=True, exist_ok=True)

    _start_stdio_tcp_bridge(
        9011,
        "mcp-server-filesystem /root/shared",
        "filesystem",
    )

    _start_stdio_tcp_bridge(
        9012,
        "npx -y mcp-shell-server",
        "bash",
    )

    _start_stdio_tcp_bridge(
        9013,
        "mcp-server-git --repository /root/shared",
        "git",
    )


def on_system_start() -> int:
    """Initialize runtime services for the image."""
    _load_context()  # Reserved for future context-driven behavior.
    _ensure_sshd()
    _start_default_mcp_backends()
    return 0


def main() -> int:
    """Dispatch by SNDBX_HOOK env var."""
    hook = os.getenv("SNDBX_HOOK", "on_system_start").strip() or "on_system_start"
    if hook == "on_system_start":
        return on_system_start()
    print(f"unknown hook: {hook!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
