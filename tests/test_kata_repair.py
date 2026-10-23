"""Tests for the Kata runtime repair endpoint and the runtime path helper."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webui_server import WebUIServer
from kata import kata_runtime_paths

ROOT = Path(__file__).resolve().parents[1]


class FakeManager:
    """Minimal sandbox manager stub for the Web UI server."""

    def __init__(self) -> None:
        """Prepare an empty sandbox configuration."""
        self.sandbox_configs: dict = {}

    def list_sandboxes(self):
        """Report that no sandbox containers exist."""
        return True, []


class KataRuntimePathTests(unittest.TestCase):
    """Verify that the runtime paths follow the Docker daemon configuration."""

    def test_defaults_when_daemon_config_is_absent(self) -> None:
        """Fall back to the historical /opt/kata layout."""
        with patch("kata.DOCKER_DAEMON_CONFIG", Path("/nonexistent/daemon.json")):
            paths = kata_runtime_paths()
        self.assertEqual(paths["shim"], Path("/opt/kata/runtime-rs/bin/containerd-shim-kata-v2"))
        self.assertEqual(
            paths["config"], Path("/etc/kata-containers/runtime-rs/configuration.toml")
        )

    def test_reads_registered_runtime(self) -> None:
        """Use the runtimeType and ConfigPath registered with Docker."""
        with tempfile.TemporaryDirectory() as temporary:
            daemon = Path(temporary) / "daemon.json"
            daemon.write_text(json.dumps({
                "runtimes": {
                    "kata": {
                        "runtimeType": "/mnt/raid1/kata/runtime-rs/bin/containerd-shim-kata-v2",
                        "options": {
                            "ConfigPath": "/etc/kata-containers/runtime-rs/configuration.toml"
                        },
                    }
                }
            }), encoding="utf-8")
            with patch("kata.DOCKER_DAEMON_CONFIG", daemon):
                paths = kata_runtime_paths()
        self.assertEqual(
            paths["shim"], Path("/mnt/raid1/kata/runtime-rs/bin/containerd-shim-kata-v2")
        )
        self.assertEqual(
            paths["default_config"],
            Path("/mnt/raid1/kata/share/defaults/kata-containers/runtime-rs/"
                 "configuration-qemu-runtime-rs.toml"),
        )

    def test_ignores_invalid_daemon_config(self) -> None:
        """Keep the defaults when daemon.json cannot be parsed."""
        with tempfile.TemporaryDirectory() as temporary:
            daemon = Path(temporary) / "daemon.json"
            daemon.write_text("{not json", encoding="utf-8")
            with patch("kata.DOCKER_DAEMON_CONFIG", daemon):
                paths = kata_runtime_paths()
        self.assertEqual(paths["shim"], Path("/opt/kata/runtime-rs/bin/containerd-shim-kata-v2"))


class KataRepairEndpointTests(unittest.TestCase):
    """Verify that the Kata repair endpoint answers instead of raising."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        frontend = self.root / "src" / "webui" / "frontend"
        frontend.parent.mkdir(parents=True, exist_ok=True)
        frontend.symlink_to(ROOT / "src" / "webui" / "frontend", target_is_directory=True)
        config = {
            "root": str(self.root),
            "sandboxes": {"items": {}},
            "webui": {
                "port": 30080,
                "bind": "127.0.0.1",
                "auth": {
                    "users": [{"login": "admin", "password": "secret", "permissions": ["all"]}],
                    "session_ttl": 3600,
                },
            },
            "ssh": {"port_range": [30200, 30210]},
        }
        self.server = WebUIServer(self.root, config, FakeManager())

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_repair_method_exists(self) -> None:
        """The repair helper is reachable as a class method."""
        self.assertTrue(hasattr(WebUIServer, "_repair_kata_runtime"))

    def test_repair_reports_missing_shim(self) -> None:
        """Report a missing shim instead of raising."""
        paths = {
            "shim": Path("/nonexistent/containerd-shim-kata-v2"),
            "config": Path("/nonexistent/configuration.toml"),
            "default_config": Path("/nonexistent/default.toml"),
        }
        with patch("webui_server.kata_runtime_paths", return_value=paths):
            result = self.server._repair_kata_runtime()
        self.assertFalse(result["ok"])
        self.assertIn("not available", result["message"])
        self.assertTrue(result["report"])

    def test_repair_reports_already_configured(self) -> None:
        """Report success when Docker already lists the kata runtime."""
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "configuration.toml"
            config.write_text("[runtime]\n", encoding="utf-8")
            paths = {
                "shim": Path("/bin/sh"),
                "config": config,
                "default_config": Path("/nonexistent/default.toml"),
            }

            def fake_run(cmd, *args, **kwargs):
                if cmd[:2] == ["docker", "info"]:
                    return subprocess_result(0, '{"kata":{"runtimeType":"/bin/sh"}}')
                return subprocess_result(0, "4.1.0\n")

            with patch("webui_server.kata_runtime_paths", return_value=paths), \
                    patch("subprocess.run", side_effect=fake_run):
                result = self.server._repair_kata_runtime()
        self.assertTrue(result["ok"], result)
        self.assertIn("already configured", result["message"])


def subprocess_result(returncode: int, stdout: str, stderr: str = ""):
    """Return a completed-process stub."""
    class _Result:
        def __init__(self) -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    return _Result()


if __name__ == "__main__":
    unittest.main()
