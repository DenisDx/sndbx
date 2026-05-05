"""sndbx Web UI backend (FastAPI).

Provides:
- Login/logout with file-based sessions
- Dashboard status and sandbox actions
- Placeholder pages for settings/console in frontend
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import logging
import os
import pty
import re
import shlex
import signal
import secrets
import struct
import subprocess
import termios
import tty
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


logger = logging.getLogger("sndbx")


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


class SSHManager:
    """SSH port reservation and socat forwarder management for sandbox SSH access.

    Reserves host ports from a configured pool and maintains socat processes that
    forward <host_port> -> <container_ip>:22 so clients can SSH into sandboxes.
    State is persisted in data/ssh_allocations.json to survive Web UI restarts.
    """

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
        """Return reserved host port for sandbox if present."""
        data = self._load()
        row = data.get(sandbox_id)
        if not row:
            return None
        try:
            return int(row["port"])
        except Exception:
            return None

    def _is_alive(self, pid: int) -> bool:
        """Check whether the socat process with given PID is still running."""
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def open(self, sandbox_id: str, container_ip: str) -> tuple[bool, Optional[int], Optional[str]]:
        """Reserve a host port and start a socat forwarder to container_ip:22.

        Returns (ok, host_port, error_message).
        """
        data = self._load()

        row = data.get(sandbox_id, {})
        existing_port = row.get("port")
        existing_pid = row.get("socat_pid")

        # Reuse existing forwarder if still alive.
        if existing_port and existing_pid and self._is_alive(int(existing_pid)):
            return True, int(existing_port), None

        # Kill stale socat if it died.
        if existing_pid:
            try:
                os.kill(int(existing_pid), 9)
            except Exception:
                pass

        # Pick a free port.
        used = {int(v.get("port")) for v in data.values() if isinstance(v, dict) and "port" in v}
        chosen: Optional[int] = None
        if existing_port and int(existing_port) not in used - {int(existing_port)}:
            chosen = int(existing_port)
        else:
            for p in range(self.start, self.end + 1):
                if p not in used:
                    chosen = p
                    break
        if chosen is None:
            return False, None, "no_ports_available"

        # Start socat forwarder on the host.
        try:
            proc = subprocess.Popen(
                [
                    "socat",
                    f"TCP-LISTEN:{chosen},fork,reuseaddr",
                    f"TCP:{container_ip}:22",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError:
            return False, None, "socat_not_installed"
        except Exception as exc:
            return False, None, str(exc)

        # Brief settle time then confirm it is still alive.
        import time as _time
        _time.sleep(0.3)
        if not self._is_alive(proc.pid):
            return False, None, f"socat exited immediately (port {chosen} busy or container unreachable)"

        data[sandbox_id] = {
            "port": chosen,
            "socat_pid": proc.pid,
            "container_ip": container_ip,
            "allocated_at": _utc_now().isoformat(),
        }
        self._save(data)
        logger.info("socat forwarder started: host:%d -> %s:22 (pid %d)", chosen, container_ip, proc.pid)
        return True, chosen, None

    def close(self, sandbox_id: str) -> bool:
        """Kill socat forwarder and release port for sandbox."""
        data = self._load()
        row = data.pop(sandbox_id, None)
        if row is None:
            return False
        pid = row.get("socat_pid")
        if pid:
            try:
                os.kill(int(pid), 15)  # SIGTERM
            except Exception:
                try:
                    os.kill(int(pid), 9)
                except Exception:
                    pass
        self._save(data)
        logger.info("SSH forwarder closed for sandbox '%s'", sandbox_id)
        return True

    def cleanup_dead_forwarders(self) -> None:
        """Remove stale entries whose socat processes have already died."""
        data = self._load()
        changed = False
        for sid, row in list(data.items()):
            pid = row.get("socat_pid")
            if pid and not self._is_alive(int(pid)):
                logger.info("Removing stale SSH forwarder record for sandbox '%s' (pid %d dead)", sid, pid)
                del data[sid]
                changed = True
        if changed:
            self._save(data)


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
        self.ssh_pool = SSHManager(self.root_dir, ssh_cfg.get("port_range", [30200, 30210]))
        self.ssh_pool.cleanup_dead_forwarders()
        self.sessions = SessionStore(self.root_dir, self.session_ttl)

        self.app = self._create_app()
        self._server: Optional[uvicorn.Server] = None

    def _find_user(self, login: str, password: str) -> Optional[Dict[str, Any]]:
        """Return user dict if credentials match."""
        for user in self.users:
            if _constant_eq(str(user.get("login", "")), login) and _constant_eq(str(user.get("password", "")), password):
                return user
        return None

    def _image_action_message(self, image_ref: str, action: str, ok: bool, output: str) -> str:
        """Build a concise dashboard message for image build/rebuild operations."""
        action_name = "build" if action == "build" else "rebuild"
        if ok:
            return f"Image '{image_ref}' {action_name} completed"

        raw = (output or "").strip()
        if not raw:
            return f"Image '{image_ref}' {action_name} failed"

        compact = " ".join(raw.splitlines())
        if len(compact) > 320:
            compact = compact[:317] + "..."
        return f"Image '{image_ref}' {action_name} failed: {compact}"

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

        # kata-runtime binary can exist while Docker runtime registration is missing.
        if checks["kata"]["ok"]:
            try:
                rt = subprocess.run(
                    ["docker", "info", "--format", "{{json .Runtimes}}"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                runtimes_text = (rt.stdout or "").strip()
                has_kata_runtime = rt.returncode == 0 and '"kata"' in runtimes_text
                if not has_kata_runtime:
                    checks["kata"] = {
                        "ok": False,
                        "detail": "kata-runtime installed, but Docker runtime 'kata' is not registered",
                    }
            except Exception as exc:
                checks["kata"] = {
                    "ok": False,
                    "detail": f"failed to verify Docker runtimes: {exc}",
                }

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

    def _repair_kata_runtime(self) -> Dict[str, Any]:
        """Try to register Kata runtime in Docker daemon using non-interactive sudo."""
        report: List[str] = []
        logger.info("Kata runtime repair requested from Web UI")

        kata_bin = subprocess.run(
            ["bash", "-lc", "command -v kata-runtime"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        kata_path = (kata_bin.stdout or "").strip()
        if kata_bin.returncode != 0 or not kata_path:
            logger.warning("Kata runtime repair failed: kata-runtime not found in PATH")
            return {
                "ok": False,
                "message": "kata-runtime binary is not available",
                "report": ["kata-runtime not found in PATH"],
            }

        report.append(f"kata-runtime: {kata_path}")
        kata_root = Path(kata_path).resolve().parent.parent
        defaults_dir = (kata_root / "share" / "defaults" / "kata-containers").resolve()
        cfg_candidates = [
            defaults_dir / "configuration.toml",
            defaults_dir / "configuration-qemu.toml",
            defaults_dir / "configuration-fc.toml",
            defaults_dir / "configuration-clh.toml",
        ]
        chosen_cfg = next((p for p in cfg_candidates if p.exists()), None)
        etc_cfg = Path("/etc/kata-containers/configuration.toml")
        custom_kata_root = str(kata_root) != "/opt/kata"
        kata_root_for_sed = str(kata_root).replace("\\", "\\\\").replace("&", "\\&").replace("#", "\\#")

        cfg_restore_cmd = ""
        if not etc_cfg.exists() and chosen_cfg is not None:
            report.append(f"Will restore missing config from {chosen_cfg}")
            cfg_restore_cmd = (
                "if [[ ! -f /etc/kata-containers/configuration.toml ]]; then "
                "sudo -n mkdir -p /etc/kata-containers; "
                f"sudo -n cp {shlex.quote(str(chosen_cfg))} /etc/kata-containers/configuration.toml; "
                "fi; "
            )
        elif not etc_cfg.exists() and chosen_cfg is None:
            report.append(
                "Kata default configuration file was not found near kata-runtime; manual reinstall may be required"
            )

        cfg_rewrite_needed = False
        if etc_cfg.exists() and custom_kata_root:
            try:
                cfg_text = etc_cfg.read_text(encoding="utf-8", errors="ignore")
                cfg_rewrite_needed = "/opt/kata/" in cfg_text
            except Exception:
                cfg_rewrite_needed = False

        cfg_rewrite_cmd = ""
        if cfg_rewrite_needed:
            report.append(f"Will rewrite /opt/kata paths in {etc_cfg} to {kata_root}")
            cfg_rewrite_cmd = (
                f"sudo -n sed -i 's#/opt/kata/#{kata_root_for_sed}/#g' /etc/kata-containers/configuration.toml; "
            )

        cfg_compat_cmd = ""
        cfg_compat_needed = False
        low_phys_bits = False
        phys_bits = 0
        if etc_cfg.exists():
            try:
                cfg_text = etc_cfg.read_text(encoding="utf-8", errors="ignore")
                cfg_compat_needed = "disable_image_nvdimm = true" not in cfg_text
            except Exception:
                cfg_compat_needed = False

        try:
            cpu_info = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="ignore")
            match = re.search(r"address sizes\s*:\s*(\d+)\s+bits physical", cpu_info)
            if match:
                phys_bits = int(match.group(1))
                low_phys_bits = 0 < phys_bits <= 36
        except Exception:
            low_phys_bits = False

        if low_phys_bits:
            report.append(f"Detected host physical address width: {phys_bits} bits")
            report.append("Will apply low phys-bits QEMU compatibility settings")

        if cfg_compat_needed:
            report.append("Will set disable_image_nvdimm = true for better compatibility on nested/limited phys-bits hosts")
            cfg_compat_cmd = (
                "if [[ -f /etc/kata-containers/configuration.toml ]]; then "
                "if grep -qE '^[[:space:]]*disable_image_nvdimm[[:space:]]*=' /etc/kata-containers/configuration.toml; then "
                "sudo -n sed -i -E 's/^[[:space:]]*disable_image_nvdimm[[:space:]]*=.*/disable_image_nvdimm = true/' /etc/kata-containers/configuration.toml; "
                "else "
                "tmp_kata_cfg=$(mktemp); "
                "awk 'BEGIN { inserted = 0 } { print; if (!inserted && $0 ~ /^\\[hypervisor\\.qemu\\]$/) { print \"disable_image_nvdimm = true\"; inserted = 1 } }' /etc/kata-containers/configuration.toml > \"$tmp_kata_cfg\"; "
                "sudo -n mv \"$tmp_kata_cfg\" /etc/kata-containers/configuration.toml; "
                "fi; "
                "fi; "
            )

        low_phys_compat_cmd = ""
        if low_phys_bits:
            wrapper_create_cmd = (
                f"printf '#!/usr/bin/env bash\\nexec {shlex.quote(str(kata_root / 'bin' / 'qemu-system-x86_64'))}"
                " -global q35-pcihost.pci-hole64-size=1073741824 \"$@\"\\n'"
                " | sudo -n tee /usr/local/bin/kata-qemu-wrapper >/dev/null; "
                "sudo -n chmod +x /usr/local/bin/kata-qemu-wrapper; "
            )
            low_phys_compat_cmd = (
                "if [[ -f /etc/kata-containers/configuration.toml ]]; then "
                "if [[ ! -x /usr/local/bin/kata-qemu-wrapper ]]; then "
                f"{wrapper_create_cmd}"
                "fi; "
                "if grep -qE '^[[:space:]]*machine_type[[:space:]]*=' /etc/kata-containers/configuration.toml; then "
                "sudo -n sed -i -E 's/^[[:space:]]*machine_type[[:space:]]*=.*/machine_type = \"q35\"/' /etc/kata-containers/configuration.toml; "
                "fi; "
                "if grep -qE '^[[:space:]]*memory_slots[[:space:]]*=' /etc/kata-containers/configuration.toml; then "
                "sudo -n sed -i -E 's/^[[:space:]]*memory_slots[[:space:]]*=.*/memory_slots = 0/' /etc/kata-containers/configuration.toml; "
                "fi; "
                "if grep -qE '^[[:space:]]*path[[:space:]]*=[[:space:]]*\".*qemu-system-x86_64\"' /etc/kata-containers/configuration.toml; then "
                "sudo -n sed -i -E 's#^[[:space:]]*path[[:space:]]*=[[:space:]]*\".*qemu-system-x86_64\"#path = \"/usr/local/bin/kata-qemu-wrapper\"#' /etc/kata-containers/configuration.toml; "
                "fi; "
                "if grep -qE '^[[:space:]]*valid_hypervisor_paths[[:space:]]*=' /etc/kata-containers/configuration.toml && ! grep -q '/usr/local/bin/kata-qemu-wrapper' /etc/kata-containers/configuration.toml; then "
                "sudo -n sed -i -E 's#^[[:space:]]*valid_hypervisor_paths[[:space:]]*=[[:space:]]*\[(.*)\]#valid_hypervisor_paths = [\1, \"/usr/local/bin/kata-qemu-wrapper\"]#' /etc/kata-containers/configuration.toml; "
                "fi; "
                "fi; "
            )

        current = subprocess.run(
            ["docker", "info", "--format", "{{json .Runtimes}}"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        current_text = (current.stdout or "").strip()
        has_kata = current.returncode == 0 and '"kata"' in current_text
        needs_runtime_registration = not has_kata
        needs_cfg_restore = bool(cfg_restore_cmd)
        needs_cfg_rewrite = bool(cfg_rewrite_cmd)
        needs_cfg_compat = bool(cfg_compat_cmd)
        needs_low_phys_compat = bool(low_phys_compat_cmd)

        if has_kata:
            report.append("Docker runtime 'kata' is already registered")
        else:
            report.append("Docker runtime 'kata' is not registered")

        if not needs_runtime_registration and not needs_cfg_restore and not needs_cfg_rewrite and not needs_cfg_compat and not needs_low_phys_compat:
            logger.info("Kata runtime repair skipped: runtime and config already configured")
            return {
                "ok": True,
                "message": "Kata runtime is already configured",
                "report": report,
            }

        cmd_parts = ["set -e"]
        if needs_runtime_registration:
            cmd_parts.extend([
                "sudo -n mkdir -p /etc/docker",
                "if [[ ! -f /etc/docker/daemon.json ]]; then echo '{}' | sudo -n tee /etc/docker/daemon.json >/dev/null; fi",
                "tmpfile=$(mktemp); jq '. as $cfg | ($cfg.runtimes // {}) as $r | ($r.kata // {}) as $k | $cfg + {runtimes: ($r + {kata: (($k + {runtimeType: \"io.containerd.kata.v2\"}) | del(.path))})}' /etc/docker/daemon.json > \"$tmpfile\"",
                "sudo -n mv \"$tmpfile\" /etc/docker/daemon.json",
            ])

        if needs_cfg_restore:
            cmd_parts.append(cfg_restore_cmd.rstrip(" ;"))

        if needs_cfg_rewrite:
            cmd_parts.append(cfg_rewrite_cmd.rstrip(" ;"))

        if needs_cfg_compat:
            cmd_parts.append(cfg_compat_cmd.rstrip(" ;"))

        if needs_low_phys_compat:
            cmd_parts.append(low_phys_compat_cmd.rstrip(" ;"))

        if needs_runtime_registration:
            cmd_parts.append("sudo -n systemctl restart docker")

        repair_cmd = "; ".join(cmd_parts)

        attempt = subprocess.run(
            ["bash", "-lc", repair_cmd],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if attempt.returncode == 0:
            runtime_ok = True
            cfg_ok = True

            if needs_runtime_registration:
                verify = subprocess.run(
                    ["docker", "info", "--format", "{{json .Runtimes}}"],
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
                verify_text = (verify.stdout or "").strip()
                runtime_ok = verify.returncode == 0 and '"kata"' in verify_text

            if needs_cfg_restore:
                cfg_ok = etc_cfg.exists()

            if needs_cfg_rewrite and cfg_ok:
                try:
                    cfg_ok = "/opt/kata/" not in etc_cfg.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    cfg_ok = False

            if needs_cfg_compat and cfg_ok:
                try:
                    cfg_ok = "disable_image_nvdimm = true" in etc_cfg.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    cfg_ok = False

            if needs_low_phys_compat and cfg_ok:
                try:
                    cfg_text_after = etc_cfg.read_text(encoding="utf-8", errors="ignore")
                    cfg_ok = (
                        "path = \"/usr/local/bin/kata-qemu-wrapper\"" in cfg_text_after
                        and "machine_type = \"q35\"" in cfg_text_after
                        and "memory_slots = 0" in cfg_text_after
                    )
                except Exception:
                    cfg_ok = False

            if runtime_ok and cfg_ok:
                report.append("Kata runtime repair steps completed successfully")
                logger.info("Kata runtime repair completed successfully")
                return {
                    "ok": True,
                    "message": "Kata runtime repaired successfully",
                    "report": report,
                }

            if not runtime_ok:
                report.append("Repair command completed, but runtime still not visible")
                logger.warning("Kata runtime repair command finished but runtime is still not visible in docker info")
            if not cfg_ok:
                report.append("Repair command completed, but /etc/kata-containers/configuration.toml is missing or still contains /opt/kata paths")
                logger.warning("Kata runtime repair command finished but Kata config is still invalid")

            return {
                "ok": False,
                "message": "Repair attempted but not all checks passed",
                "report": report,
            }

        err = ((attempt.stderr or "") + "\n" + (attempt.stdout or "")).strip()
        report.append("Automatic repair failed")
        if err:
            report.append(err)
            logger.warning("Kata runtime automatic repair failed: %s", err)

        manual = [
            "sudo mkdir -p /etc/docker",
            "if [ ! -f /etc/docker/daemon.json ]; then echo '{}' | sudo tee /etc/docker/daemon.json >/dev/null; fi",
            "tmpfile=$(mktemp) && jq '. as $cfg | ($cfg.runtimes // {}) as $r | ($r.kata // {}) as $k | $cfg + {runtimes: ($r + {kata: (($k + {runtimeType: \"io.containerd.kata.v2\"}) | del(.path))})}' /etc/docker/daemon.json > \"$tmpfile\" && sudo mv \"$tmpfile\" /etc/docker/daemon.json",
            f"if [ ! -f /etc/kata-containers/configuration.toml ]; then for f in {shlex.quote(str(defaults_dir / 'configuration.toml'))} {shlex.quote(str(defaults_dir / 'configuration-qemu.toml'))} {shlex.quote(str(defaults_dir / 'configuration-fc.toml'))} {shlex.quote(str(defaults_dir / 'configuration-clh.toml'))}; do if [ -f \"$f\" ]; then sudo mkdir -p /etc/kata-containers && sudo cp \"$f\" /etc/kata-containers/configuration.toml && break; fi; done; fi",
            f"if [ -f /etc/kata-containers/configuration.toml ]; then sudo sed -i 's#/opt/kata/#{kata_root_for_sed}/#g' /etc/kata-containers/configuration.toml; fi",
            "if [ -f /etc/kata-containers/configuration.toml ]; then"
            " if grep -qE '^[[:space:]]*disable_image_nvdimm[[:space:]]*=' /etc/kata-containers/configuration.toml; then"
            " sudo sed -i -E 's/^[[:space:]]*disable_image_nvdimm[[:space:]]*=.*/disable_image_nvdimm = true/' /etc/kata-containers/configuration.toml;"
            " else tmp_kata_cfg=$(mktemp);"
            " awk 'BEGIN{ins=0}{print; if(!ins && /^\\[hypervisor\\.qemu\\]$/){print \"disable_image_nvdimm = true\"; ins=1}}' /etc/kata-containers/configuration.toml > \"$tmp_kata_cfg\";"
            " sudo mv \"$tmp_kata_cfg\" /etc/kata-containers/configuration.toml; fi; fi",
            f"if [ -f /etc/kata-containers/configuration.toml ]; then if [ ! -x /usr/local/bin/kata-qemu-wrapper ]; then"
            f" printf '#!/usr/bin/env bash\\nexec {shlex.quote(str(kata_root / 'bin' / 'qemu-system-x86_64'))} -global q35-pcihost.pci-hole64-size=1073741824 \"$@\"\\n' | sudo tee /usr/local/bin/kata-qemu-wrapper >/dev/null;"
            " sudo chmod +x /usr/local/bin/kata-qemu-wrapper; fi; fi",
            "if [ -f /etc/kata-containers/configuration.toml ]; then sudo sed -i -E 's/^[[:space:]]*machine_type[[:space:]]*=.*/machine_type = \"q35\"/' /etc/kata-containers/configuration.toml; sudo sed -i -E 's/^[[:space:]]*memory_slots[[:space:]]*=.*/memory_slots = 0/' /etc/kata-containers/configuration.toml; sudo sed -i -E 's#^[[:space:]]*path[[:space:]]*=[[:space:]]*\".*qemu-system-x86_64\"#path = \"/usr/local/bin/kata-qemu-wrapper\"#' /etc/kata-containers/configuration.toml; if grep -qE '^[[:space:]]*valid_hypervisor_paths[[:space:]]*=' /etc/kata-containers/configuration.toml && ! grep -q '/usr/local/bin/kata-qemu-wrapper' /etc/kata-containers/configuration.toml; then sudo sed -i -E 's#^[[:space:]]*valid_hypervisor_paths[[:space:]]*=[[:space:]]*\[(.*)\]#valid_hypervisor_paths = [\1, \"/usr/local/bin/kata-qemu-wrapper\"]#' /etc/kata-containers/configuration.toml; fi; fi",
            "sudo systemctl restart docker",
            "docker info --format '{{json .Runtimes}}'",
        ]

        logger.warning("Kata runtime auto-repair needs manual steps. Run the following commands:")
        for cmd in manual:
            logger.warning("MANUAL_REPAIR_CMD: %s", cmd)

        return {
            "ok": False,
            "message": "Automatic repair requires sudo permissions",
            "report": report,
            "manual_commands": manual,
        }

    def _read_system_log(self, lines: int = 200) -> Dict[str, Any]:
        """Read recent service logs with user/system journal fallback."""
        limit = max(20, min(2000, int(lines or 200)))
        candidates = [
            ("user", ["journalctl", "--user", "-u", "sndbx", "-n", str(limit), "--no-pager", "-o", "short-iso"]),
            ("system", ["journalctl", "-u", "sndbx", "-n", str(limit), "--no-pager", "-o", "short-iso"]),
        ]

        last_error = "log source unavailable"
        for source, cmd in candidates:
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
            except Exception as exc:
                last_error = str(exc)
                continue

            if proc.returncode == 0:
                return {
                    "ok": True,
                    "source": source,
                    "lines": limit,
                    "text": (proc.stdout or "").rstrip(),
                }

            detail = (proc.stderr or proc.stdout or "").strip()
            if detail:
                last_error = detail

        return {
            "ok": False,
            "source": "none",
            "lines": limit,
            "text": "",
            "error": last_error,
        }

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
                "images": self.sandbox_manager.list_local_images(),
                "session": {
                    "login": session.get("login", ""),
                },
            }

        @app.get("/api/images")
        async def images_endpoint(session: Dict[str, Any] = Depends(self._require_session)):
            return {
                "images": self.sandbox_manager.list_local_images(),
            }

        @app.post("/api/image/{image_ref}/action")
        async def image_action(
            image_ref: str,
            body: ActionRequest,
            session: Dict[str, Any] = Depends(self._require_session),
        ):
            action = body.action.strip().lower()
            if action == "build":
                ok, out = self.sandbox_manager.build_configured_image(image_ref, no_cache=False)
                return {"ok": ok, "message": self._image_action_message(image_ref, action, ok, out)}
            if action in ("rebuild", "update"):
                ok, out = self.sandbox_manager.build_configured_image(image_ref, no_cache=True)
                return {"ok": ok, "message": self._image_action_message(image_ref, action, ok, out)}
            raise HTTPException(status_code=400, detail="Unknown image action")

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
                sandbox_cfg = self.sandbox_manager.sandbox_configs.get(sandbox_id, {})
                authorized_keys = [k for k in sandbox_cfg.get("ssh_keys", []) if isinstance(k, str) and k.strip()]
                if not authorized_keys:
                    return {"ok": False, "error": "no_ssh_keys",
                            "message": "No SSH keys configured. Add public keys to ssh_keys in config.json5."}
                status = self.sandbox_manager.get_status(sandbox_id)
                if not status.running:
                    return {"ok": False, "error": "sandbox_not_running",
                            "message": "Sandbox is not running. Start it first."}
                container_ip = self.sandbox_manager.get_container_ip(sandbox_id)
                if not container_ip:
                    return {"ok": False, "error": "no_container_ip",
                            "message": "Could not determine container IP address."}
                setup_ok, setup_msg = self.sandbox_manager.exec_ssh_setup(sandbox_id, authorized_keys)
                if not setup_ok:
                    return {"ok": False, "error": "ssh_setup_failed", "message": setup_msg}
                ok, port, err = self.ssh_pool.open(sandbox_id, container_ip)
                if not ok and err == "socat_not_installed":
                    return {
                        "ok": False,
                        "error": err,
                        "message": "Host package 'socat' is not installed. Install it: sudo apt-get update && sudo apt-get install -y socat",
                    }
                return {"ok": ok, "ssh_port": port, "error": err,
                        "message": f"SSH ready. Connect: ssh root@<host> -p {port}" if ok else err}

            if action == "ssh_close":
                ok = self.ssh_pool.close(sandbox_id)
                return {"ok": ok}

            raise HTTPException(status_code=400, detail="Unknown action")

        @app.post("/api/service/restart")
        async def service_restart(session: Dict[str, Any] = Depends(self._require_session)):
            asyncio.create_task(self._restart_service_soon())
            return {"ok": True, "message": "Service restart scheduled"}

        @app.get("/api/system-log")
        async def system_log(lines: int = 200, session: Dict[str, Any] = Depends(self._require_session)):
            return self._read_system_log(lines)

        @app.post("/api/runtime/kata/repair")
        async def repair_kata_runtime(session: Dict[str, Any] = Depends(self._require_session)):
            return self._repair_kata_runtime()

        @app.websocket("/ws/console/{sandbox_id}")
        async def ws_console(websocket: WebSocket, sandbox_id: str):
            """Interactive shell bridge to sandbox container via PTY and docker exec."""
            # Must accept before any close/send in Starlette.
            await websocket.accept()

            token = websocket.cookies.get("sndbx_session", "")
            session = self.sessions.get(token)
            if not session:
                logger.warning("ws_console: invalid/missing session cookie for sandbox '%s'", sandbox_id)
                await websocket.send_text("\r\n[sndbx] Authentication required. Please log in again.\r\n")
                await websocket.close(code=4001)
                return

            # Ensure sandbox is running before opening terminal session.
            logger.info("ws_console: ensuring sandbox '%s' is running", sandbox_id)
            if not self._ensure_sandbox_running(sandbox_id):
                logger.warning("ws_console: could not start sandbox '%s'", sandbox_id)
                await websocket.send_text("\r\n[sndbx] Unable to start sandbox for console session.\r\n")
                await websocket.close(code=1011)
                return

            logger.info("ws_console: sandbox '%s' ready, opening PTY", sandbox_id)

            master_fd, slave_fd = pty.openpty()
            # Raw mode on the host PTY master: disable host-side echo and line
            # processing so only the container PTY handles them (prevents double echo).
            tty.setraw(master_fd)
            prompt = f"[sndbx:{sandbox_id}] \\u@\\h:\\w\\$ "
            proc = subprocess.Popen(
                [
                    "docker",
                    "exec",
                    "-it",   # allocate TTY inside container → cursor keys, mc, vim work
                    "-e",
                    "TERM=xterm-256color",
                    "-e",
                    f"PS1={prompt}",
                    f"sndbx-{sandbox_id}",
                    "bash",
                    "-lc",
                    "exec bash -i",
                ],
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
                    # Resize message from FitAddon: {"type":"resize","cols":N,"rows":N}
                    try:
                        msg = json.loads(message)
                        if isinstance(msg, dict) and msg.get("type") == "resize":
                            cols = max(1, int(msg.get("cols", 80)))
                            rows = max(1, int(msg.get("rows", 24)))
                            fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                                        struct.pack("HHHH", rows, cols, 0, 0))
                            if proc.poll() is None:
                                proc.send_signal(signal.SIGWINCH)
                            continue
                    except (json.JSONDecodeError, ValueError, OSError):
                        pass
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
