#!/usr/bin/env python3
"""
sndbx main application
Orchestrates all services: MCP server, Web UI, VM management
"""

import sys
import os
import asyncio
import signal
import threading

from config import load_config
from logging_utils import configure_logging, get_logger
from sandbox import DockerSandboxManager
from mcp_server import MCPServer
from tools import ToolHandlers, register_all_tools
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
        self.logger = get_logger('core')
        self._install_exception_hooks()

    def _install_exception_hooks(self):
        """Install global exception hooks to route unhandled errors to core log."""

        def _sys_excepthook(exc_type, exc_value, exc_traceback):
            if issubclass(exc_type, KeyboardInterrupt):
                return
            self.logger.error("Unhandled Python exception", exc_info=(exc_type, exc_value, exc_traceback))

        def _thread_excepthook(args):
            if args.exc_type and issubclass(args.exc_type, KeyboardInterrupt):
                return
            thread_name = args.thread.name if args.thread is not None else "unknown"
            self.logger.error(
                "Unhandled thread exception in %s",
                thread_name,
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )

        sys.excepthook = _sys_excepthook
        if hasattr(threading, "excepthook"):
            threading.excepthook = _thread_excepthook
    
    def _setup_logging(self):
        """Setup logging from config"""
        if self.config and self.root_dir:
            configure_logging(self.config, self.root_dir)
        self.logger = get_logger('core')
        self._install_exception_hooks()
    
    async def start(self):
        """Start all sndbx services"""
        self.logger.info("Service startup initiated")

        try:
            # Load configuration
            self.root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            config_data = load_config(self.root_dir)
            self.config = config_data['config']
            self.env_vars = config_data['env']
            self._setup_logging()
            
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

            self._init_mcp_server()

            self.webui_server = WebUIServer(
                root_dir=self.root_dir,
                config=self.config,
                sandbox_manager=self.sandbox_manager,
            )
            self.logger.info("Web UI server initialized")

            mcp_task = asyncio.create_task(self._start_mcp_server())
            webui_task = asyncio.create_task(self._start_webui_server())
            await asyncio.gather(
                self.mcp_server.started_event.wait(),
                self.webui_server.started_event.wait(),
            )
            self.logger.info("Service startup completed")

            await asyncio.gather(mcp_task, webui_task)
        
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
    
    def _init_mcp_server(self):
        """Initialize MCP server and register handlers."""
        mcp_config = self.config.get('mcp', {})
        host = mcp_config.get('bind', '0.0.0.0')
        port = mcp_config.get('port', 30081)

        self.logger.info(f"Initializing MCP server on {host}:{port}")

        self.mcp_server = MCPServer(host, port, self.config)
        self.mcp_server.sandbox_manager = self.sandbox_manager

        handlers = ToolHandlers(self.sandbox_manager, self.config)
        register_all_tools(self.mcp_server, handlers)

    async def _start_mcp_server(self):
        """Start initialized MCP server."""
        self.logger.info("Starting MCP server...")
        await self.mcp_server.start()
    
    async def stop(self):
        """Stop all services gracefully"""
        if self.mcp_server:
            self.logger.info("Closing MCP server...")
            await self.mcp_server.stop()

        if self.webui_server:
            self.logger.info("Closing Web UI server...")
            await self.webui_server.stop()

        if self.sandbox_manager and self.config:
            self.logger.info("Stopping sandboxes...")
            self._stop_all_sandboxes()
        
        self.logger.info("Service shutdown completed")


async def main() -> int:
    """Main entry point."""
    app = SNDBXApp()
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    shutdown_started = False

    # Handle signals without raising KeyboardInterrupt inside running tasks.
    def signal_handler(sig, frame):
        nonlocal shutdown_started
        sig_name = signal.Signals(sig).name if sig in signal.Signals._value2member_map_ else str(sig)
        reason = os.environ.pop("SNDBX_STOP_REASON", f"received {sig_name}")
        if not shutdown_started:
            shutdown_started = True
            app.logger.info("Service shutdown initiated: %s", reason)
        loop.call_soon_threadsafe(stop_event.set)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    def loop_exception_handler(loop_obj, context):
        """Log unhandled asyncio exceptions through core logger."""
        exc = context.get("exception")
        msg = context.get("message", "Unhandled asyncio exception")
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            return
        if exc is not None:
            app.logger.error("%s", msg, exc_info=(type(exc), exc, exc.__traceback__))
            return
        app.logger.error("%s", msg)

    loop.set_exception_handler(loop_exception_handler)

    start_task = asyncio.create_task(app.start(), name="sndbx-start")
    stop_task = asyncio.create_task(stop_event.wait(), name="sndbx-stop-wait")

    try:
        done, _ = await asyncio.wait({start_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)

        if start_task in done:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
            await start_task
            return 0

        await app.stop()

        if not start_task.done():
            try:
                await asyncio.wait_for(start_task, timeout=5.0)
            except asyncio.TimeoutError:
                start_task.cancel()

        await asyncio.gather(start_task, return_exceptions=True)
        return 0
    except Exception as e:
        app.logger.exception(f"Fatal error: {e}")
        return 1
    finally:
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
