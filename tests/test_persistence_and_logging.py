"""Tests for sandbox persistence semantics and the MCP request log rotation."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from mcp_server import MCPServer
from sandbox import DockerSandboxManager


class PersistenceTests(unittest.TestCase):
    """Verify how a non-persistent sandbox is stopped and recreated."""

    def _manager(self, sandbox_config: dict) -> DockerSandboxManager:
        """Return a manager with Docker operations replaced by mocks."""
        manager = DockerSandboxManager({
            "root": ".",
            "sandboxes": {"items": {"test": sandbox_config}},
        })
        manager._run_docker_cmd = Mock(return_value=(True, "ok"))
        manager._ensure_image_ready = Mock(return_value=(True, "ready"))
        manager._run_image_hook = Mock(return_value=(True, "no hook"))
        return manager

    def test_persistent_sandbox_keeps_its_container(self) -> None:
        """Stop without removing the container when the sandbox is persistent."""
        manager = self._manager({"persistent": True})

        success, _ = manager.stop_sandbox("test")

        self.assertTrue(success)
        commands = [call.args[0] for call in manager._run_docker_cmd.call_args_list]
        self.assertEqual(commands, [["stop", "--time", "30", "sndbx-test"]])

    def test_non_persistent_sandbox_is_removed_on_stop(self) -> None:
        """Remove the container so the next start is a fresh sandbox."""
        manager = self._manager({"persistent": False})

        success, _ = manager.stop_sandbox("test")

        self.assertTrue(success)
        commands = [call.args[0] for call in manager._run_docker_cmd.call_args_list]
        self.assertEqual(commands, [
            ["stop", "--time", "30", "sndbx-test"],
            ["rm", "-f", "sndbx-test"],
        ])

    def test_missing_persistent_key_keeps_the_container(self) -> None:
        """Treat an absent key as persistent, matching the previous behaviour."""
        manager = self._manager({})

        manager.stop_sandbox("test")

        commands = [call.args[0] for call in manager._run_docker_cmd.call_args_list]
        self.assertNotIn(["rm", "-f", "sndbx-test"], commands)

    def test_recreate_refuses_a_changed_rootfs(self) -> None:
        """Protect the writable layer of a persistent sandbox."""
        manager = self._manager({"persistent": True})
        manager._run_docker_cmd = Mock(return_value=(True, "C /root/data\n"))

        success, message = manager.recreate_sandbox("test")

        self.assertFalse(success)
        self.assertIn("root filesystem has changes", message)

    def test_recreate_allows_a_changed_rootfs_when_not_persistent(self) -> None:
        """Discard the writable layer of a non-persistent sandbox."""
        manager = self._manager({"persistent": False})
        manager._run_docker_cmd = Mock(return_value=(True, "C /root/data\n"))

        success, _ = manager.recreate_sandbox("test")

        self.assertTrue(success)


def close_server(server: MCPServer) -> None:
    """Release the request log handle of a server that never started."""
    if server._request_log_file is not None:
        server._request_log_file.close()
        server._request_log_file = None


class RequestLogRotationTests(unittest.TestCase):
    """Verify the size cap on the MCP request log."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "logs").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _server(self, max_mb: int) -> MCPServer:
        """Return a server with the request log enabled."""
        return MCPServer("127.0.0.1", 0, {
            "root": str(self.root),
            "logging": {"log_mcp_requests": True, "mcp_request_log_max_mb": max_mb},
        })

    def test_rotates_the_log_when_the_cap_is_exceeded(self) -> None:
        """Move the log aside and start a fresh one."""
        server = self._server(1)
        path = self.root / "logs" / "mcp_requests.jsonl"
        path.write_text("x" * (1024 * 1024 + 10), encoding="utf-8")

        server._rotate_request_log_if_needed()

        self.assertTrue(path.with_name("mcp_requests.jsonl.1").is_file())
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_size, 0)
        close_server(server)

    def test_keeps_the_log_below_the_cap(self) -> None:
        """Leave the log alone while it is smaller than the cap."""
        server = self._server(64)
        path = self.root / "logs" / "mcp_requests.jsonl"
        path.write_text("small\n", encoding="utf-8")

        server._rotate_request_log_if_needed()

        self.assertEqual(path.read_text(encoding="utf-8"), "small\n")
        self.assertFalse(path.with_name("mcp_requests.jsonl.1").exists())
        close_server(server)

    def test_zero_disables_rotation(self) -> None:
        """Never rotate when the cap is zero."""
        server = self._server(0)
        path = self.root / "logs" / "mcp_requests.jsonl"
        path.write_text("x" * (1024 * 1024 + 10), encoding="utf-8")

        server._rotate_request_log_if_needed()

        self.assertFalse(path.with_name("mcp_requests.jsonl.1").exists())
        close_server(server)

    def test_default_cap_is_64_megabytes(self) -> None:
        """Use the documented default when nothing is configured."""
        server = MCPServer("127.0.0.1", 0, {"root": str(self.root)})
        self.assertEqual(server._request_log_max_bytes, 64 * 1024 * 1024)
        close_server(server)

    def test_requests_are_written_as_jsonl(self) -> None:
        """Append one JSON object per line."""
        server = self._server(64)
        server._log_rpc("request", {"method": "tools/list"})
        path = self.root / "logs" / "mcp_requests.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["type"], "request")
        self.assertIn("ts", entry)
        close_server(server)


if __name__ == "__main__":
    unittest.main()
