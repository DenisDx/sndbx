#!/usr/bin/env python3
"""Image lifecycle hooks for default_sandbox_1.

Called by sndbx core via docker exec. Entry point: main(), dispatches to hook
functions based on SNDBX_HOOK env var. All hooks receive a SandboxContext built
from SNDBX_CONTEXT_JSON passed by the core.
"""

import json
import os
import subprocess
import sys
from pathlib import Path


class SandboxContext:
    """Wrapper around the sandbox config dict passed by the sndbx core.

    input: parsed SNDBX_CONTEXT_JSON dict
    output: object with typed accessors
    """

    def __init__(self, data: dict):
        self._data = data
        self._cfg: dict = data.get("sandbox_cfg", {})

    @property
    def sandbox_id(self) -> str:
        """Sandbox identifier string."""
        return str(self._data.get("sandbox_id", ""))

    def ssh_keys(self) -> list[str]:
        """SSH public keys from sandbox config (ssh_keys list).

        output: non-empty key lines exactly as stored in config
        """
        return [
            k.strip()
            for k in self._cfg.get("ssh_keys", [])
            if isinstance(k, str) and k.strip()
        ]

    def shared_directories(self) -> list[dict]:
        """Shared directory mappings from sandbox config.

        output: list of {host_path, guest_path, permission} dicts
        """
        rows = self._cfg.get("shared_directories", [])
        return [r for r in rows if isinstance(r, dict)]

    def get(self, key: str, default=None):
        """Generic accessor for any sandbox_cfg field."""
        return self._cfg.get(key, default)


def _load_context() -> SandboxContext:
    """Parse SNDBX_CONTEXT_JSON from environment and return a SandboxContext.

    Exits with error if the variable is absent or unparseable.
    """
    raw = os.getenv("SNDBX_CONTEXT_JSON", "")
    if not raw:
        print("SNDBX_CONTEXT_JSON is not set — cannot run hook", file=sys.stderr)
        raise SystemExit(2)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"SNDBX_CONTEXT_JSON is not valid JSON: {exc}", file=sys.stderr)
        raise SystemExit(2)
    return SandboxContext(data)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _write_authorized_keys(path: Path, keys: list[str]) -> None:
    """Write unique, normalized keys to authorized_keys; set correct permissions.

    input: target path and list of public key strings
    output: writes file, sets chmod 700 parent / 600 file, owned by root:root
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    unique: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            unique.append(k)
    content = "\n".join(unique) + ("\n" if unique else "")
    path.write_text(content, encoding="utf-8")
    os.chmod(path.parent, 0o700)
    os.chmod(path, 0o600)

    # SSH daemon requires root ownership for authorized_keys when accepting root login.
    subprocess.run(["chown", "root:root", str(path.parent)], check=True)
    subprocess.run(["chown", "root:root", str(path)], check=True)


def _configure_sshd() -> None:
    """Set key-only root login in sshd_config and (re)start sshd.

    input: none
    output: updates /etc/ssh/sshd_config, starts daemon
    """
    cfg_path = Path("/etc/ssh/sshd_config")
    text = cfg_path.read_text(encoding="utf-8", errors="ignore")

    replacements = [
        ("#PermitRootLogin prohibit-password", "PermitRootLogin prohibit-password"),
        ("PermitRootLogin yes",                "PermitRootLogin prohibit-password"),
        ("#PasswordAuthentication yes",        "PasswordAuthentication no"),
        ("PasswordAuthentication yes",         "PasswordAuthentication no"),
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


# ──────────────────────────────────────────────────────────────────────────────
# Hooks
# ──────────────────────────────────────────────────────────────────────────────

def on_system_start(ctx: SandboxContext) -> int:
    """Set up SSH authorized_keys and start sshd when a sandbox container starts.

    Keys come from ctx.ssh_keys() (sandbox config). If the config has no keys,
    the existing /root/.ssh/authorized_keys content is preserved unchanged.

    input: SandboxContext with sandbox config provided by sndbx core
    output: process exit code (0 = success)
    """
    target = Path("/root/.ssh/authorized_keys")

    keys = ctx.ssh_keys()
    if not keys:
        # No keys in config — keep whatever is already in the file (e.g. mounted from host).
        print(f"on_system_start: no ssh_keys in config for '{ctx.sandbox_id}', keeping existing file")
    else:
        _write_authorized_keys(target, keys)
        print(f"on_system_start: wrote {len(keys)} key(s) for '{ctx.sandbox_id}'")

    _configure_sshd()
    return 0


# ──────────────────────────────────────────────────────────────────────────────
# Dispatcher
# ──────────────────────────────────────────────────────────────────────────────

def main() -> int:
    """Load context and dispatch to the requested hook."""
    ctx = _load_context()
    hook = os.getenv("SNDBX_HOOK", "on_system_start").strip() or "on_system_start"

    if hook == "on_system_start":
        return on_system_start(ctx)

    print(f"unknown hook: {hook!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
