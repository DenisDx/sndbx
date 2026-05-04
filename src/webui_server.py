"""sndbx Web UI backend (FastAPI).

Provides:
- Login/logout with file-based sessions
- Dashboard status and sandbox actions
- Placeholder pages for settings/console in frontend
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import pty
import signal
import secrets
import subprocess
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


def _utc_now() -> datetime:
    """Return current UTC time."""
    return datetime.now(timezone.utc)


def _constant_eq(a: str, b: str) -> bool:
    """Constant-time comparison for credentials/token checks."""
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


class SessionStore:
    """File-based session storage in sessions/<sha256(token)>.json."""

    def __init__(self, root_dir: Path, ttl_seconds: int):
        self.root_dir = root_dir
        self.ttl_seconds = max(60, int(ttl_seconds or 86400))
        self.sessions_dir = self.root_dir / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, token: str) -> Path:
        """Map token to session file path."""
        digest = hashlib.sha256(token.encode()).hexdigest()
        return self.sessions_dir / f"{digest}.json"

    def cleanup_expired(self) -> None:
        """Delete expired sessions from storage."""
        now = _utc_now()
        for path in self.sessions_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                exp = datetime.fromisoformat(data.get("expires_at", ""))
                if exp <= now:
                    path.unlink(missing_ok=True)
            except Exception:
                path.unlink(missing_ok=True)

    def create(self, login: str, permissions: List[str]) -> str:
        """Create new session and return token."""
        self.cleanup_expired()
        token = secrets.token_hex(32)
        now = _utc_now()
        payload = {
            "login": login,
            "permissions": permissions,
            "created_at": now.isoformat(),
            "expires_at": (now.timestamp() + self.ttl_seconds),
        }
        payload["expires_at"] = datetime.fromtimestamp(payload["expires_at"], tz=timezone.utc).isoformat()
        self._session_path(token).write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        return token

    def get(self, token: str) -> Optional[Dict[str, Any]]:
        """Return session payload for valid token, otherwise None."""
        if not token:
            return None

        path = self._session_path(token)
        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            exp = datetime.fromisoformat(data.get("expires_at", ""))
            if exp <= _utc_now():
                path.unlink(missing_ok=True)
                return None
            return data
        except Exception:
            path.unlink(missing_ok=True)
            return None

    def delete(self, token: str) -> None:
        """Delete session by token if exists."""
        if token:
            self._session_path(token).unlink(missing_ok=True)


class SSHPortPool:
    """Simple SSH port reservation pool for dashboard actions (v1)."""

    def __init__(self, root_dir: Path, port_range: List[int]):
        self.root_dir = root_dir
        self.data_dir = root_dir / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "ssh_allocations.json"

        start = int(port_range[0]) if len(port_range) > 0 else 30200
        end = int(port_range[1]) if len(port_range) > 1 else 30210
        if end < start:
            start, end = end, start
        self.start = start
        self.end = end

    def _load(self) -> Dict[str, Dict[str, Any]]:
        """Load allocations map from disk."""
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save(self, data: Dict[str, Dict[str, Any]]) -> None:
        """Persist allocations map to disk."""
        self.path.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")

    def get(self, sandbox_id: str) -> Optional[int]:
        """Get reserved SSH port for sandbox if present."""
        data = self._load()
        row = data.get(sandbox_id)
        if not row:
            return None
        try:
            return int(row.get("port"))
        except Exception:
            return None

    def reserve(self, sandbox_id: str) -> tuple[bool, Optional[int], Optional[str]]:
        """Reserve free port for sandbox, return (ok, port, error)."""
        data = self._load()

        current = data.get(sandbox_id)
        if current and "port" in current:
            return True, int(current["port"]), None

        used = {int(v.get("port")) for v in data.values() if isinstance(v, dict) and "port" in v}

        for port in range(self.start, self.end + 1):
            if port in used:
                continue
            data[sandbox_id] = {
                "port": port,
                "allocated_at": _utc_now().isoformat(),
                "state": "reserved",
            }
            self._save(data)
            return True, port, None

        return False, None, "no_ports_available"

    def release(self, sandbox_id: str) -> bool:
        """Release reserved port for sandbox."""
        data = self._load()
        if sandbox_id in data:
            del data[sandbox_id]
            self._save(data)
            return True
        return False


class ActionRequest(BaseModel):
    """Dashboard action payload."""

    action: str


class WebUIServer:
    """Web UI server wrapper around FastAPI/uvicorn."""

    def __init__(self, root_dir: Path, config: Dict[str, Any], sandbox_manager: Any):
        self.root_dir = Path(root_dir)
        self.config = config
        self.sandbox_manager = sandbox_manager

        webui_cfg = config.get("webui", {})
        self.host = str(webui_cfg.get("bind", "127.0.0.1"))
        self.port = int(webui_cfg.get("port", 30080))

        auth_cfg = webui_cfg.get("auth", {})
        self.users = list(auth_cfg.get("users", []))
        self.session_ttl = int(auth_cfg.get("session_ttl", 86400))

        ssh_cfg = config.get("ssh", {})
        self.ssh_pool = SSHPortPool(self.root_dir, ssh_cfg.get("port_range", [30200, 30210]))
        self.sessions = SessionStore(self.root_dir, self.session_ttl)

        self.app = self._create_app()
        self._server: Optional[uvicorn.Server] = None

    def _find_user(self, login: str, password: str) -> Optional[Dict[str, Any]]:
        """Return user dict if credentials match."""
        for user in self.users:
            if _constant_eq(str(user.get("login", "")), login) and _constant_eq(str(user.get("password", "")), password):
                return user
        return None

    async def _require_session(self, sndbx_session: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
        """Require valid session cookie."""
        session = self.sessions.get(sndbx_session or "")
        if not session:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
        return session

    def _health_checks(self) -> Dict[str, Any]:
        """Collect lightweight system health info."""
        checks: Dict[str, Any] = {
            "docker": {"ok": False, "detail": "unavailable"},
            "kata": {"ok": False, "detail": "unavailable"},
            "webui": {"ok": True, "detail": f"listening on {self.host}:{self.port}"},
        }

        try:
            docker = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=5)
            checks["docker"] = {
                "ok": docker.returncode == 0,
                "detail": "ok" if docker.returncode == 0 else (docker.stderr.strip() or "docker info failed"),
            }
        except Exception as exc:
            checks["docker"] = {"ok": False, "detail": str(exc)}

        try:
            kata = subprocess.run(["kata-runtime", "--version"], capture_output=True, text=True, timeout=5)
            detail = "ok"
            if kata.returncode != 0:
                detail = kata.stderr.strip() or "kata-runtime check failed"
            checks["kata"] = {"ok": kata.returncode == 0, "detail": detail}
        except Exception as exc:
            checks["kata"] = {"ok": False, "detail": str(exc)}

        return checks

    def _containers_view(self) -> List[Dict[str, Any]]:
        """Return dashboard sandbox list with runtime state, SSH port and startup flag."""
        configured = self.config.get("sandboxes", {}).get("items", {})

        ok, items = self.sandbox_manager.list_sandboxes()
        discovered = items if ok else []
        discovered_map: Dict[str, Dict[str, Any]] = {}
        for row in discovered:
            sid = row.get("sandbox_id", "")
            if sid:
                discovered_map[sid] = row

        out: List[Dict[str, Any]] = []

        # Show all configured sandboxes, even if container is not created yet.
        for sandbox_id, sandbox_cfg in configured.items():
            row = discovered_map.get(sandbox_id, {})
            out.append({
                "sandbox_id": sandbox_id,
                "image": row.get("image") or sandbox_cfg.get("image", ""),
                "status": row.get("status") or "not created",
                "ports": row.get("ports", ""),
                "ssh_port": self.ssh_pool.get(sandbox_id),
                "run_at_startup": bool(sandbox_cfg.get("run_at_startup", False)),
            })

        # Include unmanaged/discovered containers for visibility.
        configured_ids = set(configured.keys())
        for sandbox_id, row in discovered_map.items():
            if sandbox_id in configured_ids:
                continue
            out.append({
                "sandbox_id": sandbox_id,
                "image": row.get("image", ""),
                "status": row.get("status", ""),
                "ports": row.get("ports", ""),
                "ssh_port": self.ssh_pool.get(sandbox_id),
                "run_at_startup": False,
            })

        return out

    def _ensure_sandbox_running(self, sandbox_id: str) -> bool:
        """Ensure sandbox container exists and is running."""
        status_info = self.sandbox_manager.get_status(sandbox_id)
        if status_info.running:
            return True

        ok, _ = self.sandbox_manager.start_sandbox(sandbox_id)
        if ok:
            return True

        ok, _ = self.sandbox_manager.create_sandbox(sandbox_id)
        return ok

    async def _restart_service_soon(self, delay_seconds: float = 0.4) -> None:
        """Terminate current process after response so systemd can restart service."""
        await asyncio.sleep(max(0.05, float(delay_seconds or 0.0)))
        os.kill(os.getpid(), signal.SIGTERM)

    def _create_app(self) -> FastAPI:
        """Build FastAPI app with auth, status and action endpoints."""
        app = FastAPI(title="sndbx WebUI", docs_url=None, redoc_url=None)

        frontend_dir = self.root_dir / "src" / "webui" / "frontend"

        @app.post("/api/auth/login")
        async def login(request: Request, response: Response):
            self.sessions.cleanup_expired()
            body = await request.json()
            login_val = str(body.get("login", ""))
            password_val = str(body.get("password", ""))

            user = self._find_user(login_val, password_val)
            if not user:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

            token = self.sessions.create(login=str(user.get("login", "")), permissions=list(user.get("permissions", [])))
            response.set_cookie(
                "sndbx_session",
                token,
                max_age=self.session_ttl,
                httponly=True,
                samesite="strict",
                secure=bool(request.url.scheme == "https"),
            )
            return {"ok": True, "token": token}

        @app.get("/api/auth/me")
        async def auth_me(session: Dict[str, Any] = Depends(self._require_session)):
            return {
                "ok": True,
                "login": session.get("login", ""),
                "permissions": session.get("permissions", []),
            }

        @app.post("/api/auth/logout")
        async def logout(response: Response, sndbx_session: Optional[str] = Cookie(default=None)):
            self.sessions.delete(sndbx_session or "")
            response.delete_cookie("sndbx_session")
            return {"ok": True}

        @app.get("/api/status")
        async def status_endpoint(session: Dict[str, Any] = Depends(self._require_session)):
            return {
                "health": self._health_checks(),
                "containers": self._containers_view(),
                "session": {
                    "login": session.get("login", ""),
                },
            }

        @app.post("/api/sandbox/{sandbox_id}/action")
        async def sandbox_action(
            sandbox_id: str,
            body: ActionRequest,
            session: Dict[str, Any] = Depends(self._require_session),
        ):
            action = body.action.strip().lower()

            if action == "start":
                ok, out = self.sandbox_manager.start_sandbox(sandbox_id)
                if not ok:
                    ok, out = self.sandbox_manager.create_sandbox(sandbox_id)
                return {"ok": ok, "message": out}

            if action == "stop":
                ok, out = self.sandbox_manager.stop_sandbox(sandbox_id)
                return {"ok": ok, "message": out}

            if action == "restart":
                ok, out = self.sandbox_manager.restart_sandbox(sandbox_id)
                return {"ok": ok, "message": out}

            if action == "ssh_open":
                ok, port, err = self.ssh_pool.reserve(sandbox_id)
                return {"ok": ok, "ssh_port": port, "error": err}

            if action == "ssh_close":
                ok = self.ssh_pool.release(sandbox_id)
                return {"ok": ok}

            raise HTTPException(status_code=400, detail="Unknown action")

        @app.post("/api/service/restart")
        async def service_restart(session: Dict[str, Any] = Depends(self._require_session)):
            asyncio.create_task(self._restart_service_soon())
            return {"ok": True, "message": "Service restart scheduled"}

        @app.websocket("/ws/console/{sandbox_id}")
        async def ws_console(websocket: WebSocket, sandbox_id: str):
            """Interactive shell bridge to sandbox container via PTY and docker exec."""
            token = websocket.cookies.get("sndbx_session", "")
            session = self.sessions.get(token)
            if not session:
                await websocket.close(code=4001)
                return

            # Ensure sandbox is running before opening terminal session.
            if not self._ensure_sandbox_running(sandbox_id):
                await websocket.accept()
                await websocket.send_text("\r\n[sndbx] Unable to start sandbox for console session.\r\n")
                await websocket.close(code=1011)
                return

            await websocket.accept()

            master_fd, slave_fd = pty.openpty()
            proc = subprocess.Popen(
                ["docker", "exec", "-i", f"sndbx-{sandbox_id}", "bash"],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                close_fds=True,
            )
            os.close(slave_fd)

            loop = asyncio.get_running_loop()
            output_queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue()

            def _on_fd_readable() -> None:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    data = b""
                if data:
                    output_queue.put_nowait(data)
                else:
                    output_queue.put_nowait(None)

            loop.add_reader(master_fd, _on_fd_readable)

            async def _forward_output() -> None:
                while True:
                    chunk = await output_queue.get()
                    if chunk is None:
                        break
                    await websocket.send_text(chunk.decode("utf-8", errors="replace"))

            async def _forward_input() -> None:
                while True:
                    message = await websocket.receive_text()
                    os.write(master_fd, message.encode("utf-8", errors="replace"))

            try:
                await websocket.send_text(f"\r\n[sndbx] Console connected to sandbox '{sandbox_id}'.\r\n")
                await asyncio.gather(_forward_output(), _forward_input())
            except WebSocketDisconnect:
                pass
            except Exception:
                pass
            finally:
                try:
                    loop.remove_reader(master_fd)
                except Exception:
                    pass
                try:
                    os.close(master_fd)
                except Exception:
                    pass
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        proc.kill()

        # Serve frontend last so API routes take priority.
        app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")

        return app

    async def start(self) -> None:
        """Run uvicorn server."""
        cfg = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="info")
        self._server = uvicorn.Server(cfg)
        await self._server.serve()

    async def stop(self) -> None:
        """Request graceful shutdown."""
        if self._server is not None:
            self._server.should_exit = True
