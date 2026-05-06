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


def _parse_ssh_keys_from_json(json_path: Path) -> list[str]:
    """Parse SSH public keys from JSON sync file on shared mount.

    input: path to JSON file with keys
    output: list of unique, non-empty key strings; empty list if file missing or invalid
    """
    if not json_path.exists():
        return []
    
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        keys = data.get("keys", [])
        if not isinstance(keys, list):
            return []
        return [k.strip() for k in keys if isinstance(k, str) and k.strip()]
    except (json.JSONDecodeError, OSError):
        return []


def _sync_ssh_keys_from_share() -> bool:
    """Sync SSH authorized_keys from shared JSON file (called by systemd timer).

    Reads keys from /root/shared/.ssh-sync.json, merges with existing, writes to authorized_keys.
    input: none
    output: True if sync succeeded, False otherwise
    """
    json_path = Path("/root/shared/.ssh-sync.json")
    keys_path = Path("/root/.ssh/authorized_keys")
    
    # Read keys from JSON on shared mount
    new_keys = _parse_ssh_keys_from_json(json_path)
    
    if not new_keys:
        # No update available, keep existing
        return True
    
    # Parse existing keys to avoid duplicates when appending
    existing_keys = []
    if keys_path.exists():
        existing = keys_path.read_text(encoding="utf-8")
        existing_keys = [k.strip() for k in existing.split("\n") if k.strip()]
    
    # Merge: new keys take priority (replace mode), or append
    # For now: replace mode (overwrite with new_keys)
    _write_authorized_keys(keys_path, new_keys)
    print(f"_sync_ssh_keys_from_share: synced {len(new_keys)} key(s) from {json_path}")
    return True


def _install_ssh_sync_service() -> bool:
    """Install systemd service and timer for periodic SSH key sync from shared mount.

    input: none
    output: True if installation succeeded
    """
    sync_script_path = Path("/root/.ssh/sync-from-share.sh")
    service_path = Path("/etc/systemd/system/sndbx-ssh-sync.service")
    timer_path = Path("/etc/systemd/system/sndbx-ssh-sync.timer")
    
    # Create sync script
    sync_script = """#!/bin/bash
set -e
# Sync SSH keys from shared JSON file
/root/.ssh/sync-from-share.py
"""
    sync_script_path.write_text(sync_script, encoding="utf-8")
    os.chmod(sync_script_path, 0o755)
    
    # Create sync Python helper (simpler than wrapping the function)
    sync_helper = '''#!/usr/bin/env python3
import json
import sys
from pathlib import Path

def _parse_ssh_keys_from_json(json_path: Path) -> list[str]:
    """Parse SSH public keys from JSON sync file on shared mount."""
    if not json_path.exists():
        return []
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        keys = data.get("keys", [])
        if not isinstance(keys, list):
            return []
        return [k.strip() for k in keys if isinstance(k, str) and k.strip()]
    except (json.JSONDecodeError, OSError):
        return []

def _write_authorized_keys(path: Path, keys: list[str]) -> None:
    """Write unique, normalized keys to authorized_keys; set correct permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    unique: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            unique.append(k)
    content = "\\n".join(unique) + ("\\n" if unique else "")
    path.write_text(content, encoding="utf-8")
    import os
    os.chmod(path.parent, 0o700)
    os.chmod(path, 0o600)
    import subprocess
    subprocess.run(["chown", "root:root", str(path.parent)], check=True)
    subprocess.run(["chown", "root:root", str(path)], check=True)

json_path = Path("/root/shared/.ssh-sync.json")
keys_path = Path("/root/.ssh/authorized_keys")
new_keys = _parse_ssh_keys_from_json(json_path)

if new_keys:
    _write_authorized_keys(keys_path, new_keys)
    print(f"SSH keys synced: {len(new_keys)} key(s)")
    sys.exit(0)
else:
    sys.exit(0)
'''
    sync_helper_path = Path("/root/.ssh/sync-from-share.py")
    sync_helper_path.write_text(sync_helper, encoding="utf-8")
    os.chmod(sync_helper_path, 0o755)
    
    # Create systemd service
    service_content = """[Unit]
Description=Sync SSH authorized_keys from shared JSON
After=network.target

[Service]
Type=oneshot
ExecStart=/root/.ssh/sync-from-share.py
User=root
StandardOutput=journal
StandardError=journal
"""
    service_path.write_text(service_content, encoding="utf-8")
    
    # Create systemd timer (runs every 30 seconds after boot)
    timer_content = """[Unit]
Description=Periodic SSH key sync timer
Requires=sndbx-ssh-sync.service

[Timer]
OnBootSec=5s
OnUnitActiveSec=30s
Persistent=true

[Install]
WantedBy=timers.target
"""
    timer_path.write_text(timer_content, encoding="utf-8")
    
    try:
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", "sndbx-ssh-sync.timer"], check=True)
        subprocess.run(["systemctl", "start", "sndbx-ssh-sync.timer"], check=True)
        print("SSH sync systemd timer installed and started")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Failed to install SSH sync timer: {e}", file=sys.stderr)
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Hooks
# ──────────────────────────────────────────────────────────────────────────────

def on_system_start(ctx: SandboxContext) -> int:
    """Set up SSH authorized_keys and start sshd when a sandbox container starts.

    Keys come from ctx.ssh_keys() (sandbox config). If the config has no keys,
    the existing /root/.ssh/authorized_keys content is preserved unchanged.
    
    Installs systemd timer for periodic SSH key sync from shared JSON file.

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
    _install_ssh_sync_service()
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
