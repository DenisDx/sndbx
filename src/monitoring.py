"""Read-only runtime monitoring for sndbx sandboxes.

Collects Docker-level CPU/memory data and an optional guest probe with
memory availability, OOM counters, swap, uptime, and a short process list.
No method in this module starts, stops, or modifies a sandbox.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from logging_utils import get_logger

logger = get_logger("monitoring")

HISTORY_MAX_SAMPLES = 120
PRIMARY_INTERVAL_SECONDS = 5.0
DEEP_INTERVAL_SECONDS = 15.0
DEEP_MAX_AGE_SECONDS = 45.0
DEEP_FAILURE_COOLDOWN_SECONDS = 60.0
DISK_INTERVAL_SECONDS = 60.0
RESPONSE_CACHE_SECONDS = 2.0
HISTORY_IDLE_SECONDS = 600.0
MAX_MONITORED_SANDBOXES = 32
MAX_PROCESSES = 5

MEMORY_WARN_USED_PCT = 85.0
MEMORY_CRIT_USED_PCT = 95.0
MEMORY_WARN_AVAILABLE_PCT = 20.0
MEMORY_CRIT_AVAILABLE_PCT = 10.0
MEMORY_WARN_SWAP_PCT = 10.0
MEMORY_CRIT_SWAP_PCT = 25.0
CPU_WARN_ALLOCATED_PCT = 80.0
CPU_CRIT_ALLOCATED_PCT = 95.0
CPU_STREAK_SAMPLES = 3
MEMORY_WARN_STREAK_SAMPLES = 2
HOST_WARN_AVAILABLE_PCT = 15.0
HOST_CRIT_AVAILABLE_PCT = 8.0
HOST_WARN_SWAP_PCT = 25.0
HOST_CRIT_SWAP_PCT = 50.0
DISK_WARN_FREE_PCT = 15.0
DISK_CRIT_FREE_PCT = 5.0

SANDBOX_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SIZE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*$")
_PROC_RE = re.compile(r"^\s*(\d+)\s+(.*?)\s+([0-9]+(?:\.[0-9]+)?)\s+(\d+)\s*$")

_SIZE_UNITS = {
    "B": 1,
    "kB": 1000,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
}


def _iso_now() -> str:
    """Return the current UTC time in ISO-8601 form with a trailing Z."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_byte_size(value: Any) -> Optional[int]:
    """Parse a Docker human-readable byte size; return bytes or None."""
    text = str(value or "").strip()
    if not text:
        return None
    match = _SIZE_RE.match(text)
    if not match:
        try:
            return int(float(text))
        except (TypeError, ValueError):
            return None
    number = float(match.group(1))
    unit = match.group(2)
    factor = _SIZE_UNITS.get(unit)
    if factor is None:
        factor = _SIZE_UNITS.get(unit.lower())
    if factor is None:
        return None
    return int(number * factor)


def parse_usage_pair(value: Any) -> Tuple[Optional[int], Optional[int]]:
    """Parse a Docker stats pair such as '1.3GiB / 4GiB' into bytes."""
    text = str(value or "").strip()
    if not text:
        return None, None
    parts = [part.strip() for part in text.split("/", 1)]
    if len(parts) != 2:
        return None, None
    return parse_byte_size(parts[0]), parse_byte_size(parts[1])


def parse_percent(value: Any) -> Optional[float]:
    """Parse a Docker percentage string; return a float or None."""
    text = str(value or "").strip().replace("%", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value: Any) -> Optional[int]:
    """Parse a decimal integer; return None on failure."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def parse_meminfo(text: str) -> Dict[str, int]:
    """Parse /proc/meminfo into byte values keyed by field name."""
    values: Dict[str, int] = {}
    for line in str(text or "").splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        match = re.match(r"\s*(\d+)\s*kB\b", rest)
        if match:
            values[key.strip()] = int(match.group(1)) * 1024
    return values


def parse_cgroup_events(text: str) -> Dict[str, int]:
    """Parse cgroup v2 memory.events counters."""
    events: Dict[str, int] = {}
    for line in str(text or "").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        value = parse_int(parts[1])
        if value is not None:
            events[parts[0]] = value
    return events


def parse_pressure(text: str) -> Dict[str, float]:
    """Parse the 'some' line of a PSI pressure file."""
    for line in str(text or "").splitlines():
        if not line.startswith("some "):
            continue
        values: Dict[str, float] = {}
        for part in line.split()[1:]:
            if "=" not in part:
                continue
            key, raw = part.split("=", 1)
            try:
                values[key] = float(raw)
            except ValueError:
                continue
        return values
    return {}


def parse_processes(text: str) -> Dict[str, List[Dict[str, Any]]]:
    """Parse ps output and return top processes by RSS and by CPU."""
    rows: List[Dict[str, Any]] = []
    for line in str(text or "").splitlines():
        match = _PROC_RE.match(line)
        if not match:
            continue
        rows.append({
            "pid": int(match.group(1)),
            "name": match.group(2).strip() or "?",
            "cpu_pct": round(float(match.group(3)), 2),
            "rss_bytes": int(match.group(4)) * 1024,
        })
    by_memory = sorted(rows, key=lambda row: row["rss_bytes"], reverse=True)[:MAX_PROCESSES]
    by_cpu = sorted(rows, key=lambda row: row["cpu_pct"], reverse=True)[:MAX_PROCESSES]
    return {"top_memory": by_memory, "top_cpu": by_cpu}


def parse_guest_probe(text: str, probed_at: Optional[str] = None) -> Dict[str, Any]:
    """Parse the fixed guest probe output into structured metrics."""
    sections: Dict[str, str] = {}
    current: Optional[str] = None
    buffer: List[str] = []
    for line in str(text or "").splitlines():
        if line.startswith("@@"):
            if current is not None:
                sections[current] = "\n".join(buffer)
            current = line[2:].strip()
            buffer = []
            continue
        buffer.append(line)
    if current is not None:
        sections[current] = "\n".join(buffer)

    meminfo = parse_meminfo(sections.get("MEMINFO", ""))
    events = parse_cgroup_events(sections.get("MEMEVENTS", ""))
    uptime_raw = str(sections.get("UPTIME", "")).split()
    try:
        uptime_seconds = float(uptime_raw[0]) if uptime_raw else None
    except ValueError:
        uptime_seconds = None

    total = meminfo.get("MemTotal")
    available = meminfo.get("MemAvailable")
    available_pct = None
    if total and available is not None:
        available_pct = round(available / total * 100.0, 1)

    return {
        "probed_at": probed_at or _iso_now(),
        "mem_total_bytes": total,
        "mem_available_bytes": available,
        "mem_available_pct": available_pct,
        "swap_total_bytes": meminfo.get("SwapTotal"),
        "swap_free_bytes": meminfo.get("SwapFree"),
        "swap_current_bytes": parse_int(sections.get("SWAP", "")),
        "oom_events": events.get("oom"),
        "oom_kill_total": events.get("oom_kill"),
        "max_events": events.get("max"),
        "pressure_memory": parse_pressure(sections.get("PSIMEM", "")),
        "pressure_cpu": parse_pressure(sections.get("PSICPU", "")),
        "uptime_seconds": uptime_seconds,
        "processes": parse_processes(sections.get("PROCS", "")),
    }


def parse_stats_row(row: Dict[str, Any], allocated_vcpus: int) -> Optional[Dict[str, Any]]:
    """Convert one Docker stats JSON row into normalized metrics."""
    if not isinstance(row, dict):
        return None
    used, limit = parse_usage_pair(row.get("MemUsage"))
    cpu_percent = parse_percent(row.get("CPUPerc"))
    net_rx, net_tx = parse_usage_pair(row.get("NetIO"))
    if used is None or limit is None:
        return None
    try:
        vcpus = max(1, int(allocated_vcpus or 1))
    except (TypeError, ValueError):
        vcpus = 1
    used_pct = round(used / limit * 100.0, 1) if limit > 0 else None
    allocated_pct = round(cpu_percent / vcpus, 2) if cpu_percent is not None else None
    return {
        "used_bytes": used,
        "limit_bytes": limit,
        "used_pct": used_pct,
        "cpu_percent": cpu_percent,
        "allocated_vcpus": vcpus,
        "allocated_pct": allocated_pct,
        "net_rx_bytes": net_rx,
        "net_tx_bytes": net_tx,
        "pids": parse_int(row.get("PIDs")),
    }


def build_host_stats(meminfo_text: str, pressure_text: str = "") -> Dict[str, Any]:
    """Build host memory status from /proc/meminfo and PSI text."""
    meminfo = parse_meminfo(meminfo_text)
    total = meminfo.get("MemTotal")
    available = meminfo.get("MemAvailable")
    swap_total = meminfo.get("SwapTotal") or 0
    swap_free = meminfo.get("SwapFree") or 0
    reasons: List[str] = []
    status = "ok"

    available_pct = None
    if total and available is not None:
        available_pct = round(available / total * 100.0, 1)
        if available_pct < HOST_CRIT_AVAILABLE_PCT:
            status = "critical"
            reasons.append("host_memory_low")
        elif available_pct < HOST_WARN_AVAILABLE_PCT:
            status = "warn"
            reasons.append("host_memory_low")

    swap_used = max(0, swap_total - swap_free)
    swap_used_pct = round(swap_used / swap_total * 100.0, 1) if swap_total > 0 else None
    if swap_used_pct is not None:
        if swap_used_pct >= HOST_CRIT_SWAP_PCT:
            status = "critical"
            reasons.append("host_swap_used")
        elif swap_used_pct >= HOST_WARN_SWAP_PCT and status == "ok":
            status = "warn"
            reasons.append("host_swap_used")

    pressure = parse_pressure(pressure_text)
    return {
        "mem_total_bytes": total,
        "mem_available_bytes": available,
        "mem_available_pct": available_pct,
        "swap_total_bytes": swap_total,
        "swap_used_bytes": swap_used,
        "swap_used_pct": swap_used_pct,
        "pressure_some_avg60": pressure.get("avg60"),
        "status": status if available_pct is not None else "unknown",
        "reasons": reasons,
    }


def read_host_stats() -> Dict[str, Any]:
    """Read host memory and optional PSI data from procfs."""
    try:
        meminfo_text = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError as error:
        return {
            "status": "unknown",
            "reasons": [],
            "error": str(error),
        }
    pressure_text = ""
    try:
        pressure_text = Path("/proc/pressure/memory").read_text(encoding="utf-8")
    except OSError:
        pass
    return build_host_stats(meminfo_text, pressure_text)


def _severity_status(severity: int) -> str:
    """Map an internal 0/1/2 severity to an API status string."""
    if severity >= 2:
        return "critical"
    if severity == 1:
        return "warn"
    return "ok"


def _is_running_status(status: Any) -> bool:
    """Return True when a docker ps status string describes a running VM."""
    text = str(status or "").strip().lower()
    return text.startswith("up") or text == "running"


class SandboxMonitor:
    """Collect and cache read-only runtime metrics for configured sandboxes."""

    def __init__(
        self,
        sandbox_manager: Any,
        config: Dict[str, Any],
        clock: Optional[Callable[[], float]] = None,
        wall_clock: Optional[Callable[[], datetime]] = None,
        host_stats_provider: Optional[Callable[[], Dict[str, Any]]] = None,
        disk_usage: Optional[Callable[[Path], Tuple[int, int, int]]] = None,
    ):
        self.sandbox_manager = sandbox_manager
        self.config = config or {}
        self._clock = clock or time.monotonic
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._host_stats_provider = host_stats_provider or read_host_stats
        self._disk_usage = disk_usage or shutil.disk_usage
        self._lock = threading.RLock()
        self._states: Dict[str, Dict[str, Any]] = {}
        self._cache: Dict[Tuple[str, ...], Tuple[float, Dict[str, Any]]] = {}
        root = str(self.config.get("root") or "").strip()
        self.root_dir = Path(root).resolve() if root else Path.cwd().resolve()
        self.images_dir = self.root_dir / "images"

    def _now_iso(self) -> str:
        """Return the current wall-clock time as an ISO string."""
        moment = self._wall_clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _validate_ids(self, sandbox_ids: Optional[List[str]]) -> List[str]:
        """Validate and normalize requested sandbox IDs."""
        configured = self.sandbox_manager.sandbox_configs or {}
        requested = list(sandbox_ids) if sandbox_ids else list(configured.keys())
        if len(requested) > MAX_MONITORED_SANDBOXES:
            raise ValueError(f"At most {MAX_MONITORED_SANDBOXES} sandboxes can be monitored at once")

        result: List[str] = []
        seen = set()
        for raw in requested:
            sandbox_id = str(raw or "").strip()
            if not sandbox_id:
                continue
            if not SANDBOX_ID_RE.match(sandbox_id):
                raise ValueError(f"Invalid sandbox id: {sandbox_id}")
            if sandbox_id not in configured:
                raise ValueError(f"Unknown sandbox id: {sandbox_id}")
            if sandbox_id in seen:
                continue
            seen.add(sandbox_id)
            result.append(sandbox_id)
        return result

    def _state(self, sandbox_id: str) -> Dict[str, Any]:
        """Return mutable sampling state for one sandbox."""
        state = self._states.get(sandbox_id)
        if state is None:
            state = {
                "history": [],
                "last_sample_at": 0.0,
                "last_requested_at": 0.0,
                "deep": None,
                "last_deep_ok": 0.0,
                "deep_error": None,
                "deep_next_at": 0.0,
                "disk": None,
                "last_disk_at": 0.0,
                "mem_warn_streak": 0,
                "cpu_warn_streak": 0,
                "cpu_crit_streak": 0,
            }
            self._states[sandbox_id] = state
        return state

    def _evict_idle(self, now: float, requested: set[str]) -> None:
        """Drop history for sandboxes not requested for a long time."""
        for sandbox_id in list(self._states):
            if sandbox_id in requested:
                continue
            state = self._states[sandbox_id]
            if now - float(state.get("last_requested_at", 0.0)) > HISTORY_IDLE_SECONDS:
                self._states.pop(sandbox_id, None)
        for key in list(self._cache):
            if not set(key).intersection(requested):
                timestamp, _ = self._cache[key]
                if now - timestamp > HISTORY_IDLE_SECONDS:
                    self._cache.pop(key, None)

    def collect(self, sandbox_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """Collect current metrics and return the dashboard payload."""
        ids = self._validate_ids(sandbox_ids)
        now = self._clock()
        cache_key = tuple(sorted(ids))
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and now - cached[0] < RESPONSE_CACHE_SECONDS:
                return cached[1]

            try:
                host = self._host_stats_provider()
            except Exception as error:
                host = {"status": "unknown", "reasons": [], "error": str(error)}
            if not isinstance(host, dict):
                host = {"status": "unknown", "reasons": [], "error": "host stats unavailable"}
            discovered: Dict[str, Dict[str, Any]] = {}
            discovery_ok = True
            discovery_error = None
            try:
                list_ok, rows = self.sandbox_manager.list_sandboxes()
                if list_ok:
                    for row in rows:
                        sandbox_id = str(row.get("sandbox_id", ""))
                        if sandbox_id:
                            discovered[sandbox_id] = row
                else:
                    discovery_ok = False
                    discovery_error = str(rows or "Docker container list unavailable")
            except Exception as error:
                discovery_ok = False
                discovery_error = str(error)

            running_ids = [
                sandbox_id for sandbox_id in ids
                if _is_running_status(discovered.get(sandbox_id, {}).get("status"))
            ]
            stats_by_id: Dict[str, Dict[str, Any]] = {}
            stats_error = None
            if running_ids:
                try:
                    stats_ok, stats_rows = self.sandbox_manager.get_container_stats(running_ids)
                except Exception as error:
                    stats_ok, stats_rows = False, str(error)
                if stats_ok and isinstance(stats_rows, list):
                    for row in stats_rows:
                        name = str(row.get("Name", ""))
                        sandbox_id = name[len("sndbx-"):] if name.startswith("sndbx-") else name
                        if sandbox_id not in running_ids:
                            continue
                        cfg = self.sandbox_manager.sandbox_configs.get(sandbox_id, {})
                        parsed = parse_stats_row(row, cfg.get("cpus", 1) or 1)
                        if parsed is not None:
                            stats_by_id[sandbox_id] = parsed
                else:
                    stats_error = str(stats_rows or "docker stats failed")

            sandboxes: Dict[str, Any] = {}
            for sandbox_id in ids:
                sandboxes[sandbox_id] = self._collect_one(
                    sandbox_id, now, host, discovered, running_ids, stats_by_id,
                    discovery_ok, discovery_error, stats_error,
                )

            self._evict_idle(now, set(ids))
            payload = {
                "ok": True,
                "generated_at": self._now_iso(),
                "sample_interval_seconds": int(PRIMARY_INTERVAL_SECONDS),
                "host": host,
                "sandboxes": sandboxes,
            }
            self._cache[cache_key] = (now, payload)
            return payload

    def _collect_one(
        self,
        sandbox_id: str,
        now: float,
        host: Dict[str, Any],
        discovered: Dict[str, Dict[str, Any]],
        running_ids: List[str],
        stats_by_id: Dict[str, Dict[str, Any]],
        discovery_ok: bool,
        discovery_error: Optional[str],
        stats_error: Optional[str],
    ) -> Dict[str, Any]:
        """Build one sandbox result without affecting other sandboxes."""
        state = self._state(sandbox_id)
        state["last_requested_at"] = now
        cfg = self.sandbox_manager.sandbox_configs.get(sandbox_id, {}) or {}
        running = sandbox_id in running_ids
        collected_at = self._now_iso()

        if not running:
            error = "not running"
            if sandbox_id not in discovered:
                error = "not created" if discovery_ok else (discovery_error or "container list unavailable")
            return {
                "sandbox_id": sandbox_id,
                "running": False,
                "collected_at": collected_at,
                "status": "unknown",
                "reasons": ["not_running"],
                "health_source": "none",
                "memory": None,
                "cpu": None,
                "disk": None,
                "network": None,
                "pids": None,
                "uptime_seconds": None,
                "processes": None,
                "history": [],
                "error": error,
            }

        stats = stats_by_id.get(sandbox_id)
        if stats is None:
            return {
                "sandbox_id": sandbox_id,
                "running": True,
                "collected_at": collected_at,
                "status": "unknown",
                "reasons": ["docker_stats_failed"],
                "health_source": "none",
                "memory": None,
                "cpu": None,
                "disk": None,
                "network": None,
                "pids": None,
                "uptime_seconds": None,
                "processes": None,
                "history": list(state.get("history", [])),
                "error": stats_error or "docker stats unavailable",
            }

        deep, probe_status = self._deep_for(sandbox_id, now)
        disk = self._disk_for(sandbox_id, cfg, now, running)
        memory = self._memory_view(stats, deep, probe_status, cfg)
        cpu = {
            "percent": stats.get("cpu_percent"),
            "allocated_vcpus": stats.get("allocated_vcpus"),
            "allocated_pct": stats.get("allocated_pct"),
        }
        network = {
            "rx_bytes": stats.get("net_rx_bytes"),
            "tx_bytes": stats.get("net_tx_bytes"),
        }

        mem_sev, mem_reasons = self._evaluate_memory(state, memory, probe_status)
        cpu_sev, cpu_reasons = self._evaluate_cpu(state, cpu, deep)
        disk_sev, disk_reasons = self._evaluate_disk(disk)
        severity = max(mem_sev, cpu_sev, disk_sev)
        reasons = mem_reasons + cpu_reasons + disk_reasons + self._probe_reasons(probe_status)
        reasons = list(dict.fromkeys(reasons))

        host_status = str(host.get("status", "unknown"))
        if mem_sev > 0 and host_status in ("warn", "critical"):
            health_source = "both"
        elif mem_sev > 0:
            health_source = "guest"
        elif host_status in ("warn", "critical"):
            health_source = "host"
        else:
            health_source = "none"

        history = self._append_history(state, now, memory, cpu, collected_at)
        processes = None
        if deep is not None:
            processes = dict(deep.get("processes") or {})
            processes["probed_at"] = deep.get("probed_at")
            processes["stale"] = probe_status != "ok"

        return {
            "sandbox_id": sandbox_id,
            "running": True,
            "collected_at": collected_at,
            "status": _severity_status(severity),
            "reasons": reasons,
            "health_source": health_source,
            "memory": memory,
            "cpu": cpu,
            "disk": disk,
            "network": network,
            "pids": stats.get("pids"),
            "uptime_seconds": deep.get("uptime_seconds") if deep else None,
            "processes": processes,
            "history": list(history),
            "error": None,
        }

    def _memory_view(
        self,
        stats: Dict[str, Any],
        deep: Optional[Dict[str, Any]],
        probe_status: str,
        cfg: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Combine Docker memory values with optional guest memory data."""
        limit = stats.get("limit_bytes") or 0
        used = stats.get("used_bytes") or 0
        available = deep.get("mem_available_bytes") if deep else None
        available_pct = deep.get("mem_available_pct") if deep else None
        swap_current = deep.get("swap_current_bytes") if deep else None
        swap_ratio = None
        if swap_current is not None and limit > 0:
            swap_ratio = round(swap_current / limit * 100.0, 1)
        return {
            "used_bytes": used,
            "limit_bytes": limit,
            "used_pct": stats.get("used_pct"),
            "available_bytes": available,
            "available_pct": available_pct,
            "swap_used_bytes": swap_current,
            "swap_used_pct_of_limit": swap_ratio,
            "oom_kill_total": deep.get("oom_kill_total") if deep else None,
            "probe": probe_status,
            "configured_memory": cfg.get("memory"),
        }

    def _probe_reasons(self, probe_status: str) -> List[str]:
        """Return reasons for a missing or stale guest probe."""
        if probe_status == "failed":
            return ["guest_probe_failed"]
        if probe_status == "stale":
            return ["guest_probe_stale"]
        return []

    def _evaluate_memory(
        self,
        state: Dict[str, Any],
        memory: Dict[str, Any],
        probe_status: str,
    ) -> Tuple[int, List[str]]:
        """Evaluate guest memory pressure and apply warning debounce."""
        reasons: List[str] = []
        severity = 0
        used_pct = memory.get("used_pct")
        available_pct = memory.get("available_pct")
        swap_pct = memory.get("swap_used_pct_of_limit")
        oom_kills = memory.get("oom_kill_total")

        if available_pct is not None:
            if available_pct < MEMORY_CRIT_AVAILABLE_PCT:
                severity = 2
                reasons.append("guest_memory_available_low")
            elif available_pct < MEMORY_WARN_AVAILABLE_PCT:
                severity = max(severity, 1)
                reasons.append("guest_memory_available_low")
        elif used_pct is not None:
            if used_pct >= MEMORY_CRIT_USED_PCT:
                severity = 2
                reasons.append("guest_memory_used_high")
            elif used_pct >= MEMORY_WARN_USED_PCT:
                severity = max(severity, 1)
                reasons.append("guest_memory_used_high")
        if swap_pct is not None:
            if swap_pct >= MEMORY_CRIT_SWAP_PCT:
                severity = 2
                reasons.append("guest_swap_used")
            elif swap_pct >= MEMORY_WARN_SWAP_PCT:
                severity = max(severity, 1)
                reasons.append("guest_swap_used")
        if oom_kills is not None and oom_kills > 0:
            severity = 2
            reasons.append("guest_oom_kill")

        if severity == 1:
            state["mem_warn_streak"] = int(state.get("mem_warn_streak", 0)) + 1
            if state["mem_warn_streak"] < MEMORY_WARN_STREAK_SAMPLES:
                return 0, []
        else:
            state["mem_warn_streak"] = 0
        return severity, reasons

    def _evaluate_cpu(
        self,
        state: Dict[str, Any],
        cpu: Dict[str, Any],
        deep: Optional[Dict[str, Any]],
    ) -> Tuple[int, List[str]]:
        """Evaluate CPU saturation with a short streak requirement."""
        reasons: List[str] = []
        allocated = cpu.get("allocated_pct")
        if allocated is None:
            state["cpu_warn_streak"] = 0
            state["cpu_crit_streak"] = 0
            return 0, reasons

        if allocated >= CPU_CRIT_ALLOCATED_PCT:
            state["cpu_crit_streak"] = int(state.get("cpu_crit_streak", 0)) + 1
        else:
            state["cpu_crit_streak"] = 0
        if allocated >= CPU_WARN_ALLOCATED_PCT:
            state["cpu_warn_streak"] = int(state.get("cpu_warn_streak", 0)) + 1
        else:
            state["cpu_warn_streak"] = 0

        severity = 0
        if state["cpu_crit_streak"] >= CPU_STREAK_SAMPLES:
            severity = 2
            reasons.append("cpu_saturated")
        elif state["cpu_warn_streak"] >= CPU_STREAK_SAMPLES:
            severity = 1
            reasons.append("cpu_saturated")

        pressure = (deep or {}).get("pressure_cpu") or {}
        if (pressure.get("avg60") or 0.0) >= 20.0:
            severity = max(severity, 1)
            reasons.append("cpu_pressure")
        return severity, reasons

    def _evaluate_disk(self, disk: Optional[Dict[str, Any]]) -> Tuple[int, List[str]]:
        """Evaluate host filesystem free space."""
        if not disk:
            return 0, []
        free_pct = disk.get("host_free_pct")
        if free_pct is None:
            return 0, []
        if free_pct < DISK_CRIT_FREE_PCT:
            return 2, ["host_disk_low"]
        if free_pct < DISK_WARN_FREE_PCT:
            return 1, ["host_disk_low"]
        return 0, []

    def _append_history(
        self,
        state: Dict[str, Any],
        now: float,
        memory: Dict[str, Any],
        cpu: Dict[str, Any],
        collected_at: str,
    ) -> List[Dict[str, Any]]:
        """Append a primary sample at the configured cadence."""
        last_sample = float(state.get("last_sample_at", 0.0))
        history = state["history"]
        if not history or now - last_sample >= PRIMARY_INTERVAL_SECONDS:
            history.append({
                "ts": collected_at,
                "mem_pct": memory.get("used_pct"),
                "cpu_pct": cpu.get("allocated_pct"),
                "available_pct": memory.get("available_pct"),
            })
            if len(history) > HISTORY_MAX_SAMPLES:
                del history[: len(history) - HISTORY_MAX_SAMPLES]
            state["last_sample_at"] = now
        return history

    def _deep_for(self, sandbox_id: str, now: float) -> Tuple[Optional[Dict[str, Any]], str]:
        """Return cached guest metrics and their freshness status."""
        state = self._state(sandbox_id)
        if now >= float(state.get("deep_next_at", 0.0)):
            try:
                ok, output = self.sandbox_manager.probe_guest_metrics(sandbox_id)
            except Exception as error:
                ok, output = False, str(error)
            if ok:
                parsed = parse_guest_probe(str(output), self._now_iso())
                if parsed.get("mem_total_bytes") and parsed.get("mem_available_bytes") is not None:
                    state["deep"] = parsed
                    state["last_deep_ok"] = now
                    state["deep_error"] = None
                    state["deep_next_at"] = now + DEEP_INTERVAL_SECONDS
                else:
                    state["deep_error"] = "guest memory data unavailable"
                    state["deep_next_at"] = now + DEEP_FAILURE_COOLDOWN_SECONDS
            else:
                state["deep_error"] = str(output or "guest probe failed")
                state["deep_next_at"] = now + DEEP_FAILURE_COOLDOWN_SECONDS

        deep = state.get("deep")
        age = now - float(state.get("last_deep_ok", 0.0))
        if deep is not None and age <= DEEP_MAX_AGE_SECONDS:
            return deep, ("stale" if state.get("deep_error") else "ok")
        if state.get("deep_error"):
            return None, "failed"
        if deep is not None:
            return None, "stale"
        return None, "pending"

    def _disk_for(
        self,
        sandbox_id: str,
        cfg: Dict[str, Any],
        now: float,
        running: bool,
    ) -> Optional[Dict[str, Any]]:
        """Return cached disk values, refreshing them infrequently."""
        state = self._state(sandbox_id)
        if state.get("disk") is not None and now - float(state.get("last_disk_at", 0.0)) < DISK_INTERVAL_SECONDS:
            return state["disk"]

        writable = None
        if running:
            try:
                size_ok, size_value = self.sandbox_manager.get_container_disk_size(sandbox_id)
                if size_ok:
                    writable = int(size_value)
            except Exception:
                writable = None

        path = self._data_path(sandbox_id, cfg)
        try:
            total, used, free = self._disk_usage(path)
            free_pct = round(free / total * 100.0, 1) if total else None
            disk = {
                "writable_bytes": writable,
                "host_free_bytes": free,
                "host_total_bytes": total,
                "host_free_pct": free_pct,
                "source": str(path),
            }
        except OSError as error:
            disk = {
                "writable_bytes": writable,
                "host_free_bytes": None,
                "host_total_bytes": None,
                "host_free_pct": None,
                "source": str(path),
                "error": str(error),
            }
        state["disk"] = disk
        state["last_disk_at"] = now
        return disk

    def _data_path(self, sandbox_id: str, cfg: Dict[str, Any]) -> Path:
        """Resolve the persistent image directory used for host disk stats."""
        local_id = str(cfg.get("local_image_id") or "").strip() or sandbox_id
        candidate = self.images_dir / local_id
        return candidate if candidate.exists() else self.root_dir
