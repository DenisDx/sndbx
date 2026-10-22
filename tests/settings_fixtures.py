"""Shared temporary-root fixtures for settings tests."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ROOT_CONFIG = """{
  version: "1.0",
  title: "test sndbx",
  instance: "${TEST_INSTANCE:-dev}",
  root: "${SNDBX_ROOT}",
  logging: {
    level: "${LOG_LEVEL:-info}",
    timezone: "UTC",
    levels: { core: "info" },
    log_mcp_requests: false,
    wipe_period: 0,
  },
  webui: {
    port: 30080,
    bind: "127.0.0.1",
    auth: {
      mode: "login",
      session_ttl: 86400,
      users: [
        { login: "admin", password: "${ADMIN_PASSWORD:-secret}", permissions: ["all"] },
      ],
    },
  },
  mcp: {
    port: 30081,
    bind: "127.0.0.1",
    auth: { mode: "token", tokens: ["${MCP_TOKENS:-token}"] },
    items: [],
  },
  sandboxes: {
    items: {
      demo: {
        title: "Root demo",
        image: "demo:latest",
        memory: "1G",
        cpus: 1,
        disk_max: "10G",
        network_traffic_max: "1G",
        persistent: true,
        run_at_startup: false,
        ssh_keys: ["${TEST_SSH_KEY:-}"],
        shared_directories: [
          { host_path: "${SNDBX_ROOT}/shared", guest_path: "/mnt", permission: "rw" },
        ],
        runtime_contract: {
          version: 1,
          environment: { DEMO_VALUE: "${DEMO_VALUE:-x}" },
        },
        backup: { enabled: false, interval_hours: 24, full_every: 7 },
      },
    },
  },
  envids: {
    "demo-token": { sandbox: "demo", description: "Demo environment" },
  },
  users: {
    items: [
      { login: "u", password: "p", token: "t", envids: ["demo-token"] },
    ],
  },
  ssh: { port_range: [30200, 30210], session_ttl: 3600 },
}
"""

OVERLAY_CONFIG = """{
  sandboxes: {
    items: {
      demo: {
        title: "Overlay demo",
        memory: "2G",
        configure_apt_mirror: true,
        shared_directories: [
          { host_path: "/overlay", guest_path: "/mnt", permission: "ro" },
        ],
      },
    },
  },
}
"""


class TempRoot:
    """Create a temporary sndbx root and clean it up."""

    def __init__(self) -> None:
        self.path = Path(tempfile.mkdtemp(prefix="sndbx-settings-"))

    def __enter__(self) -> Path:
        shutil.copy2(ROOT / "config.descriptor.json5", self.path / "config.descriptor.json5")
        shutil.copy2(ROOT / "config.types.json5", self.path / "config.types.json5")
        (self.path / "config.json5").write_text(ROOT_CONFIG, encoding="utf-8")
        (self.path / ".env").write_text(
            "TEST_INSTANCE=test\nADMIN_PASSWORD=admin-secret\nMCP_TOKENS=test-token\n",
            encoding="utf-8",
        )
        image = self.path / "images" / "demo"
        image.mkdir(parents=True)
        (image / "sndbx.overlay.json5").write_text(OVERLAY_CONFIG, encoding="utf-8")
        return self.path

    def __exit__(self, *exc_info: object) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def copy_live_root(destination: Path) -> Path:
    """Copy the live root config, dotenv and overlays into a temp root."""
    for name in ("config.json5", "config.descriptor.json5", "config.types.json5"):
        shutil.copy2(ROOT / name, destination / name)
    env_lines = (ROOT / ".env").read_text(encoding="utf-8").splitlines()
    env_lines = [line for line in env_lines if not line.strip().startswith("SNDBX_ROOT=")]
    env_lines.append(f"SNDBX_ROOT={destination}")
    (destination / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    overrides = ROOT / "data" / "sandbox_startup_overrides.json"
    if overrides.exists():
        (destination / "data").mkdir()
        shutil.copy2(overrides, destination / "data" / overrides.name)
    for overlay in (ROOT / "images").glob("*/sndbx.overlay.json5"):
        target = destination / "images" / overlay.parent.name
        target.mkdir(parents=True)
        shutil.copy2(overlay, target / overlay.name)
    return destination
