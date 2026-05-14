"""
MCP tool implementations for sndbx
Core operations: file read/write, command execution, SSH access
"""

import os
import json
import asyncio
from pathlib import Path
from typing import Dict, Any, Optional

from logging_utils import get_logger

logger = get_logger("mcp")


class ToolHandlers:
    """MCP tool handlers"""

    def __init__(self, sandbox_manager, config):
        self.sandbox_manager = sandbox_manager
        self.config = config

    async def tool_execute_command(self, params, envid, token):
        """Execute bash command in sandbox"""
        command = params.get("command", "")
        if not command:
            return {"error": "command parameter is required"}
        sandbox_id = self._get_sandbox_for_envid(envid)
        if not sandbox_id:
            return {"error": f"No sandbox for envid {envid}"}
        logger.info("tool_execute_command sandbox=%s", sandbox_id)
        success, output = self.sandbox_manager.execute_command(sandbox_id, command)
        logger.info("tool_execute_command finished sandbox=%s success=%s", sandbox_id, success)
        return {"success": success, "output": output, "sandbox_id": sandbox_id}

    async def tool_read_file(self, params, envid, token):
        """Read file from sandbox"""
        path = params.get("path", "")
        if not path:
            return {"error": "path parameter is required"}
        sandbox_id = self._get_sandbox_for_envid(envid)
        if not sandbox_id:
            return {"error": f"No sandbox for envid {envid}"}
        logger.info("tool_read_file sandbox=%s path=%s", sandbox_id, path)
        success, output = self.sandbox_manager.execute_command(
            sandbox_id, f'cat "{path}" 2>&1'
        )
        logger.info("tool_read_file finished sandbox=%s success=%s", sandbox_id, success)
        return {"success": success, "content": output, "path": path, "sandbox_id": sandbox_id}

    async def tool_write_file(self, params, envid, token):
        """Write file to sandbox"""
        path = params.get("path", "")
        content = params.get("content", "")
        if not path:
            return {"error": "path parameter is required"}
        sandbox_id = self._get_sandbox_for_envid(envid)
        if not sandbox_id:
            return {"error": f"No sandbox for envid {envid}"}
        logger.info("tool_write_file sandbox=%s path=%s bytes=%s", sandbox_id, path, len(str(content)))
        success, output = self.sandbox_manager.execute_command(
            sandbox_id,
            f'mkdir -p "$(dirname {path})" && cat > "{path}" << \'EOF\'\n{content}\nEOF',
        )
        logger.info("tool_write_file finished sandbox=%s success=%s", sandbox_id, success)
        return {"success": success, "path": path, "sandbox_id": sandbox_id,
                "message": "File written" if success else output}

    async def tool_sandbox_status(self, params, envid, token):
        """Get sandbox status"""
        sandbox_id = self._get_sandbox_for_envid(envid)
        if not sandbox_id:
            return {"error": f"No sandbox for envid {envid}"}
        logger.info("tool_sandbox_status sandbox=%s", sandbox_id)
        status = self.sandbox_manager.get_status(sandbox_id)
        return {"id": status.id, "running": status.running,
                "container_id": status.container_id, "ip": status.ip, "error": status.error}

    async def tool_sandbox_start(self, params, envid, token):
        """Start sandbox"""
        sandbox_id = self._get_sandbox_for_envid(envid)
        if not sandbox_id:
            return {"error": f"No sandbox for envid {envid}"}
        logger.info("tool_sandbox_start sandbox=%s", sandbox_id)
        success, output = self.sandbox_manager.start_sandbox(sandbox_id)
        return {"success": success, "message": output, "sandbox_id": sandbox_id}

    async def tool_sandbox_stop(self, params, envid, token):
        """Stop sandbox"""
        sandbox_id = self._get_sandbox_for_envid(envid)
        if not sandbox_id:
            return {"error": f"No sandbox for envid {envid}"}
        logger.info("tool_sandbox_stop sandbox=%s", sandbox_id)
        success, output = self.sandbox_manager.stop_sandbox(sandbox_id)
        return {"success": success, "message": output, "sandbox_id": sandbox_id}

    async def tool_mcp_proxy_call(self, params, envid, token):
        """Proxy one MCP request to a VM backend listed in mcp_bindings.

        Performs the MCP initialize handshake before the real call, then closes.
        input:  backend_id (optional), request (JSON-RPC object), timeout_sec (optional)
        output: proxied backend response payload
        """
        sandbox_id = self._get_sandbox_for_envid(envid)
        if not sandbox_id:
            return {"error": f"No sandbox for envid {envid}"}
        logger.info("tool_mcp_proxy_call sandbox=%s backend_id=%s", sandbox_id, params.get("backend_id", ""))

        sandbox_cfg = self.sandbox_manager.sandbox_configs.get(sandbox_id, {})
        rows = sandbox_cfg.get("mcp_bindings", [])
        if not isinstance(rows, list) or not rows:
            return {"error": f"No mcp_bindings configured for sandbox {sandbox_id}"}

        requested_backend_id = str(params.get("backend_id", "")).strip()
        selected = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            backend_id = str(row.get("backend_id", "")).strip()
            if requested_backend_id:
                if backend_id == requested_backend_id:
                    selected = row
                    break
            elif not selected:
                selected = row

        if not selected:
            return {"error": f"mcp backend not found: {requested_backend_id or '(default)'}"}

        auth_mode = str(selected.get("auth_mode", "token_passthrough")).strip().lower()
        if auth_mode == "internal_only":
            return {"error": "Selected mcp backend is internal_only and cannot be called externally"}

        request = params.get("request")
        if not isinstance(request, dict):
            return {"error": "request parameter is required and must be an object"}

        req_method = str(request.get("method", "")).strip()
        allowed_tools = selected.get("tools")
        if isinstance(allowed_tools, list) and allowed_tools:
            allowed = {str(x).strip() for x in allowed_tools if str(x).strip()}
            if "*" not in allowed and req_method and req_method not in allowed:
                return {"error": f"Method '{req_method}' is not allowed by mcp binding allow-list"}

        if auth_mode == "token_passthrough" and "token" not in request:
            request = dict(request)
            request["token"] = token

        vm_port = selected.get("vm_port")
        try:
            vm_port_i = int(vm_port)
        except (TypeError, ValueError):
            return {"error": f"Invalid vm_port in mcp binding: {vm_port}"}

        status = self.sandbox_manager.get_status(sandbox_id)
        if not status.running:
            return {"error": f"Sandbox is not running: {sandbox_id}"}
        if not status.ip:
            return {"error": f"Could not determine sandbox IP: {sandbox_id}"}

        timeout_sec = float(params.get("timeout_sec", 15.0))
        transport = str(selected.get("transport", "tcp-jsonl")).strip().lower()
        if transport not in ("tcp-jsonl", "tcp-stdio"):
            return {"error": f"Unsupported transport '{transport}'. Supported: tcp-jsonl, tcp-stdio"}

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(status.ip, vm_port_i),
                timeout=timeout_sec,
            )
        except Exception as exc:
            return {"error": f"Could not connect to backend {status.ip}:{vm_port_i}: {exc}"}

        try:
            if transport == "tcp-jsonl":
                backend_response = await self._mcp_jsonl_call(reader, writer, request, timeout_sec)
            else:
                backend_response = await self._mcp_stdio_call(reader, writer, request, timeout_sec)

            return {
                "success": True,
                "sandbox_id": sandbox_id,
                "backend_id": str(selected.get("backend_id", "")),
                "transport": transport,
                "response": backend_response,
            }
        except asyncio.TimeoutError:
            return {"error": f"Backend request timed out after {timeout_sec:.1f}s"}
        except asyncio.IncompleteReadError:
            return {"error": "Backend closed stream before full response was read"}
        except EOFError as exc:
            return {"error": str(exc)}
        finally:
            writer.close()
            await writer.wait_closed()

    async def _mcp_jsonl_call(self, reader, writer, request, timeout_sec):
        """MCP JSONL handshake: initialize -> initialized -> real request.

        input:  open reader/writer, user request dict, timeout
        output: parsed JSON-RPC response dict
        """
        init_req = {
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "sndbx-proxy", "version": "1.0"},
            },
        }
        await self._mcp_jsonl_send(writer, init_req, timeout_sec)
        await self._mcp_jsonl_recv(reader, timeout_sec)  # discard initialize result
        await self._mcp_jsonl_send(writer, {"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout_sec)
        await self._mcp_jsonl_send(writer, request, timeout_sec)
        return await self._mcp_jsonl_recv(reader, timeout_sec)

    async def _mcp_stdio_call(self, reader, writer, request, timeout_sec):
        """MCP Content-Length framing handshake: initialize -> initialized -> real request.

        input:  open reader/writer, user request dict, timeout
        output: parsed JSON-RPC response dict
        """
        init_req = {
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "sndbx-proxy", "version": "1.0"},
            },
        }
        await self._mcp_send_frame(writer, init_req, timeout_sec)
        await self._mcp_read_frame(reader, timeout_sec)  # discard initialize result
        await self._mcp_send_frame(writer, {"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout_sec)
        await self._mcp_send_frame(writer, request, timeout_sec)
        return await self._mcp_read_frame(reader, timeout_sec)

    @staticmethod
    async def _mcp_jsonl_send(writer, obj, timeout_sec):
        """Send one newline-delimited JSON message.

        input:  writer, object to send, timeout
        """
        line = json.dumps(obj, ensure_ascii=True) + "\n"
        writer.write(line.encode("utf-8"))
        await asyncio.wait_for(writer.drain(), timeout=timeout_sec)

    @staticmethod
    async def _mcp_jsonl_recv(reader, timeout_sec):
        """Read one JSONL message, skipping non-JSON banner lines.

        input:  reader, timeout
        output: parsed JSON object
        raises: asyncio.TimeoutError, EOFError
        """
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=timeout_sec)
            if not line:
                raise EOFError("Backend closed stream before JSON response")
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                logger.debug("Skipping non-JSON line from backend: %r", text[:200])

    @staticmethod
    async def _mcp_send_frame(writer, obj, timeout_sec):
        """Send one Content-Length framed JSON message.

        input:  writer, object to send, timeout
        """
        body = json.dumps(obj, ensure_ascii=True).encode("utf-8")
        frame = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        writer.write(frame)
        await asyncio.wait_for(writer.drain(), timeout=timeout_sec)

    @staticmethod
    async def _mcp_read_frame(reader, timeout_sec):
        """Read one Content-Length framed message, skipping pre-frame banner bytes.

        input:  reader, timeout
        output: parsed JSON object
        raises: asyncio.TimeoutError, asyncio.IncompleteReadError, EOFError, ValueError
        """
        buf = b""
        while True:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout_sec)
            if not chunk:
                raise EOFError("Backend closed stream before Content-Length frame")
            buf += chunk
            lower = buf.lower()
            header_start = lower.find(b"content-length:")
            if header_start >= 0 and b"\r\n\r\n" in buf[header_start:]:
                break
            if len(buf) > 131072:
                raise ValueError(f"No Content-Length frame in first 128 KiB: {buf[:200]!r}")

        framed = buf[header_start:]
        header_part, rest = framed.split(b"\r\n\r\n", 1)
        content_length = None
        for hline in header_part.decode("ascii", errors="ignore").split("\r\n"):
            if hline.lower().startswith("content-length:"):
                try:
                    content_length = int(hline.split(":", 1)[1].strip())
                except ValueError:
                    pass
                break

        if content_length is None or content_length < 0:
            raise ValueError("Invalid MCP frame: missing or bad Content-Length")

        if len(rest) < content_length:
            rest += await asyncio.wait_for(
                reader.readexactly(content_length - len(rest)), timeout=timeout_sec
            )
        body_bytes = rest[:content_length]
        text = body_bytes.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}

    def _get_sandbox_for_envid(self, envid):
        """Get sandbox ID for an envid"""
        envids_config = self.config.get("envids", {})
        if envid in envids_config:
            return envids_config[envid].get("sandbox")
        return None
