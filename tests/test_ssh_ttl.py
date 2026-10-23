"""Tests for the SSH forwarder session TTL."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from webui_server import SSHManager


class SshSessionTtlTests(unittest.TestCase):
    """Verify that expired forwarder reservations are released."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_allocation(self, sandbox_id: str, allocated_at: datetime, pid: int = 0) -> None:
        """Write one allocation record."""
        path = self.root / "data" / "ssh_allocations.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        data[sandbox_id] = {
            "port": 30200,
            "socat_pid": pid,
            "container_ip": "10.0.0.5",
            "allocated_at": allocated_at.isoformat(),
        }
        path.write_text(json.dumps(data), encoding="utf-8")

    def _allocations(self) -> dict:
        """Return the current allocation map."""
        path = self.root / "data" / "ssh_allocations.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def test_releases_an_expired_reservation(self) -> None:
        """Drop a forwarder that is older than the TTL."""
        manager = SSHManager(self.root, [30200, 30210], session_ttl_seconds=3600)
        now = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)
        self._write_allocation("old", now - timedelta(hours=2))

        expired = manager.expire_stale(now)

        self.assertEqual(expired, ["old"])
        self.assertEqual(self._allocations(), {})

    def test_keeps_a_recent_reservation(self) -> None:
        """Keep a forwarder that is still within the TTL."""
        manager = SSHManager(self.root, [30200, 30210], session_ttl_seconds=3600)
        now = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)
        self._write_allocation("fresh", now - timedelta(minutes=10))

        expired = manager.expire_stale(now)

        self.assertEqual(expired, [])
        self.assertIn("fresh", self._allocations())

    def test_zero_ttl_disables_expiry(self) -> None:
        """Keep every reservation when the TTL is not positive."""
        manager = SSHManager(self.root, [30200, 30210], session_ttl_seconds=0)
        now = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)
        self._write_allocation("old", now - timedelta(days=30))

        expired = manager.expire_stale(now)

        self.assertEqual(expired, [])
        self.assertIn("old", self._allocations())

    def test_ignores_records_without_a_timestamp(self) -> None:
        """Keep a record whose allocated_at cannot be parsed."""
        manager = SSHManager(self.root, [30200, 30210], session_ttl_seconds=3600)
        path = self.root / "data" / "ssh_allocations.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"broken": {"port": 30200}}), encoding="utf-8")

        expired = manager.expire_stale(datetime(2026, 10, 23, tzinfo=timezone.utc))

        self.assertEqual(expired, [])
        self.assertIn("broken", self._allocations())

    def test_defaults_to_an_hour(self) -> None:
        """Use the documented one-hour default when nothing is configured."""
        manager = SSHManager(self.root, [30200, 30210])
        self.assertEqual(manager.session_ttl_seconds, 3600)


if __name__ == "__main__":
    unittest.main()
