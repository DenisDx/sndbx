"""
MCP (Model Context Protocol) HTTP server for sndbx
Provides streamable HTTP endpoints and legacy SSE/message endpoints.
"""

import asyncio
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import os
from typing import Any, Callable, Dict, List, Optional

from aiohttp import web

from logging_utils import get_logger

logger = get_logger("mcp")


@dataclass
class MCPResponse:
    """MCP tool response payload.

    Output: JSON-RPC response dict.
    Input: response id, result or error.
    """

    id: Any
    result: Optional[Any] = None
    error: Optional[str] = None


class MCPServer:
    """MCP protocol server over HTTP/SSE transport."""

    def __init__(self, host: str, port: int, config: Dict[str, Any]):
        self.host = host
        self.port = port
        self.config = config
        self.tools: Dict[str, Callable] = {}
        self.tool_specs: Dict[str, Dict[str, Any]] = {}
        self.tokens = config.get("mcp", {}).get("auth", {}).get("tokens", [])
        self.sandbox_manager = None
        self.started_event = asyncio.Event()
        self._shutdown_event = asyncio.Event()

        # JSONL request log
        self._request_log_file = None
        if config.get("logging", {}).get("log_mcp_requests", False):
            root = config.get("root", ".")
            log_path = os.path.join(root, "logs", "mcp_requests.jsonl")
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            try:
                self._request_log_file = open(log_path, "a", encoding="utf-8", buffering=1)  # line-buffered
                logger.info("MCP request log: %s", log_path)
            except OSError as exc:
                logger.error("Cannot open MCP request log %s: %s", log_path, exc)

        self._app = web.Application()
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

        # Legacy SSE sessions keyed by session id.
        self._sse_queues: Dict[str, asyncio.Queue] = {}

        self._register_routes()

    def _published_tools(self) -> List[str]:
        """Return sorted published tool names.

        Output: list of tool names.
        Input: current tool registry.
        """

        return sorted(self.tools.keys())

    def _log_published_tools(self, reason: str) -> None:
        """Log current published tools snapshot.

        Output: None.
        Input: reason label for log entry.
        """

        names = self._published_tools()
        logger.info("Published tools (%s): count=%d names=%s", reason, len(names), ",".join(names) if names else "-")

    def register_tool(
        self,
        name: str,
        handler: Callable,
        description: Optional[str] = None,
        input_schema: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Register a tool handler.

        Output: None.
        Input: tool name and async handler(params, envid, token).
        """

        self.tools[name] = handler
        self.tool_specs[name] = {
            "name": name,
            "description": description or "",
            "inputSchema": input_schema or {"type": "object", "properties": {}, "required": []},
        }
        logger.info("Registered tool: %s", name)
        self._log_published_tools("register")

    def _tools_list_response(self, request_id: Any) -> Dict[str, Any]:
        """Build JSON-RPC tools/list response with tool metadata.

        Output: JSON-RPC response dict with full MCP tool descriptors.
        Input: request id.
        """

        tools = [self.tool_specs[name] for name in sorted(self.tool_specs.keys())]
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": tools},
        }

    def validate_token(self, token: Optional[str]) -> bool:
        """Validate authentication token.

        Output: bool.
        Input: token string.
        """

        if not token:
            return False
        return token in self.tokens

    def resolve_envid(self, token: str, envid: Optional[str]) -> Optional[str]:
        """Resolve sandbox environment id for token.

        Output: resolved envid or None.
        Input: token and optional requested envid.
        """

        users = self.config.get("users", {}).get("items", [])

        if not envid:
            for user in users:
                if user.get("token") == token:
                    envids = user.get("envids", [])
                    return envids[0] if envids else None
            return None

        for user in users:
            if user.get("token") == token and envid in user.get("envids", []):
                return envid
        return None

    def _register_routes(self) -> None:
        """Register HTTP routes for streamable and legacy MCP transports.

        Output: None.
        Input: internal app router.
        """

        self._app.router.add_post("/", self.handle_streamable_http)
        self._app.router.add_post("/mcp", self.handle_streamable_http)
        self._app.router.add_post("/mcp/v1", self.handle_streamable_http)
        self._app.router.add_get("/sse", self.handle_legacy_sse)
        self._app.router.add_post("/messages", self.handle_legacy_messages)

        @web.middleware
        async def request_logging_middleware(request: web.Request, handler):
            """Log HTTP MCP request and response metadata.

            Output: handler response.
            Input: request and next handler.
            """

            peer = request.remote or "unknown"
            logger.info("Client request: peer=%s method=%s path=%s", peer, request.method, request.path)

            if request.method == "OPTIONS":
                response = web.Response(status=204)
                for key, value in self._cors_headers(request).items():
                    response.headers[key] = value
                logger.info("Client response: peer=%s method=%s path=%s status=%s", peer, request.method, request.path, response.status)
                return response

            try:
                response = await handler(request)
                for key, value in self._cors_headers(request).items():
                    response.headers.setdefault(key, value)
                logger.info("Client response: peer=%s method=%s path=%s status=%s", peer, request.method, request.path, response.status)
                return response
            except web.HTTPMethodNotAllowed:
                # Browser preflight may hit paths with only POST/GET handlers.
                if request.method == "OPTIONS":
                    response = web.Response(status=204)
                    for key, value in self._cors_headers(request).items():
                        response.headers[key] = value
                    logger.info("Client response: peer=%s method=%s path=%s status=%s", peer, request.method, request.path, response.status)
                    return response
                logger.exception("Client request failed: peer=%s method=%s path=%s", peer, request.method, request.path)
                raise
            except Exception:
                logger.exception("Client request failed: peer=%s method=%s path=%s", peer, request.method, request.path)
                raise

        self._app.middlewares.append(request_logging_middleware)

    def _log_rpc(self, type_: str, data: Any) -> None:
        """Append one JSONL entry to the request log if enabled.

        Output: None.
        Input: entry type ('request'/'response') and JSON-serialisable data.
        """
        if self._request_log_file is None:
            return
        try:
            entry = {"type": type_, "ts": datetime.now(timezone.utc).isoformat(), "data": data}
            self._request_log_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning("Failed to write MCP request log entry: %s", exc)

    def _cors_headers(self, request: web.Request) -> Dict[str, str]:
        """Build CORS headers for browser-based MCP calls.

        Output: HTTP headers map.
        Input: incoming request with optional Origin header.
        """

        origin = (request.headers.get("Origin") or "").strip()
        allow_origin = origin if origin else "*"
        return {
            "Access-Control-Allow-Origin": allow_origin,
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type,Authorization,Accept,X-MCP-Stream",
            "Access-Control-Max-Age": "600",
            "Vary": "Origin",
        }

    @staticmethod
    def _json_error(req_id: Any, message: str) -> Dict[str, Any]:
        """Build JSON-RPC error response.

        Output: JSON response dict.
        Input: request id and error text.
        """

        return asdict(MCPResponse(id=req_id, error=message))

    def _extract_token(self, req_json: Dict[str, Any], request: web.Request) -> Optional[str]:
        """Extract auth token from payload or Authorization header.

        Output: token string or None.
        Input: JSON-RPC body and HTTP request.
        """

        token = req_json.get("token")
        if isinstance(token, str) and token.strip():
            return token.strip()

        auth_obj = req_json.get("auth")
        if isinstance(auth_obj, dict):
            auth_token = auth_obj.get("token")
            if isinstance(auth_token, str) and auth_token.strip():
                return auth_token.strip()

        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            return auth_header.split(" ", 1)[1].strip() or None

        return None

    async def handle_rpc(self, req_json: Dict[str, Any], request: web.Request) -> Dict[str, Any]:
        """Handle a single JSON-RPC request.

        Output: JSON-RPC response dict.
        Input: JSON-RPC request object and HTTP request.
        """

        request_id = req_json.get("id", "unknown")
        method = req_json.get("method")
        params = req_json.get("params", {})
        envid = req_json.get("envid")
        token = self._extract_token(req_json, request)
        self._log_rpc("request", req_json)
        handler = self.tools.get(method) if isinstance(method, str) else None
        if isinstance(method, str) and method in {"tools/list", "resources/list", "prompts/list"}:
            call_target = f"builtin:{method}"
        elif handler is not None:
            call_target = f"tool:{getattr(handler, '__name__', '<anonymous>')}"
        else:
            call_target = "handler:not_found"
        logger.info(
            "RPC request: peer=%s id=%s method=%s has_params=%s call_target=%s envid=%s",
            request.remote or "unknown",
            request_id,
            method,
            isinstance(params, dict) and bool(params),
            call_target,
            envid,
        )

        if not isinstance(method, str) or not method.strip():
            response = self._json_error(request_id, "Invalid or missing method")
            logger.info("RPC response: id=%s method=%s ok=false error=%s", request_id, method, response.get("error"))
            self._log_rpc("response", response)
            return response

        if method == "tools/list":
            response = self._tools_list_response(request_id)
            logger.info("RPC response: id=%s method=%s ok=true tools_count=%d", request_id, method, len(self.tools))
            self._log_rpc("response", response)
            return response

        if method == "resources/list":
            response = {"id": request_id, "result": {"resources": []}, "error": None}
            logger.info("RPC response: id=%s method=%s ok=true", request_id, method)
            self._log_rpc("response", response)
            return response

        if method == "prompts/list":
            response = {"id": request_id, "result": {"prompts": []}, "error": None}
            logger.info("RPC response: id=%s method=%s ok=true", request_id, method)
            self._log_rpc("response", response)
            return response

        if not self.validate_token(token):
            response = self._json_error(request_id, "Invalid or missing token")
            logger.info("RPC response: id=%s method=%s ok=false error=%s", request_id, method, response.get("error"))
            self._log_rpc("response", response)
            return response

        resolved_envid = self.resolve_envid(token, envid)
        logger.info("Request received: id=%s method=%s envid=%s", request_id, method, resolved_envid or envid)
        if not resolved_envid:
            response = self._json_error(request_id, "No access to requested envid or sandbox")
            logger.info("RPC response: id=%s method=%s ok=false error=%s", request_id, method, response.get("error"))
            self._log_rpc("response", response)
            return response

        handler = self.tools.get(method)
        if handler is None:
            response = self._json_error(request_id, f"Unknown tool: {method}")
            logger.info("RPC response: id=%s method=%s ok=false error=%s", request_id, method, response.get("error"))
            self._log_rpc("response", response)
            return response

        try:
            result = await handler(params if isinstance(params, dict) else {}, resolved_envid, token)
            logger.info("Request completed: id=%s method=%s ok=%s", request_id, method, "error" not in (result or {}))
            response = asdict(MCPResponse(id=request_id, result=result))
            logger.info("RPC response: id=%s method=%s ok=true", request_id, method)
            self._log_rpc("response", response)
            return response
        except Exception as exc:
            logger.exception("Error handling request id=%s method=%s", request_id, method)
            response = self._json_error(request_id, f"Internal error: {exc}")
            logger.info("RPC response: id=%s method=%s ok=false error=%s", request_id, method, response.get("error"))
            self._log_rpc("response", response)
            return response

    async def _parse_http_requests(self, request: web.Request) -> tuple[List[Dict[str, Any]], Optional[str]]:
        """Parse one or many JSON-RPC requests from HTTP body.

        Output: list of request objects and optional parse error.
        Input: aiohttp request.
        """

        raw = await request.text()
        if not raw.strip():
            return [], "Request body is empty"

        content_type = (request.content_type or "").lower()

        if "application/json" in content_type:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return [], "Invalid JSON"

            if isinstance(payload, dict):
                return [payload], None
            if isinstance(payload, list):
                valid = [row for row in payload if isinstance(row, dict)]
                if not valid:
                    return [], "JSON array must contain objects"
                return valid, None
            return [], "JSON payload must be object or array"

        # Fallback: NDJSON or plain lines.
        rows: List[Dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                return [], "Invalid JSON line in request body"
            if not isinstance(item, dict):
                return [], "Each JSON line must be an object"
            rows.append(item)

        if not rows:
            return [], "Request body is empty"
        return rows, None

    async def _write_sse_event(self, response: web.StreamResponse, event: str, data: Dict[str, Any]) -> None:
        """Write one SSE event frame.

        Output: None.
        Input: stream response, event name, JSON payload.
        """

        payload = json.dumps(data, ensure_ascii=True)
        frame = f"event: {event}\ndata: {payload}\n\n"
        await response.write(frame.encode("utf-8"))

    async def handle_streamable_http(self, request: web.Request) -> web.StreamResponse:
        """Handle streamable HTTP MCP endpoint for POST /, /mcp and /mcp/v1.

        Output: JSON response or SSE stream.
        Input: HTTP POST with one or many JSON-RPC requests.
        """

        requests, parse_error = await self._parse_http_requests(request)
        if parse_error:
            return web.json_response(self._json_error("error", parse_error), status=400)

        responses: List[Dict[str, Any]] = []
        for row in requests:
            responses.append(await self.handle_rpc(row, request))

        wants_stream = (
            request.query.get("stream", "").lower() in {"1", "true", "yes"}
            or "text/event-stream" in request.headers.get("Accept", "").lower()
            or request.headers.get("X-MCP-Stream", "").lower() in {"1", "true", "yes"}
        )

        if wants_stream:
            stream = web.StreamResponse(
                status=200,
                headers={
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    **self._cors_headers(request),
                },
            )
            await stream.prepare(request)

            for item in responses:
                await self._write_sse_event(stream, "message", item)

            await self._write_sse_event(stream, "done", {"count": len(responses)})
            await stream.write_eof()
            return stream

        if len(responses) == 1:
            return web.json_response(responses[0])
        return web.json_response(responses)

    async def handle_legacy_sse(self, request: web.Request) -> web.StreamResponse:
        """Handle legacy GET /sse endpoint.

        Output: long-lived SSE connection.
        Input: optional session_id query argument.
        """

        session_id = (request.query.get("session_id") or "").strip() or str(uuid.uuid4())
        queue: asyncio.Queue = asyncio.Queue()
        self._sse_queues[session_id] = queue
        logger.info("SSE connected: session_id=%s peer=%s", session_id, request.remote or "unknown")

        stream = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                **self._cors_headers(request),
            },
        )
        await stream.prepare(request)

        await self._write_sse_event(stream, "ready", {"session_id": session_id})

        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=20)
                    await self._write_sse_event(stream, "message", item)
                except asyncio.TimeoutError:
                    await self._write_sse_event(stream, "ping", {"ok": True})
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self._sse_queues.pop(session_id, None)
            logger.info("SSE disconnected: session_id=%s peer=%s", session_id, request.remote or "unknown")

        return stream

    async def handle_legacy_messages(self, request: web.Request) -> web.Response:
        """Handle legacy POST /messages endpoint.

        Output: JSON-RPC response payload.
        Input: JSON-RPC request body and optional session_id.
        """

        try:
            body = await request.json()
        except Exception:
            return web.json_response(self._json_error("error", "Invalid JSON"), status=400)

        if not isinstance(body, dict):
            return web.json_response(self._json_error("error", "JSON body must be object"), status=400)

        session_id = (request.query.get("session_id") or body.get("session_id") or "").strip()
        logger.info("Legacy message: peer=%s session_id=%s", request.remote or "unknown", session_id or "-")
        result = await self.handle_rpc(body, request)

        if session_id and session_id in self._sse_queues:
            try:
                self._sse_queues[session_id].put_nowait(result)
                logger.info("Legacy message forwarded to SSE: session_id=%s", session_id)
            except Exception:
                pass

        return web.json_response(result)

    async def start(self):
        """Start HTTP MCP server.

        Output: never returns until stop or cancellation.
        Input: configured bind host and port.
        """

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=self.host, port=self.port)
        await self._site.start()
        logger.info("MCP HTTP server listening on %s:%s", self.host, self.port)
        self._log_published_tools("startup")
        self.started_event.set()
        self._shutdown_event.clear()

        # Keep task alive until stop() signals shutdown.
        await self._shutdown_event.wait()

    async def stop(self):
        """Stop HTTP MCP server gracefully.

        Output: None.
        Input: internal runner/site state.
        """

        if self._site is not None:
            await self._site.stop()
            self._site = None

        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

        self._shutdown_event.set()

        if self._request_log_file is not None:
            try:
                self._request_log_file.close()
            except OSError:
                pass
            self._request_log_file = None
