"""Tests for the centralized log wipe."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from logging_utils import wipe_expired_logs


def iso(moment: datetime) -> str:
    """Return an ISO-8601 timestamp with milliseconds."""
    return moment.isoformat(timespec="milliseconds")


class LogWipeTests(unittest.TestCase):
    """Verify that expired log entries are removed and recent ones are kept."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.logs = Path(self.temp.name)
        self.now = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_text_log(self, name: str, moments: list[datetime]) -> Path:
        """Write a text log with one line per timestamp."""
        path = self.logs / name
        lines = [
            f"{iso(moment)} [INFO] [core] message {index}\n"
            for index, moment in enumerate(moments)
        ]
        path.write_text("".join(lines), encoding="utf-8")
        return path

    def test_removes_only_expired_text_lines(self) -> None:
        """Drop lines older than the age and keep the rest."""
        old = self.now - timedelta(hours=5)
        fresh = self.now - timedelta(minutes=5)
        path = self._write_text_log("core.log", [old, fresh, old])

        removed = wipe_expired_logs(self.logs, 3600)

        self.assertEqual(removed.get("core.log"), 2)
        content = path.read_text(encoding="utf-8")
        self.assertIn("message 1", content)
        self.assertNotIn("message 0", content)
        self.assertNotIn("message 2", content)

    def test_keeps_everything_when_nothing_expired(self) -> None:
        """Leave the file untouched when all entries are recent."""
        fresh = self.now - timedelta(minutes=1)
        path = self._write_text_log("core.log", [fresh, fresh])

        removed = wipe_expired_logs(self.logs, 3600)

        self.assertEqual(removed, {})
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)

    def test_removes_expired_jsonl_entries(self) -> None:
        """Drop MCP request log entries older than the age."""
        old = self.now - timedelta(hours=5)
        fresh = self.now - timedelta(minutes=5)
        path = self.logs / "mcp_requests.jsonl"
        entries = [
            {"type": "request", "ts": iso(moment), "data": {"n": index}}
            for index, moment in enumerate([old, fresh])
        ]
        path.write_text(
            "".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8"
        )

        removed = wipe_expired_logs(self.logs, 3600)

        self.assertEqual(removed.get("mcp_requests.jsonl"), 1)
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["data"]["n"], 1)

    def test_preserves_the_file_inode(self) -> None:
        """Rewrite the file in place so open handles stay valid."""
        old = self.now - timedelta(hours=5)
        path = self._write_text_log("core.log", [old])
        before = path.stat().st_ino

        wipe_expired_logs(self.logs, 3600)

        self.assertEqual(path.stat().st_ino, before)
        self.assertEqual(path.read_text(encoding="utf-8"), "")

    def test_zero_age_disables_wiping(self) -> None:
        """Do nothing when the age is not positive."""
        path = self._write_text_log("core.log", [self.now - timedelta(hours=5)])

        removed = wipe_expired_logs(self.logs, 0)

        self.assertEqual(removed, {})
        self.assertTrue(path.read_text(encoding="utf-8"))

    def test_missing_directory_is_not_an_error(self) -> None:
        """Tolerate a missing log directory."""
        self.assertEqual(wipe_expired_logs(self.logs / "missing", 3600), {})

    def test_drops_leading_lines_without_a_timestamp(self) -> None:
        """A log file starts with an entry header; leading orphans are dropped."""
        path = self.logs / "core.log"
        path.write_text("not a timestamp line\n", encoding="utf-8")

        removed = wipe_expired_logs(self.logs, 3600)

        self.assertEqual(removed.get("core.log"), 1)
        self.assertEqual(path.read_text(encoding="utf-8"), "")

    def test_removes_multiline_entries_as_a_whole(self) -> None:
        """Drop the continuation lines of an expired multi-line message."""
        old = self.now - timedelta(hours=5)
        fresh = self.now - timedelta(minutes=5)
        path = self.logs / "core.log"
        path.write_text(
            f"{iso(old)} [ERROR] [core] traceback start\n"
            "  File \"x.py\", line 1\n"
            "    raise RuntimeError\n"
            f"{iso(fresh)} [INFO] [core] fresh entry\n",
            encoding="utf-8",
        )

        removed = wipe_expired_logs(self.logs, 3600)

        self.assertEqual(removed.get("core.log"), 3)
        content = path.read_text(encoding="utf-8")
        self.assertNotIn("traceback start", content)
        self.assertNotIn("raise RuntimeError", content)
        self.assertIn("fresh entry", content)

    def test_removes_orphaned_continuation_lines(self) -> None:
        """Drop continuation lines whose entry header is already gone."""
        path = self.logs / "core.log"
        path.write_text(
            "  File \"x.py\", line 1\n"
            "    raise RuntimeError\n"
            f"{iso(self.now)} [INFO] [core] fresh entry\n",
            encoding="utf-8",
        )

        removed = wipe_expired_logs(self.logs, 3600)

        self.assertEqual(removed.get("core.log"), 2)
        content = path.read_text(encoding="utf-8")
        self.assertNotIn("raise RuntimeError", content)
        self.assertIn("fresh entry", content)


if __name__ == "__main__":
    unittest.main()
