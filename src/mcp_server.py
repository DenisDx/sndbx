"""
MCP (Model Context Protocol) server for sndbx
Handles tool calls for sandbox management
"""

import asyncio
import json
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, asdict

from logging_utils import get_logger

logger = get_logger("mcp")


@dataclass
class MCPRequest:
    """MCP tool request"""
    id: str
    method: str
    params: Dict[str, Any]
    token: Optional[str] = None
    envid: Optional[str] = None


@dataclass
class MCPResponse:
    """MCP tool response"""
    id: str
    result: Optional[Any] = None
    error: Optional[str] = None


class MCPServer:
    """MCP protocol server"""
    
    def __init__(self, host: str, port: int, config: Dict[str, Any]):
        self.host = host
        self.port = port
        self.config = config
        self.tools: Dict[str, Callable] = {}
        self.tokens = config.get('mcp', {}).get('auth', {}).get('tokens', [])
        self.sandbox_manager = None
        self.started_event = asyncio.Event()
    
    def register_tool(self, name: str, handler: Callable) -> None:
        """Register a tool handler"""
        self.tools[name] = handler
        logger.info("Registered tool: %s", name)
    
    def validate_token(self, token: Optional[str]) -> bool:
        """Validate authentication token"""
        if not token:
            return False
        return token in self.tokens
    
    def resolve_envid(self, token: str, envid: Optional[str]) -> Optional[str]:
        """Resolve which sandbox to use based on token and envid"""
        if not envid:
            # Use first available envid for this token
            users = self.config.get('users', {}).get('items', [])
            for user in users:
                if user.get('token') == token:
                    envids = user.get('envids', [])
                    return envids[0] if envids else None
        else:
            # Verify token has access to this envid
            users = self.config.get('users', {}).get('items', [])
            for user in users:
                if user.get('token') == token:
                    if envid in user.get('envids', []):
                        return envid
        return None
    
    async def handle_request(self, data: str) -> str:
        """Handle a single MCP request"""
        try:
            req_json = json.loads(data)
            
            # Parse request
            request_id = req_json.get('id', 'unknown')
            method = req_json.get('method')
            params = req_json.get('params', {})
            token = req_json.get('token')
            envid = req_json.get('envid')
            
            # Validate token
            if not self.validate_token(token):
                return json.dumps(asdict(MCPResponse(
                    id=request_id,
                    error="Invalid or missing token"
                )))
            
            # Resolve envid/sandbox
            resolved_envid = self.resolve_envid(token, envid)
            logger.info("Request received: id=%s method=%s envid=%s", request_id, method, resolved_envid or envid)
            if not resolved_envid:
                return json.dumps(asdict(MCPResponse(
                    id=request_id,
                    error="No access to requested envid or sandbox"
                )))
            
            # Look up and call tool
            if method not in self.tools:
                return json.dumps(asdict(MCPResponse(
                    id=request_id,
                    error=f"Unknown tool: {method}"
                )))
            
            handler = self.tools[method]
            result = await handler(params, resolved_envid, token)
            logger.info("Request completed: id=%s method=%s ok=%s", request_id, method, "error" not in (result or {}))
            
            return json.dumps(asdict(MCPResponse(
                id=request_id,
                result=result
            )))
        
        except json.JSONDecodeError:
            return json.dumps(asdict(MCPResponse(
                id="error",
                error="Invalid JSON"
            )))
        except Exception as e:
            logger.exception("Error handling MCP request")
            return json.dumps(asdict(MCPResponse(
                id="error",
                error=f"Internal error: {str(e)}"
            )))
    
    async def handle_client(self, reader, writer):
        """Handle a client connection"""
        addr = writer.get_extra_info('peername')
        logger.info("Client connected: %s", addr)
        
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                
                response = await self.handle_request(line.decode('utf-8'))
                writer.write((response + '\n').encode('utf-8'))
                await writer.drain()
        
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception("Error handling client %s", addr)
        finally:
            writer.close()
            await writer.wait_closed()
            logger.info("Client disconnected: %s", addr)
    
    async def start(self):
        """Start the MCP server"""
        server = await asyncio.start_server(
            self.handle_client,
            self.host,
            self.port
        )
        
        addr = server.sockets[0].getsockname()
        logger.info("MCP server listening on %s:%s", addr[0], addr[1])
        self.started_event.set()
        
        async with server:
            await server.serve_forever()
