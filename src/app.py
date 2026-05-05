#!/usr/bin/env python3
"""
sndbx main application
Orchestrates all services: MCP server, Web UI, VM management
"""

import sys
import os
import logging
import asyncio
import signal

from config import load_config
from sandbox import DockerSandboxManager
from mcp_server import MCPServer
from tools import ToolHandlers
from webui_server import WebUIServer


class SNDBXApp:
    """Main sndbx application - orchestrates all services"""
    
    def __init__(self):
        self.config = None
        self.env_vars = None
        self.root_dir = None
        self.sandbox_manager = None
        self.mcp_server = None
        self.webui_server = None
        self.logger = None
        self._setup_logging()
    
    def _setup_logging(self):
        """Setup logging from config"""
        log_level = os.getenv('LOG_LEVEL', 'info').upper()
        self.logger = logging.getLogger('sndbx')
        self.logger.setLevel(getattr(logging, log_level, logging.INFO))
        
        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(getattr(logging, log_level, logging.INFO))
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)
    
    async def start(self):
        """Start all sndbx services"""
        self.logger.info("=" * 80)
        self.logger.info("Starting sndbx application")
        self.logger.info("=" * 80)
        
        try:
            # Load configuration
            self.root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            config_data = load_config(self.root_dir)
            self.config = config_data['config']
            self.env_vars = config_data['env']
            
            self.logger.info(f"Config: {self.config.get('title')}")
            self.logger.info(f"Instance: {self.config.get('instance')}")
            self.logger.info(f"Root: {self.config.get('root')}")
            
            # Initialize sandbox manager
            self.sandbox_manager = DockerSandboxManager(self.config)
            self.logger.info("Sandbox manager initialized")

            # Build local images used by configured sandboxes when not yet built.
            self._build_startup_images()

            # Start sandboxes that are marked to run at startup.
            self._start_startup_sandboxes()

            self.webui_server = WebUIServer(
                root_dir=self.root_dir,
                config=self.config,
                sandbox_manager=self.sandbox_manager,
            )
            self.logger.info("Web UI server initialized")

            await asyncio.gather(
                self._start_mcp_server(),
                self._start_webui_server(),
            )
            
            self.logger.info("=" * 80)
            self.logger.info("sndbx is running. Press Ctrl+C to stop.")
            self.logger.info("=" * 80)
        
        except Exception as e:
            self.logger.exception(f"Failed to start application: {e}")
            raise

    def _build_startup_images(self):
        """Build local images/<id> images that are not yet present in Docker."""
        items = self.config.get('sandboxes', {}).get('items', {})
        seen: set = set()
        for sandbox_id, sandbox_cfg in items.items():
            image_ref = str(sandbox_cfg.get('image', '')).strip()
            if not image_ref or image_ref in seen:
                continue
            seen.add(image_ref)
            ok, msg = self.sandbox_manager._ensure_image_ready(image_ref)
            if ok:
                self.logger.info("Image ready: '%s'", image_ref)
            else:
                self.logger.warning("Image '%s': %s", image_ref, msg)

    def _start_startup_sandboxes(self):
        """Create/start configured sandboxes with run_at_startup=true."""
        items = self.config.get('sandboxes', {}).get('items', {})
        for sandbox_id, sandbox_cfg in items.items():
            if not sandbox_cfg.get('run_at_startup', False):
                continue

            self.logger.info(f"Sandbox '{sandbox_id}' has run_at_startup=true; ensuring it is running")
            status = self.sandbox_manager.get_status(sandbox_id)
            if status.running:
                self.logger.info(f"Sandbox '{sandbox_id}' is already running")
                continue

            # Try start first (works when container already exists but is stopped).
            ok, out = self.sandbox_manager.start_sandbox(sandbox_id)
            if not ok:
                # If start failed, try create (works when container does not exist).
                ok, out = self.sandbox_manager.create_sandbox(sandbox_id)

            if ok:
                self.logger.info(f"Sandbox '{sandbox_id}' is running")
            else:
                self.logger.error(f"Failed to start sandbox '{sandbox_id}': {out}")

    def _stop_all_sandboxes(self):
        """Stop all known sandboxes during app shutdown."""
        configured = set(self.config.get('sandboxes', {}).get('items', {}).keys())

        discovered = set()
        ok, listed = self.sandbox_manager.list_sandboxes()
        if ok:
            for row in listed:
                sid = row.get('sandbox_id', '')
                if sid:
                    discovered.add(sid)

        all_sandbox_ids = sorted(configured | discovered)
        for sandbox_id in all_sandbox_ids:
            status = self.sandbox_manager.get_status(sandbox_id)
            if not status.running:
                continue
            stop_ok, stop_out = self.sandbox_manager.stop_sandbox(sandbox_id)
            if stop_ok:
                self.logger.info(f"Stopped sandbox '{sandbox_id}' during shutdown")
            else:
                self.logger.warning(f"Could not stop sandbox '{sandbox_id}' during shutdown: {stop_out}")

    async def _start_webui_server(self):
        """Start Web UI server."""
        webui_cfg = self.config.get('webui', {})
        host = webui_cfg.get('bind', '127.0.0.1')
        port = webui_cfg.get('port', 30080)
        self.logger.info(f"Starting Web UI server on {host}:{port}")
        await self.webui_server.start()
    
    async def _start_mcp_server(self):
        """Initialize and start MCP server"""
        mcp_config = self.config.get('mcp', {})
        host = mcp_config.get('bind', '0.0.0.0')
        port = mcp_config.get('port', 30081)
        
        self.logger.info(f"Initializing MCP server on {host}:{port}")
        
        self.mcp_server = MCPServer(host, port, self.config)
        self.mcp_server.sandbox_manager = self.sandbox_manager
        
        # Register tool handlers
        handlers = ToolHandlers(self.sandbox_manager, self.config)
        
        self.mcp_server.register_tool('execute_command', handlers.tool_execute_command)
        self.mcp_server.register_tool('read_file', handlers.tool_read_file)
        self.mcp_server.register_tool('write_file', handlers.tool_write_file)
        self.mcp_server.register_tool('sandbox_status', handlers.tool_sandbox_status)
        self.mcp_server.register_tool('sandbox_start', handlers.tool_sandbox_start)
        self.mcp_server.register_tool('sandbox_stop', handlers.tool_sandbox_stop)
        
        self.logger.info("Starting MCP server...")
        await self.mcp_server.start()
    
    async def stop(self):
        """Stop all services gracefully"""
        self.logger.info("=" * 80)
        self.logger.info("Stopping sndbx application")
        self.logger.info("=" * 80)
        
        if self.mcp_server:
            self.logger.info("Closing MCP server...")
            # TODO: implement graceful shutdown for MCP server

        if self.webui_server:
            self.logger.info("Closing Web UI server...")
            await self.webui_server.stop()

        if self.sandbox_manager and self.config:
            self.logger.info("Stopping sandboxes...")
            self._stop_all_sandboxes()
        
        self.logger.info("sndbx stopped")


async def main():
    """Main entry point"""
    app = SNDBXApp()
    
    # Handle signals
    def signal_handler(sig, frame):
        app.logger.info(f"Received signal {sig}")
        raise KeyboardInterrupt()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        await app.start()
    except KeyboardInterrupt:
        await app.stop()
        sys.exit(0)
    except Exception as e:
        app.logger.exception(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())
