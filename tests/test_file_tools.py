"""Tests for the MCP file tools and the shell-free execution path."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import Mock

from tools import ToolHandlers, READ_FILE_LIMIT_SCRIPT, READ_FILE_SCRIPT


class RecordingManager:
    """Sandbox manager stub that records the executed argv."""

    def __init__(self) -> None:
        """Prepare an empty call record."""
        self.calls: list[tuple[list[str], str | None]] = []
        self.result = (True, "output")

    def exec_argv(self, sandbox_id, argv, stdin_data=None, timeout=30, encoding="utf-8", errors="replace"):
        """Record the argv vector and the stdin payload."""
        self.calls.append((list(argv), stdin_data))
        return self.result

    def execute_command(self, sandbox_id, command):
        """Reject shell-based execution."""
        raise AssertionError("file tools must not use a shell command")


class FileToolTests(unittest.TestCase):
    """Verify that file tools pass paths as argv entries."""

    def setUp(self) -> None:
        self.manager = RecordingManager()
        self.handlers = ToolHandlers(self.manager, {
            "envids": {"demo-env": {"sandbox": "demo"}}
        })

    def _argv(self) -> list[str]:
        return self.manager.calls[-1][0]

    def test_read_file_uses_argv(self) -> None:
        """Read a file without embedding the path in a shell string."""
        asyncio.run(self.handlers.tool_read_file(
            {"path": "/root/a b.txt"}, "demo-env", "token"
        ))
        argv = self._argv()
        self.assertEqual(argv[0], "sh")
        self.assertEqual(argv[-1], "/root/a b.txt")
        self.assertIn("$1", argv[2])
        self.assertNotIn("/root/a b.txt", argv[2])

    def test_read_file_honours_the_limit(self) -> None:
        """Add the head pipeline when a limit is given."""
        asyncio.run(self.handlers.tool_read_file(
            {"path": "/root/a.txt", "offset": 4, "limit": 10}, "demo-env", "token"
        ))
        script = self._argv()[2]
        self.assertEqual(script, READ_FILE_LIMIT_SCRIPT.format(offset=5, limit=10))

    def test_read_file_without_a_limit_reads_to_eof(self) -> None:
        """Omit the head pipeline when no limit is given."""
        asyncio.run(self.handlers.tool_read_file(
            {"path": "/root/a.txt"}, "demo-env", "token"
        ))
        self.assertEqual(self._argv()[2], READ_FILE_SCRIPT.format(offset=1))

    def test_write_file_sends_the_content_on_stdin(self) -> None:
        """Pass the file content through stdin, never through the command."""
        content = "line one\nEOF\nnot a delimiter\n"
        asyncio.run(self.handlers.tool_write_file(
            {"path": "/root/a.txt", "content": content}, "demo-env", "token"
        ))
        argv, stdin = self.manager.calls[-1]
        self.assertEqual(argv[-1], "/root/a.txt")
        self.assertEqual(stdin, content)
        self.assertNotIn("EOF", argv[2])

    def test_write_file_requires_a_path(self) -> None:
        """Reject a missing path."""
        result = asyncio.run(self.handlers.tool_write_file(
            {"content": "x"}, "demo-env", "token"
        ))
        self.assertIn("error", result)
        self.assertEqual(self.manager.calls, [])

    def test_patch_file_reads_and_writes_without_a_shell(self) -> None:
        """Read the file, patch it on the host and write it back."""
        patch = "<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n"
        self.manager.result = (True, "old\nrest\n")
        asyncio.run(self.handlers.tool_patch_file(
            {"file_path": "/root/a.txt", "patch_content": patch}, "demo-env", "token"
        ))
        self.assertEqual(len(self.manager.calls), 2)
        read_argv, read_stdin = self.manager.calls[0]
        self.assertEqual(read_argv[-1], "/root/a.txt")
        self.assertIsNone(read_stdin)
        write_argv, write_stdin = self.manager.calls[1]
        self.assertEqual(write_argv[-1], "/root/a.txt")
        self.assertEqual(write_stdin, "new\nrest\n")

    def test_patch_file_needs_no_interpreter_in_the_guest(self) -> None:
        """Do not require python3 inside the guest."""
        patch = "<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n"
        self.manager.result = (True, "old\n")
        asyncio.run(self.handlers.tool_patch_file(
            {"file_path": "/root/a.txt", "patch_content": patch}, "demo-env", "token"
        ))
        for argv, _ in self.manager.calls:
            self.assertNotIn("python3", argv)

    def test_patch_file_rejects_a_malformed_patch(self) -> None:
        """Refuse a patch that does not use the block markers."""
        result = asyncio.run(self.handlers.tool_patch_file(
            {"file_path": "/root/a.txt", "patch_content": "nonsense"}, "demo-env", "token"
        ))
        self.assertIn("error", result)
        self.assertEqual(self.manager.calls, [])

    def test_tools_report_an_unknown_envid(self) -> None:
        """Refuse to act without a resolvable sandbox."""
        result = asyncio.run(self.handlers.tool_read_file(
            {"path": "/root/a.txt"}, "missing-env", "token"
        ))
        self.assertIn("error", result)


class PublishedEndpointTests(unittest.TestCase):
    """Verify the guest-port to host-port mapping."""

    def _manager(self, bindings):
        from sandbox import DockerSandboxManager
        return DockerSandboxManager({"sandboxes": {"items": {"demo": {"port_bindings": bindings}}}})

    def test_maps_a_published_port(self) -> None:
        """Return the published host endpoint for a guest port."""
        manager = self._manager([
            {"bind_host": "127.0.0.1", "vm_port": 9011, "publish_port": 39011}
        ])
        self.assertEqual(manager.published_endpoint("demo", 9011), ("127.0.0.1", 39011))

    def test_maps_a_wildcard_bind_to_loopback(self) -> None:
        """Use loopback when the binding listens on every address."""
        manager = self._manager([
            {"bind_host": "0.0.0.0", "vm_port": 9011, "publish_port": 39011}
        ])
        self.assertEqual(manager.published_endpoint("demo", 9011), ("127.0.0.1", 39011))

    def test_returns_none_for_an_unpublished_port(self) -> None:
        """Report that the guest port is not published."""
        manager = self._manager([
            {"bind_host": "127.0.0.1", "vm_port": 9011, "publish_port": 39011}
        ])
        self.assertIsNone(manager.published_endpoint("demo", 9999))

    def test_ignores_invalid_bindings(self) -> None:
        """Skip malformed binding rows."""
        manager = self._manager([{"vm_port": "x", "publish_port": None}])
        self.assertIsNone(manager.published_endpoint("demo", 9011))


if __name__ == "__main__":
    unittest.main()
