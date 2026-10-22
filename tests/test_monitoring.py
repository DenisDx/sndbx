"""Tests for read-only sandbox runtime monitoring."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

from monitoring import (
    SandboxMonitor,
    build_host_stats,
    parse_byte_size,
    parse_guest_probe,
    parse_meminfo,
    parse_processes,
    parse_stats_row,
    parse_usage_pair,
)
from webui_server import WebUIServer
from sandbox import DockerSandboxManager

ROOT = Path(__file__).resolve().parents[1]

GUEST_PROBE = """@@MEMINFO
MemTotal:        4194304 kB
MemFree:         1000000 kB
MemAvailable:    2097152 kB
SwapTotal:       0 kB
SwapFree:        0 kB
@@MEMEVENTS
low 0
high 0
max 0
oom 0
oom_kill 0
@@SWAP
0
@@UPTIME
100.0 200.0
@@PROCS
    2 MainThread         2.2 524288
  100 postgres          0.4 98304
  101 postgres          0.3 65536
"""


def guest_probe_with_available(mem_available_kb: int, oom_kills: int = 0) -> str:
    """Return a probe fixture with a custom MemAvailable and OOM count."""
    return GUEST_PROBE.replace(
        "MemAvailable:    2097152 kB",
        f"MemAvailable:    {mem_available_kb} kB",
    ).replace("oom_kill 0", f"oom_kill {oom_kills}")


def stats_row(cpu_percent: str = "50.0%") -> dict[str, Any]:
    """Return one docker stats JSON row."""
    return {
        "Name": "sndbx-a",
        "MemUsage": "1GiB / 4GiB",
        "MemPerc": "25.0%",
        "CPUPerc": cpu_percent,
        "NetIO": "1MB / 2MB",
        "PIDs": "10",
        "BlockIO": "0B / 0B",
    }


class FakeManager:
    """Minimal manager surface for monitor and HTTP tests."""

    def __init__(self, sandbox_id: str = "a", running: bool = True):
        self.sandbox_id = sandbox_id
        self.running = running
        self.sandbox_configs = {sandbox_id: {"cpus": 2, "memory": "4G", "local_image_id": sandbox_id}}
        self.stats_calls = 0
        self.probe_calls = 0
        self.disk_calls = 0
        self.stats_value = stats_row()
        self.probe_value = GUEST_PROBE
        self.stats_ok = True
        self.probe_ok = True

    def list_sandboxes(self):
        """Return one discovered sandbox with a docker ps-like status."""
        status = "Up 2 days" if self.running else "Exited (0) 2 hours ago"
        return True, [{
            "sandbox_id": self.sandbox_id,
            "container_name": f"sndbx-{self.sandbox_id}",
            "status": status,
        }]

    def get_container_stats(self, sandbox_ids):
        """Return stats rows for requested IDs."""
        self.stats_calls += 1
        if not self.stats_ok:
            return False, "docker stats failed"
        return True, [dict(self.stats_value) for _ in sandbox_ids]

    def probe_guest_metrics(self, sandbox_id):
        """Return the guest probe fixture."""
        self.probe_calls += 1
        return self.probe_ok, self.probe_value

    def get_container_disk_size(self, sandbox_id):
        """Return a fixed writable-layer size."""
        self.disk_calls += 1
        return True, "1048576"


def make_monitor(fake: FakeManager, root: Path, now: list[float]):
    """Build a monitor with deterministic host and disk values."""
    config = {
        "root": str(root),
        "sandboxes": {"items": fake.sandbox_configs},
    }
    return SandboxMonitor(
        fake,
        config,
        clock=lambda: now[0],
        wall_clock=lambda: datetime(2026, 10, 22, 11, 0, tzinfo=timezone.utc),
        host_stats_provider=lambda: {
            "mem_total_bytes": 64 * 1024**3,
            "mem_available_bytes": 40 * 1024**3,
            "mem_available_pct": 62.5,
            "swap_total_bytes": 0,
            "swap_used_bytes": 0,
            "status": "ok",
            "reasons": [],
        },
        disk_usage=lambda path: (100 * 1024**3, 20 * 1024**3, 80 * 1024**3),
    )


class MonitoringParsingTests(unittest.TestCase):
    """Verify parsers used by the monitor."""

    def test_parse_byte_size_units(self) -> None:
        self.assertEqual(parse_byte_size("1GiB"), 1024**3)
        self.assertEqual(parse_byte_size("1.5MiB"), int(1.5 * 1024**2))
        self.assertEqual(parse_byte_size("10MB"), 10_000_000)
        self.assertEqual(parse_byte_size("512B"), 512)
        self.assertIsNone(parse_byte_size("n/a"))

    def test_parse_usage_pair(self) -> None:
        self.assertEqual(
            parse_usage_pair("1.5GiB / 4GiB"),
            (int(1.5 * 1024**3), 4 * 1024**3),
        )

    def test_parse_meminfo(self) -> None:
        values = parse_meminfo("MemTotal: 1024 kB\nMemAvailable: 512 kB\n")
        self.assertEqual(values["MemTotal"], 1024 * 1024)
        self.assertEqual(values["MemAvailable"], 512 * 1024)

    def test_parse_processes_sorts_and_limits(self) -> None:
        result = parse_processes(GUEST_PROBE.split("@@PROCS\n", 1)[1])
        self.assertEqual(result["top_memory"][0]["name"], "MainThread")
        self.assertEqual(result["top_cpu"][0]["name"], "MainThread")
        self.assertLessEqual(len(result["top_memory"]), 5)

    def test_parse_guest_probe(self) -> None:
        probe = parse_guest_probe(GUEST_PROBE)
        self.assertEqual(probe["mem_total_bytes"], 4 * 1024**3)
        self.assertEqual(probe["mem_available_bytes"], 2 * 1024**3)
        self.assertEqual(probe["oom_kill_total"], 0)
        self.assertEqual(probe["uptime_seconds"], 100.0)
        self.assertEqual(probe["processes"]["top_memory"][0]["pid"], 2)

    def test_parse_stats_row_normalizes_cpu(self) -> None:
        parsed = parse_stats_row(stats_row("150.0%"), 2)
        self.assertEqual(parsed["used_bytes"], 1024**3)
        self.assertEqual(parsed["limit_bytes"], 4 * 1024**3)
        self.assertEqual(parsed["allocated_pct"], 75.0)
        self.assertEqual(parsed["pids"], 10)

    def test_host_stats_thresholds(self) -> None:
        meminfo = "MemTotal: 1000000 kB\nMemAvailable: 120000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n"
        host = build_host_stats(meminfo)
        self.assertEqual(host["status"], "warn")
        self.assertIn("host_memory_low", host["reasons"])

        critical = build_host_stats("MemTotal: 1000000 kB\nMemAvailable: 10000 kB\n")
        self.assertEqual(critical["status"], "critical")


class SandboxMonitorTests(unittest.TestCase):
    """Verify sampling, history, caching, and resource rules."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = [1000.0]
        self.fake = FakeManager()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_collect_populates_metrics_and_history(self) -> None:
        monitor = make_monitor(self.fake, self.root, self.now)
        payload = monitor.collect(["a"])
        row = payload["sandboxes"]["a"]
        self.assertTrue(row["running"])
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["memory"]["available_pct"], 50.0)
        self.assertEqual(row["cpu"]["allocated_pct"], 25.0)
        self.assertEqual(len(row["history"]), 1)
        self.assertEqual(row["processes"]["top_memory"][0]["name"], "MainThread")
        self.assertEqual(row["disk"]["writable_bytes"], 1048576)
        self.assertEqual(row["disk"]["host_free_pct"], 80.0)

    def test_response_cache_avoids_duplicate_docker_calls(self) -> None:
        monitor = make_monitor(self.fake, self.root, self.now)
        monitor.collect(["a"])
        self.assertEqual(self.fake.stats_calls, 1)
        self.now[0] += 1.0
        monitor.collect(["a"])
        self.assertEqual(self.fake.stats_calls, 1)
        self.now[0] += 5.0
        monitor.collect(["a"])
        self.assertEqual(self.fake.stats_calls, 2)

    def test_history_appends_at_five_second_cadence(self) -> None:
        monitor = make_monitor(self.fake, self.root, self.now)
        monitor.collect(["a"])
        self.now[0] += 5.0
        row = monitor.collect(["a"])["sandboxes"]["a"]
        self.assertEqual(len(row["history"]), 2)
        self.assertEqual(row["history"][-1]["ts"], "2026-10-22T11:00:00Z")

    def test_low_available_memory_is_critical_immediately(self) -> None:
        self.fake.probe_value = guest_probe_with_available(300 * 1024)
        monitor = make_monitor(self.fake, self.root, self.now)
        row = monitor.collect(["a"])["sandboxes"]["a"]
        self.assertEqual(row["status"], "critical")
        self.assertIn("guest_memory_available_low", row["reasons"])

    def test_oom_kill_is_critical(self) -> None:
        self.fake.probe_value = guest_probe_with_available(2 * 1024 * 1024, oom_kills=1)
        monitor = make_monitor(self.fake, self.root, self.now)
        row = monitor.collect(["a"])["sandboxes"]["a"]
        self.assertEqual(row["status"], "critical")
        self.assertIn("guest_oom_kill", row["reasons"])
        self.assertEqual(row["memory"]["oom_kill_total"], 1)

    def test_memory_warning_is_debounced(self) -> None:
        self.fake.probe_value = guest_probe_with_available(700 * 1024)
        monitor = make_monitor(self.fake, self.root, self.now)
        first = monitor.collect(["a"])["sandboxes"]["a"]
        self.assertEqual(first["status"], "ok")
        self.now[0] += 5.0
        second = monitor.collect(["a"])["sandboxes"]["a"]
        self.assertEqual(second["status"], "warn")
        self.assertIn("guest_memory_available_low", second["reasons"])

    def test_cpu_saturation_requires_three_samples(self) -> None:
        self.fake.stats_value = stats_row("180.0%")
        monitor = make_monitor(self.fake, self.root, self.now)
        self.assertEqual(monitor.collect(["a"])["sandboxes"]["a"]["status"], "ok")
        self.now[0] += 5.0
        self.assertEqual(monitor.collect(["a"])["sandboxes"]["a"]["status"], "ok")
        self.now[0] += 5.0
        third = monitor.collect(["a"])["sandboxes"]["a"]
        self.assertEqual(third["status"], "warn")
        self.assertIn("cpu_saturated", third["reasons"])

    def test_host_pressure_is_reported_separately(self) -> None:
        monitor = make_monitor(self.fake, self.root, self.now)
        monitor._host_stats_provider = lambda: {
            "status": "warn",
            "reasons": ["host_memory_low"],
            "mem_available_pct": 12.0,
        }
        payload = monitor.collect(["a"])
        row = payload["sandboxes"]["a"]
        self.assertEqual(payload["host"]["status"], "warn")
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["health_source"], "host")

    def test_stopped_sandbox_returns_unknown(self) -> None:
        self.fake.running = False
        monitor = make_monitor(self.fake, self.root, self.now)
        row = monitor.collect(["a"])["sandboxes"]["a"]
        self.assertFalse(row["running"])
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["error"], "not running")
        self.assertEqual(self.fake.stats_calls, 0)

    def test_unknown_sandbox_id_is_rejected(self) -> None:
        monitor = make_monitor(self.fake, self.root, self.now)
        with self.assertRaises(ValueError):
            monitor.collect(["missing"])


class ManagerCommandTests(unittest.TestCase):
    """Verify the manager's read-only monitoring commands."""

    def test_stats_uses_explicit_container_names(self) -> None:
        manager = DockerSandboxManager({"sandboxes": {"items": {}}, "root": "."})
        with patch.object(manager, "_run_docker_cmd", return_value=(True, '{"Name":"sndbx-a"}\n')) as run:
            ok, rows = manager.get_container_stats(["a"])
        self.assertTrue(ok)
        self.assertEqual(rows[0]["Name"], "sndbx-a")
        command = run.call_args[0][0]
        self.assertEqual(command[:2], ["stats", "--no-stream"])
        self.assertIn("sndbx-a", command)

    def test_guest_probe_is_read_only_and_fixed(self) -> None:
        manager = DockerSandboxManager({"sandboxes": {"items": {}}, "root": "."})
        with patch.object(manager, "_run_docker_cmd", return_value=(True, "@@MEMINFO\n")) as run:
            ok, _ = manager.probe_guest_metrics("a")
        self.assertTrue(ok)
        command = run.call_args[0][0]
        self.assertEqual(command[:3], ["exec", "sndbx-a", "sh"])
        self.assertIn("cat /proc/meminfo", command[-1])
        self.assertNotIn("; rm ", command[-1])

    def test_disk_size_uses_inspect_size(self) -> None:
        manager = DockerSandboxManager({"sandboxes": {"items": {}}, "root": "."})
        with patch.object(manager, "_run_docker_cmd", return_value=(True, "1048576\n")) as run:
            ok, value = manager.get_container_disk_size("a")
        self.assertTrue(ok)
        self.assertEqual(value, "1048576")
        command = run.call_args[0][0]
        self.assertEqual(command[:3], ["inspect", "--size", "--format"])


async def asgi_call(app, method: str, path: str, headers: dict[str, str] | None = None, body: bytes = b""):
    """Call an ASGI app and return (status, headers, body bytes)."""
    header_list = [(b"host", b"testserver")]
    for key, value in (headers or {}).items():
        header_list.append((key.lower().encode(), value.encode()))
    target = urlsplit(path)
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "path": target.path,
        "raw_path": target.path.encode(),
        "query_string": target.query.encode(),
        "headers": header_list,
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 1),
        "root_path": "",
    }
    messages = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    response_headers = dict(
        next(message["headers"] for message in messages if message["type"] == "http.response.start")
    )
    payload = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return status, response_headers, payload


class MonitoringHttpTests(unittest.TestCase):
    """Verify monitor endpoint authentication and validation."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        frontend = self.root / "src" / "webui" / "frontend"
        frontend.parent.mkdir(parents=True, exist_ok=True)
        frontend.symlink_to(ROOT / "src" / "webui" / "frontend", target_is_directory=True)
        self.fake = FakeManager()
        config = {
            "root": str(self.root),
            "sandboxes": {"items": self.fake.sandbox_configs},
            "webui": {
                "port": 30080,
                "bind": "127.0.0.1",
                "auth": {
                    "users": [{"login": "admin", "password": "secret", "permissions": ["all"]}],
                    "session_ttl": 3600,
                },
            },
            "ssh": {"port_range": [30200, 30210]},
        }
        self.server = WebUIServer(self.root, config, self.fake)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _login_cookie(self) -> str:
        status, headers, _ = asyncio.run(asgi_call(
            self.server.app,
            "POST",
            "/api/auth/login",
            headers={"content-type": "application/json"},
            body=json.dumps({"login": "admin", "password": "secret"}).encode(),
        ))
        self.assertEqual(status, 200)
        cookie = headers.get(b"set-cookie", b"").decode()
        return cookie.split(";", 1)[0]

    def test_monitor_requires_authentication(self) -> None:
        status, _, _ = asyncio.run(asgi_call(self.server.app, "GET", "/api/monitor?ids=a"))
        self.assertEqual(status, 401)

    def test_monitor_returns_data_for_authenticated_user(self) -> None:
        cookie = self._login_cookie()
        status, _, payload = asyncio.run(asgi_call(
            self.server.app,
            "GET",
            "/api/monitor?ids=a",
            headers={"cookie": cookie},
        ))
        self.assertEqual(status, 200)
        data = json.loads(payload)
        self.assertTrue(data["ok"])
        self.assertIn("a", data["sandboxes"])

    def test_monitor_rejects_unknown_id(self) -> None:
        cookie = self._login_cookie()
        status, _, payload = asyncio.run(asgi_call(
            self.server.app,
            "GET",
            "/api/monitor?ids=missing",
            headers={"cookie": cookie},
        ))
        self.assertEqual(status, 400)
        self.assertIn(b"Unknown sandbox id", payload)


if __name__ == "__main__":
    unittest.main()
