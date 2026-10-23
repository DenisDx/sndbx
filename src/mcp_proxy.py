"""Client for MCP backends published by a sandbox over TCP.

A sandbox image may start MCP servers inside the guest and expose them on
guest TCP ports. The protocol is newline-delimited JSON (JSONL) with a
mandatory initialization handshake on every connection:

    client -> initialize            (request)
    server -> result
    client -> notifications/initialized
    client -> <real request>
    server -> <real response>

The same transport is described for direct clients in ``MCP_USAGE.md``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional, Tuple

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "sndbx-mcp-proxy", "version": "1.0"}


class McpProxyError(Exception):
    """Proxy transport or protocol failure."""


class VmMcpClient:
    """Talks JSON-RPC to one MCP backend reachable over TCP."""

    def __init__(
        self,
        host: str,
        port: int,
        startup_timeout: float = 10.0,
        read_timeout: float = 30.0,
    ) -> None:
        """Store the endpoint and the timeouts used for the handshake and calls."""
        self.host = host
        self.port = int(port)
        self.startup_timeout = float(startup_timeout)
        self.read_timeout = float(read_timeout)

    async def _send(self, writer: asyncio.StreamWriter, payload: Dict[str, Any]) -> None:
        """Write one JSON-RPC object as a single JSONL line."""
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        writer.write(data)
        await asyncio.wait_for(writer.drain(), timeout=self.read_timeout)

    async def _receive(self, reader: asyncio.StreamReader) -> Dict[str, Any]:
        """Read lines until one parses as a JSON object."""
        while True:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=self.read_timeout)
            except asyncio.TimeoutError as error:
                raise McpProxyError("timed out waiting for the backend response") from error
            if not line:
                raise McpProxyError("the backend closed the connection")
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed

    async def _handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Perform the initialize handshake and the initialized notification."""
        await self._send(writer, {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        })
        await self._receive(reader)
        await self._send(writer, {"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def call(self, request: Dict[str, Any], token: Optional[str] = None) -> Dict[str, Any]:
        """Forward one JSON-RPC request to the backend and return its response.

        input: JSON-RPC request object and an optional token to pass through
        output: the backend JSON-RPC response object
        """
        payload = dict(request or {})
        if token and "token" not in payload:
            payload["token"] = token

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.startup_timeout,
            )
        except asyncio.TimeoutError as error:
            raise McpProxyError(
                f"timed out connecting to the backend at {self.host}:{self.port}"
            ) from error
        except OSError as error:
            raise McpProxyError(
                f"could not connect to the backend at {self.host}:{self.port}: {error}"
            ) from error

        try:
            await self._handshake(reader, writer)
            await self._send(writer, payload)
            return await self._receive(reader)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


def resolve_tool_name(request: Dict[str, Any]) -> Optional[str]:
    """Return the tool name a JSON-RPC request would invoke, if any."""
    if not isinstance(request, dict):
        return None
    method = str(request.get("method") or "").strip()
    if method != "tools/call":
        return None
    params = request.get("params")
    if not isinstance(params, dict):
        return None
    name = params.get("name")
    return str(name).strip() if isinstance(name, str) and name.strip() else None


def tool_is_allowed(request: Dict[str, Any], allowed: Any) -> Tuple[bool, Optional[str]]:
    """Check a request against an mcp_bindings tool allow-list.

    input: JSON-RPC request and the configured 'tools' value
    output: (allowed, blocked tool name)
    """
    if not isinstance(allowed, list) or not allowed:
        return True, None
    if "*" in allowed:
        return True, None
    name = resolve_tool_name(request)
    if name is None:
        return True, None
    if name in allowed:
        return True, None
    return False, name
