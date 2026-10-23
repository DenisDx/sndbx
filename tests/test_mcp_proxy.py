"""Tests for the VM-side MCP proxy client and the mcp_proxy_call tool."""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import Mock

from mcp_proxy import McpProxyError, VmMcpClient, resolve_tool_name, tool_is_allowed
from tools import ToolHandlers


class FakeBackend:
    """A minimal MCP server speaking JSONL over TCP."""

    def __init__(self, tools: list[dict] | None = None) -> None:
        """Prepare the tool list the backend publishes."""
        self.tools = tools or []
        self.requests: list[dict] = []
        self._server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> None:
        """Start listening on a random localhost port."""
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        """Stop the listener."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Serve one JSONL connection."""
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                request = json.loads(line.decode("utf-8"))
                self.requests.append(request)
                method = request.get("method")
                if method == "initialize":
                    writer.write(json.dumps({
                        "jsonrpc": "2.0", "id": request.get("id"),
                        "result": {"protocolVersion": "2024-11-05", "capabilities": {}},
                    }).encode("utf-8") + b"\n")
                elif method == "notifications/initialized":
                    continue
                elif method == "tools/list":
                    writer.write(json.dumps({
                        "jsonrpc": "2.0", "id": request.get("id"),
                        "result": {"tools": self.tools},
                    }).encode("utf-8") + b"\n")
                else:
                    writer.write(json.dumps({
                        "jsonrpc": "2.0", "id": request.get("id"),
                        "result": {"echo": request.get("params")},
                    }).encode("utf-8") + b"\n")
                await writer.drain()
        finally:
            writer.close()


class ProxyClientTests(unittest.TestCase):
    """Verify the handshake and the forwarded request."""

    def test_forwards_a_request_after_the_handshake(self) -> None:
        """Perform the handshake and return the backend response."""
        async def scenario():
            backend = FakeBackend(tools=[{"name": "read_file"}])
            await backend.start()
            try:
                client = VmMcpClient("127.0.0.1", backend.port)
                response = await client.call({
                    "jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {},
                })
                return backend.requests, response
            finally:
                await backend.stop()

        requests, response = asyncio.run(scenario())
        methods = [request.get("method") for request in requests]
        self.assertEqual(methods[0], "initialize")
        self.assertEqual(methods[1], "notifications/initialized")
        self.assertEqual(methods[2], "tools/list")
        self.assertEqual(response["result"]["tools"], [{"name": "read_file"}])

    def test_passes_the_token_through(self) -> None:
        """Add the caller's token to the forwarded request."""
        async def scenario():
            backend = FakeBackend()
            await backend.start()
            try:
                client = VmMcpClient("127.0.0.1", backend.port)
                await client.call(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token="secret"
                )
                return backend.requests
            finally:
                await backend.stop()

        requests = asyncio.run(scenario())
        self.assertEqual(requests[-1].get("token"), "secret")

    def test_reports_an_unreachable_backend(self) -> None:
        """Return an error instead of raising."""
        async def scenario():
            client = VmMcpClient("127.0.0.1", 1)
            await client.call({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

        with self.assertRaises(McpProxyError):
            asyncio.run(scenario())


class ToolAllowListTests(unittest.TestCase):
    """Verify the mcp_bindings tool allow-list."""

    def test_resolves_the_tool_name(self) -> None:
        """Find the tool name inside a tools/call request."""
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "read_file"}}
        self.assertEqual(resolve_tool_name(request), "read_file")

    def test_wildcard_allows_everything(self) -> None:
        """Allow any tool when the list is '*'."""
        request = {"method": "tools/call", "params": {"name": "read_file"}}
        self.assertEqual(tool_is_allowed(request, ["*"]), (True, None))

    def test_allows_a_listed_tool(self) -> None:
        """Allow a tool that is on the list."""
        request = {"method": "tools/call", "params": {"name": "read_file"}}
        self.assertEqual(tool_is_allowed(request, ["read_file"]), (True, None))

    def test_blocks_an_unlisted_tool(self) -> None:
        """Block a tool that is not on the list."""
        request = {"method": "tools/call", "params": {"name": "write_file"}}
        allowed, blocked = tool_is_allowed(request, ["read_file"])
        self.assertFalse(allowed)
        self.assertEqual(blocked, "write_file")

    def test_allows_metadata_methods(self) -> None:
        """Allow tools/list even when the tool list is restricted."""
        request = {"method": "tools/list"}
        self.assertEqual(tool_is_allowed(request, ["read_file"]), (True, None))


class ProxyToolTests(unittest.TestCase):
    """Verify the mcp_proxy_call tool handler."""

    def setUp(self) -> None:
        self.manager = Mock()
        self.manager.sandbox_configs = {
            "toolbox": {
                "port_bindings": [
                    {"bind_host": "127.0.0.1", "vm_port": 9011, "publish_port": 39011}
                ],
                "mcp_bindings": [
                    {
                        "backend_id": "filesystem",
                        "transport": "tcp-jsonl",
                        "vm_port": 9011,
                        "tools": ["*"],
                        "auth_mode": "token_passthrough",
                    }
                ],
            }
        }
        self.manager.published_endpoint = Mock(return_value=("127.0.0.1", 39011))
        self.manager.get_container_ip = Mock(return_value="10.0.0.5")
        self.handlers = ToolHandlers(self.manager, {
            "envids": {"toolbox-env": {"sandbox": "toolbox"}}
        })

    def test_returns_an_error_without_bindings(self) -> None:
        """Report that the sandbox publishes no backends."""
        self.manager.sandbox_configs = {"toolbox": {}}
        result = asyncio.run(self.handlers.tool_mcp_proxy_call(
            {"request": {"method": "tools/list"}}, "toolbox-env", "token"
        ))
        self.assertFalse(result["success"])
        self.assertIn("error", result)

    def test_returns_an_error_for_an_unknown_backend(self) -> None:
        """Report that the requested backend does not exist."""
        result = asyncio.run(self.handlers.tool_mcp_proxy_call(
            {"backend_id": "nope", "request": {"method": "tools/list"}},
            "toolbox-env",
            "token",
        ))
        self.assertFalse(result["success"])
        self.assertIn("not found", result["error"])

    def test_blocks_a_tool_outside_the_allow_list(self) -> None:
        """Refuse to forward a tool the binding does not publish."""
        self.manager.sandbox_configs["toolbox"]["mcp_bindings"][0]["tools"] = ["read_file"]
        result = asyncio.run(self.handlers.tool_mcp_proxy_call(
            {"request": {"method": "tools/call", "params": {"name": "write_file"}}},
            "toolbox-env",
            "token",
        ))
        self.assertFalse(result["success"])
        self.assertIn("not published", result["error"])

    def test_requires_a_jsonrpc_request(self) -> None:
        """Reject a missing request object."""
        result = asyncio.run(self.handlers.tool_mcp_proxy_call({}, "toolbox-env", "token"))
        self.assertFalse(result["success"])
        self.assertIn("error", result)

    def test_forwards_the_request_to_the_backend(self) -> None:
        """Return the backend response for a valid call."""
        async def scenario():
            backend = FakeBackend(tools=[{"name": "read_file"}])
            await backend.start()
            try:
                self.manager.published_endpoint = Mock(
                    return_value=("127.0.0.1", backend.port)
                )
                return await self.handlers.tool_mcp_proxy_call(
                    {"request": {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}},
                    "toolbox-env",
                    "token",
                )
            finally:
                await backend.stop()

        result = asyncio.run(scenario())
        self.assertTrue(result["success"], result)
        self.assertEqual(result["response"]["result"]["tools"], [{"name": "read_file"}])


if __name__ == "__main__":
    unittest.main()
