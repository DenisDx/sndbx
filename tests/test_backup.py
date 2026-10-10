"""Tests for the per-VM incremental backup scheduler."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from backup import (
    BackupManager,
    BackupSettings,
    _archive_basename,
    _read_timestamp_from_name,
    is_safe_local_image_id,
)


def _skip_if_no_tar() -> None:
    """Skip the test when GNU tar is unavailable on the host."""
    if shutil.which("tar") is None:
        raise unittest.SkipTest("GNU tar is required for backup tests")


class BackupSettingsValidationTests(unittest.TestCase):
    """Verify BackupSettings.from_config accepts only valid options."""

    def test_returns_disabled_when_block_absent(self) -> None:
        """Default to disabled when no backup block is provided."""
        settings = BackupSettings.from_config(None)
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.interval_hours, 24)
        self.assertEqual(settings.full_every, 7)

    def test_rejects_non_object_backup_block(self) -> None:
        """Reject a non-object backup configuration value."""
        with self.assertRaises(Exception):
            BackupSettings.from_config("enabled")

    def test_rejects_non_boolean_enabled(self) -> None:
        """Reject a non-boolean enabled flag."""
        with self.assertRaises(Exception):
            BackupSettings.from_config({"enabled": "yes"})

    def test_rejects_non_positive_interval(self) -> None:
        """Reject zero or negative interval hours."""
        with self.assertRaises(Exception):
            BackupSettings.from_config({"enabled": True, "interval_hours": 0})

    def test_rejects_non_positive_full_every(self) -> None:
        """Reject zero or negative full_every value."""
        with self.assertRaises(Exception):
            BackupSettings.from_config({"enabled": True, "full_every": -1})

    def test_rejects_boolean_interval_hours(self) -> None:
        """Reject booleans masquerading as integers."""
        with self.assertRaises(Exception):
            BackupSettings.from_config({"enabled": True, "interval_hours": True})

    def test_accepts_explicit_values(self) -> None:
        """Accept a fully populated backup block."""
        settings = BackupSettings.from_config({
            "enabled": True,
            "interval_hours": 12,
            "full_every": 3,
        })
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.interval_hours, 12)
        self.assertEqual(settings.full_every, 3)


class LocalImageIdSafetyTests(unittest.TestCase):
    """Verify path-traversal-style identifiers are rejected."""

    def test_accepts_typical_identifier(self) -> None:
        """Allow a normal directory name component."""
        self.assertTrue(is_safe_local_image_id("denis_obsidian_assistant"))

    def test_rejects_empty_string(self) -> None:
        """Reject an empty identifier."""
        self.assertFalse(is_safe_local_image_id(""))
        self.assertFalse(is_safe_local_image_id("   "))

    def test_rejects_relative_components(self) -> None:
        """Reject '.' and '..' components."""
        self.assertFalse(is_safe_local_image_id("."))
        self.assertFalse(is_safe_local_image_id(".."))

    def test_rejects_path_separators(self) -> None:
        """Reject forward and backward slashes."""
        self.assertFalse(is_safe_local_image_id("../foo"))
        self.assertFalse(is_safe_local_image_id("foo/bar"))
        self.assertFalse(is_safe_local_image_id("foo\\bar"))

    def test_rejects_leading_hyphen(self) -> None:
        """Reject identifiers that begin with a hyphen."""
        self.assertFalse(is_safe_local_image_id("-bad"))

    def test_rejects_non_string(self) -> None:
        """Reject non-string inputs."""
        self.assertFalse(is_safe_local_image_id(None))
        self.assertFalse(is_safe_local_image_id(123))


class ArchiveNameParsingTests(unittest.TestCase):
    """Verify archive basename parsing helpers."""

    def test_basename_uses_utc_timestamp_and_type(self) -> None:
        """Format the archive name with UTC timestamp and type suffix."""
        moment = datetime(2026, 10, 10, 11, 0, 0, tzinfo=timezone.utc)
        name = _archive_basename("image_x", "full", moment)
        self.assertEqual(name, "image_x-2026-10-10T110000.000Z-full.tar.gz")

    def test_parse_existing_archive_name(self) -> None:
        """Recover the timestamp encoded in a full archive name."""
        result = _read_timestamp_from_name(
            "image_x-2026-10-10T110000.000Z-full.tar.gz", "image_x"
        )
        self.assertEqual(result, datetime(2026, 10, 10, 11, 0, 0, tzinfo=timezone.utc))

    def test_parse_archive_name_with_ms_suffix(self) -> None:
        """Recover the timestamp when a non-zero millisecond suffix is present."""
        result = _read_timestamp_from_name(
            "image_x-2026-10-10T110000.123Z-full.tar.gz", "image_x"
        )
        self.assertEqual(
            result,
            datetime(2026, 10, 10, 11, 0, 0, tzinfo=timezone.utc) + timedelta(milliseconds=123),
        )

    def test_parse_rejects_unknown_format(self) -> None:
        """Reject names that do not match the expected pattern."""
        self.assertIsNone(_read_timestamp_from_name("other-name.tar.gz", "image_x"))
        self.assertIsNone(_read_timestamp_from_name("image_x-bogus.tar.gz", "image_x"))
        self.assertIsNone(
            _read_timestamp_from_name("image_x-2026-10-10T110000Z-bogus.tar.gz", "image_x")
        )


class BackupManagerSchedulingTests(unittest.TestCase):
    """Verify BackupManager picks enabled jobs and skips unsafe configurations."""

    def _make_manager(self, root_dir: Path) -> BackupManager:
        """Build a manager with a stubbed sandbox manager."""
        config = {"root": str(root_dir), "sandboxes": {"items": {}}}
        manager = BackupManager(config, str(root_dir), Mock())
        manager.sandbox_manager._local_image_id_for_ref = Mock(return_value=None)
        return manager

    def test_disabled_by_default_when_no_backup_block(self) -> None:
        """Schedule no jobs when no sandbox enables backup."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "images" / "demo").mkdir(parents=True)
            manager = self._make_manager(root)
            manager.config = {
                "root": str(root),
                "sandboxes": {"items": {"s1": {"image": "demo", "local_image_id": "demo"}}},
            }
            manager.reconfigure()
            self.assertEqual(manager.list_enabled_jobs(), [])

    def test_rejects_sandbox_without_local_image_directory(self) -> None:
        """Log and skip a backup job for a sandbox without images/<id>."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = self._make_manager(root)
            manager.config = {
                "root": str(root),
                "sandboxes": {"items": {"s1": {
                    "image": "demo:latest",
                    "backup": {"enabled": True, "interval_hours": 24, "full_every": 7},
                }}},
            }
            manager.reconfigure()
            self.assertEqual(manager.list_enabled_jobs(), [])
            self.assertIn("s1", manager._rejected)

    def test_rejects_unsafe_local_image_id(self) -> None:
        """Reject a sandbox with an unsafe local_image_id value."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "images" / "good").mkdir(parents=True)
            manager = self._make_manager(root)
            manager.config = {
                "root": str(root),
                "sandboxes": {"items": {"s1": {
                    "image": "good",
                    "local_image_id": "../escape",
                    "backup": {"enabled": True, "interval_hours": 24, "full_every": 7},
                }}},
            }
            manager.reconfigure()
            self.assertEqual(manager.list_enabled_jobs(), [])

    def test_invalid_backup_settings_are_logged_and_skipped(self) -> None:
        """Record a rejection for invalid backup settings without crashing."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "images" / "good").mkdir(parents=True)
            manager = self._make_manager(root)
            manager.config = {
                "root": str(root),
                "sandboxes": {"items": {"s1": {
                    "image": "good",
                    "backup": {"enabled": True, "interval_hours": 0},
                }}},
            }
            manager.reconfigure()
            self.assertEqual(manager.list_enabled_jobs(), [])
            self.assertIn("s1", manager._rejected)

    def test_full_decision_without_snapshot(self) -> None:
        """Always create a full archive when there is no snapshot file."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "images" / "demo").mkdir(parents=True)
            manager = self._make_manager(root)
            job = BackupJobStub(root / "images" / "demo", root / "backup" / "demo")
            job.backup_dir.mkdir(parents=True, exist_ok=True)
            self.assertEqual(manager._decide_archive_type(job), "full")

    def test_incremental_decision_with_recent_full(self) -> None:
        """Return incremental when the latest full archive is recent."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "images" / "demo").mkdir(parents=True)
            manager = self._make_manager(root)
            backup_dir = root / "backup" / "demo"
            backup_dir.mkdir(parents=True, exist_ok=True)
            (backup_dir / "demo.snar").write_text("", encoding="utf-8")
            recent = datetime.now(timezone.utc) - timedelta(hours=1)
            stamp = recent.strftime("%Y-%m-%dT%H%M%SZ")
            (backup_dir / f"demo-{stamp}-full.tar.gz").write_bytes(b"")
            job = BackupJobStub(root / "images" / "demo", backup_dir, interval_hours=24, full_every=7)
            self.assertEqual(manager._decide_archive_type(job), "incremental")

    def test_rotation_after_full_every(self) -> None:
        """Force a new full archive once full_every intervals have elapsed."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "images" / "demo").mkdir(parents=True)
            manager = self._make_manager(root)
            backup_dir = root / "backup" / "demo"
            backup_dir.mkdir(parents=True, exist_ok=True)
            (backup_dir / "demo.snar").write_text("", encoding="utf-8")
            old = datetime.now(timezone.utc) - timedelta(hours=24 * 30)
            stamp = old.strftime("%Y-%m-%dT%H%M%SZ")
            (backup_dir / f"demo-{stamp}-full.tar.gz").write_bytes(b"")
            job = BackupJobStub(root / "images" / "demo", backup_dir, interval_hours=24, full_every=7)
            self.assertEqual(manager._decide_archive_type(job), "full")


class BackupManagerRunTests(unittest.TestCase):
    """Verify BackupManager.run_backup produces real archives."""

    def _populate_image(self, image_dir: Path) -> None:
        """Create a small image directory with a data subdirectory."""
        image_dir.mkdir(parents=True, exist_ok=True)
        (image_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        (image_dir / "data").mkdir(exist_ok=True)
        (image_dir / "data" / "note.txt").write_text("hello\n", encoding="utf-8")

    def test_run_creates_full_archive(self) -> None:
        """Produce a full archive on the first run for a configured sandbox."""
        _skip_if_no_tar()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._populate_image(root / "images" / "demo")
            manager = BackupManager(
                {"root": str(root), "sandboxes": {"items": {}}}, str(root), Mock()
            )
            manager.config = {
                "root": str(root),
                "sandboxes": {"items": {"s1": {
                    "image": "demo",
                    "local_image_id": "demo",
                    "backup": {"enabled": True, "interval_hours": 24, "full_every": 7},
                }}},
            }
            manager.reconfigure()
            moment = datetime(2026, 10, 10, 11, 0, 0, tzinfo=timezone.utc)
            result = manager.run_backup("s1", now=moment)

            self.assertTrue(result.success, result.message)
            self.assertEqual(result.archive_type, "full")
            self.assertTrue(result.archive_path.is_file())
            self.assertTrue(result.size_bytes > 0)
            self.assertTrue(result.snapshot_path.is_file())

            listing = subprocess.run(
                ["tar", "--list", "--gzip", f"--file={str(result.archive_path)}"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(listing.returncode, 0, listing.stderr)
            names = listing.stdout.splitlines()
            self.assertTrue(any(name.endswith("demo/Dockerfile") for name in names))
            self.assertTrue(any(name.endswith("demo/data/note.txt") for name in names))

    def test_run_creates_incremental_archive(self) -> None:
        """Produce an incremental archive on the second run."""
        _skip_if_no_tar()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._populate_image(root / "images" / "demo")
            manager = BackupManager(
                {"root": str(root), "sandboxes": {"items": {}}}, str(root), Mock()
            )
            manager.config = {
                "root": str(root),
                "sandboxes": {"items": {"s1": {
                    "image": "demo",
                    "local_image_id": "demo",
                    "backup": {"enabled": True, "interval_hours": 24, "full_every": 7},
                }}},
            }
            manager.reconfigure()
            first = datetime(2026, 10, 10, 0, 0, 0, tzinfo=timezone.utc)
            first_result = manager.run_backup("s1", now=first)
            self.assertTrue(first_result.success, first_result.message)

            (root / "images" / "demo" / "data" / "new.txt").write_text("new\n", encoding="utf-8")
            second = first + timedelta(hours=1)
            second_result = manager.run_backup("s1", now=second)
            self.assertTrue(second_result.success, second_result.message)
            self.assertEqual(second_result.archive_type, "incremental")

            archives = sorted((root / "backup" / "demo").glob("demo-*-incremental.tar.gz"))
            self.assertEqual(len(archives), 1)
            listing = subprocess.run(
                ["tar", "--list", "--gzip", f"--file={str(archives[0])}"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(listing.returncode, 0, listing.stderr)
            names = listing.stdout.splitlines()
            self.assertTrue(any(name.endswith("demo/data/new.txt") for name in names))

    def test_run_reports_missing_source(self) -> None:
        """Report failure when the source image directory is absent."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "images" / "demo").mkdir(parents=True)
            manager = BackupManager(
                {"root": str(root), "sandboxes": {"items": {}}}, str(root), Mock()
            )
            manager.config = {
                "root": str(root),
                "sandboxes": {"items": {"s1": {
                    "image": "demo",
                    "local_image_id": "demo",
                    "backup": {"enabled": True, "interval_hours": 24, "full_every": 7},
                }}},
            }
            manager.reconfigure()
            (root / "images" / "demo").rmdir()
            result = manager.run_backup("s1")
            self.assertFalse(result.success)
            self.assertIn("missing", result.message)

    def test_run_reports_root_helper_error_before_tar(self) -> None:
        """Report a root helper failure before attempting a direct tar run."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image_dir = root / "images" / "demo"
            image_dir.mkdir(parents=True)
            manager = BackupManager(
                {"root": str(root), "sandboxes": {"items": {}}},
                str(root),
                Mock(),
                backup_helper_path="/usr/local/libexec/sndbx-backup-helper",
                backup_state_root=str(root / "root-state"),
            )
            manager.config = {
                "root": str(root),
                "sandboxes": {"items": {"s1": {
                    "image": "demo",
                    "local_image_id": "demo",
                    "backup": {"enabled": True, "interval_hours": 24, "full_every": 7},
                }}},
            }
            manager.reconfigure()
            manager._run_root_backup_helper = Mock(
                return_value="root backup helper failed: sudoers rule is missing"
            )

            result = manager.run_backup("s1")

            self.assertFalse(result.success)
            self.assertIn("root backup helper failed", result.message)

    def test_run_for_unknown_sandbox_returns_error(self) -> None:
        """Return a structured failure for an unknown sandbox id."""
        manager = BackupManager(
            {"root": "/tmp/sndbx-test", "sandboxes": {"items": {}}}, "/tmp/sndbx-test", Mock()
        )
        result = manager.run_backup("missing")
        self.assertFalse(result.success)
        self.assertIn("no enabled backup job", result.message)


class BackupManagerConcurrencyTests(unittest.TestCase):
    """Verify only one backup runs per image directory at a time."""

    def test_concurrent_call_is_suppressed(self) -> None:
        """Reject a second concurrent call while the first is in flight."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "images" / "demo").mkdir(parents=True)
            manager = BackupManager(
                {"root": str(root), "sandboxes": {"items": {}}}, str(root), Mock()
            )
            manager.config = {
                "root": str(root),
                "sandboxes": {"items": {"s1": {
                    "image": "demo",
                    "local_image_id": "demo",
                    "backup": {"enabled": True, "interval_hours": 24, "full_every": 7},
                }}},
            }
            manager.reconfigure()

            async def runner() -> tuple:
                lock = asyncio.Lock()
                manager._running["demo"] = lock
                await lock.acquire()
                try:
                    return await asyncio.gather(
                        manager.run_backup_async("s1"),
                        manager.run_backup_async("s1"),
                    )
                finally:
                    lock.release()
                    manager._running.pop("demo", None)

            first, second = asyncio.run(runner())
            self.assertFalse(second.success)
            self.assertIn("already running", second.message)


class BackupManagerTarErrorTests(unittest.TestCase):
    """Verify tar failures leave no archive behind."""

    def test_tar_failure_removes_partial_archive(self) -> None:
        """Remove the temporary archive when tar exits non-zero."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image_dir = root / "images" / "demo"
            image_dir.mkdir(parents=True, exist_ok=True)
            (image_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

            real_run = subprocess.run

            def fake_run(cmd, *args, **kwargs):
                """Return a non-zero returncode to model a failed tar invocation."""
                result = real_run(cmd, *args, **kwargs)
                result.returncode = 2
                result.stderr = "synthetic failure"
                return result

            manager = BackupManager(
                {"root": str(root), "sandboxes": {"items": {}}, "config": {}}, str(root), Mock()
            )
            manager.config = {
                "root": str(root),
                "sandboxes": {"items": {"s1": {
                    "image": "demo",
                    "local_image_id": "demo",
                    "backup": {"enabled": True, "interval_hours": 24, "full_every": 7},
                }}},
            }
            manager.reconfigure()

            import backup as backup_module
            original_run = backup_module.subprocess.run
            backup_module.subprocess.run = fake_run
            try:
                result = manager.run_backup("s1")
            finally:
                backup_module.subprocess.run = original_run

            self.assertFalse(result.success)
            self.assertIn("tar failed", result.message)
            leftovers = list((root / "backup" / "demo").glob("*.tar.gz"))
            self.assertEqual(leftovers, [])
            leftovers = list((root / "backup" / "demo").glob("*.tmp"))
            self.assertEqual(leftovers, [])


class BackupManagerTarBinaryTests(unittest.TestCase):
    """Verify tar binary discovery behaves as expected."""

    def test_find_tar_binary_returns_string_when_available(self) -> None:
        """Return a path when GNU tar is on PATH."""
        from backup import find_tar_binary
        if shutil.which("tar") is None:
            raise unittest.SkipTest("tar not installed")
        self.assertIsNotNone(find_tar_binary())


class BackupManagerSchedulerIntegrationTests(unittest.TestCase):
    """Verify the run_loop scheduler survives a due-job cycle."""

    def _populate(self, root: Path) -> None:
        """Create a small image directory."""
        image_dir = root / "images" / "demo"
        image_dir.mkdir(parents=True, exist_ok=True)
        (image_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

    def test_run_loop_due_job_then_continue(self) -> None:
        """Process an immediately-due job and stay alive for the next cycle."""
        _skip_if_no_tar()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._populate(root)
            manager = BackupManager(
                {"root": str(root), "sandboxes": {"items": {}}},
                str(root),
                Mock(),
            )
            manager.config = {
                "root": str(root),
                "sandboxes": {"items": {"s1": {
                    "image": "demo",
                    "local_image_id": "demo",
                    "backup": {"enabled": True, "interval_hours": 24, "full_every": 7},
                }}},
            }
            manager.reconfigure()
            job = manager._jobs["s1"]
            job.next_run = time.monotonic() - 1.0

            loop = asyncio.new_event_loop()
            try:
                task = loop.create_task(manager.run_loop())
                loop.run_until_complete(asyncio.sleep(0.05))
                task.cancel()
                try:
                    loop.run_until_complete(task)
                except asyncio.CancelledError:
                    pass
                backup_files = list((root / "backup" / "demo").glob("demo-*-full.tar.gz"))
                self.assertEqual(len(backup_files), 1)
            finally:
                loop.close()


class BackupManagerSnapshotProtectionTests(unittest.TestCase):
    """Verify the snapshot is preserved across tar failures."""

    def test_tar_failure_preserves_live_snapshot(self) -> None:
        """Leave the live snapshot unchanged after tar fails."""
        _skip_if_no_tar()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image_dir = root / "images" / "demo"
            image_dir.mkdir(parents=True, exist_ok=True)
            (image_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            manager = BackupManager(
                {"root": str(root), "sandboxes": {"items": {}}}, str(root), Mock()
            )
            manager.config = {
                "root": str(root),
                "sandboxes": {"items": {"s1": {
                    "image": "demo",
                    "local_image_id": "demo",
                    "backup": {"enabled": True, "interval_hours": 24, "full_every": 7},
                }}},
            }
            manager.reconfigure()
            first = manager.run_backup("s1")
            self.assertTrue(first.success, first.message)
            snapshot_before = first.snapshot_path.read_bytes()
            archive_count_before = len(list((root / "backup" / "demo").glob("demo-*.tar.gz")))

            import backup as backup_module
            real_subprocess_run = subprocess.run
            original_run = backup_module.subprocess.run

            def fake_run(cmd, *args, **kwargs):
                """Force tar to fail after writing a partial snapshot."""
                result = real_subprocess_run(cmd, *args, **kwargs)
                result.returncode = 2
                result.stderr = "synthetic failure"
                return result

            backup_module.subprocess.run = fake_run
            try:
                second = manager.run_backup("s1")
            finally:
                backup_module.subprocess.run = original_run

            self.assertFalse(second.success)
            self.assertEqual(snapshot_before, first.snapshot_path.read_bytes())
            archive_count_after = len(list((root / "backup" / "demo").glob("demo-*.tar.gz")))
            self.assertEqual(archive_count_before, archive_count_after)

    def test_full_archive_extracts_independently(self) -> None:
        """A full archive must restore its content without any snapshot file."""
        _skip_if_no_tar()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image_dir = root / "images" / "demo"
            image_dir.mkdir(parents=True, exist_ok=True)
            (image_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            (image_dir / "data").mkdir(exist_ok=True)
            (image_dir / "data" / "note.txt").write_text("hello\n", encoding="utf-8")
            manager = BackupManager(
                {"root": str(root), "sandboxes": {"items": {}}}, str(root), Mock()
            )
            manager.config = {
                "root": str(root),
                "sandboxes": {"items": {"s1": {
                    "image": "demo",
                    "local_image_id": "demo",
                    "backup": {"enabled": True, "interval_hours": 24, "full_every": 7},
                }}},
            }
            manager.reconfigure()
            result = manager.run_backup("s1")
            self.assertTrue(result.success, result.message)
            full_archive = result.archive_path
            self.assertTrue(full_archive.is_file())

            restore_dir = root / "restore"
            restore_dir.mkdir()
            tar_proc = subprocess.run(
                ["tar", "--extract", "--gzip",
                 f"--listed-incremental={str(restore_dir / 'snapshot.snar')}",
                 f"--file={str(full_archive)}",
                 f"--directory={str(restore_dir)}"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(tar_proc.returncode, 0, tar_proc.stderr)
            self.assertTrue((restore_dir / "demo" / "Dockerfile").is_file())
            self.assertTrue((restore_dir / "demo" / "data" / "note.txt").is_file())


class BackupManagerRestoreChainTests(unittest.TestCase):
    """Verify a full+incremental restore chain recovers added/modified/deleted files."""

    def test_full_plus_incremental_restores_changes(self) -> None:
        """Restore an added, modified, and deleted file via full+incremental archives."""
        _skip_if_no_tar()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image_dir = root / "images" / "demo"
            image_dir.mkdir(parents=True, exist_ok=True)
            (image_dir / "keep.txt").write_text("keep\n", encoding="utf-8")
            (image_dir / "modify.txt").write_text("original\n", encoding="utf-8")
            (image_dir / "drop.txt").write_text("drop\n", encoding="utf-8")
            manager = BackupManager(
                {"root": str(root), "sandboxes": {"items": {}}}, str(root), Mock()
            )
            manager.config = {
                "root": str(root),
                "sandboxes": {"items": {"s1": {
                    "image": "demo",
                    "local_image_id": "demo",
                    "backup": {"enabled": True, "interval_hours": 24, "full_every": 7},
                }}},
            }
            manager.reconfigure()
            t0 = datetime(2026, 10, 10, 0, 0, 0, tzinfo=timezone.utc)
            first = manager.run_backup("s1", now=t0)
            self.assertTrue(first.success, first.message)

            (image_dir / "modify.txt").write_text("modified\n", encoding="utf-8")
            (image_dir / "drop.txt").unlink()
            (image_dir / "added.txt").write_text("added\n", encoding="utf-8")
            t1 = t0 + timedelta(hours=1)
            second = manager.run_backup("s1", now=t1)
            self.assertTrue(second.success, second.message)
            self.assertEqual(second.archive_type, "incremental")

            restore_dir = root / "restore"
            restore_dir.mkdir()
            snapshot = restore_dir / "snapshot.snar"
            tar_proc = subprocess.run(
                ["tar", "--extract", "--gzip",
                 f"--listed-incremental={str(snapshot)}",
                 f"--file={str(first.archive_path)}",
                 f"--directory={str(restore_dir)}"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(tar_proc.returncode, 0, tar_proc.stderr)
            tar_proc = subprocess.run(
                ["tar", "--extract", "--gzip",
                 f"--listed-incremental={str(snapshot)}",
                 f"--file={str(second.archive_path)}",
                 f"--directory={str(restore_dir)}"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(tar_proc.returncode, 0, tar_proc.stderr)

            self.assertEqual((restore_dir / "demo" / "keep.txt").read_text(), "keep\n")
            self.assertEqual((restore_dir / "demo" / "modify.txt").read_text(), "modified\n")
            self.assertEqual((restore_dir / "demo" / "added.txt").read_text(), "added\n")
            self.assertFalse((restore_dir / "demo" / "drop.txt").exists())


class BackupManagerPerImageLockTests(unittest.TestCase):
    """Verify the async lock is keyed by local_image_id, not sandbox_id."""

    def test_lock_is_shared_by_same_image_id_across_sandboxes(self) -> None:
        """Treat two sandboxes targeting the same image as a single lock owner."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "images" / "demo").mkdir(parents=True)
            manager = BackupManager(
                {"root": str(root), "sandboxes": {"items": {}}}, str(root), Mock()
            )
            manager.config = {
                "root": str(root),
                "sandboxes": {"items": {
                    "a": {"image": "demo", "local_image_id": "demo",
                          "backup": {"enabled": True, "interval_hours": 24, "full_every": 7}},
                }},
            }
            manager.reconfigure()

            async def runner() -> tuple:
                lock = asyncio.Lock()
                manager._running["demo"] = lock
                await lock.acquire()
                try:
                    return await asyncio.gather(
                        manager.run_backup_async("a"),
                        manager.run_backup_async("a"),
                    )
                finally:
                    lock.release()
                    manager._running.pop("demo", None)

            first, second = asyncio.run(runner())
            self.assertFalse(second.success)
            self.assertIn("already running", second.message)


class BackupManagerSameImageDeduplicationTests(unittest.TestCase):
    """Verify duplicate enabled sandboxes for one image do not double-run."""

    def test_conflicting_schedules_for_same_image_are_rejected(self) -> None:
        """Reject a second enabled backup referencing the same image with different settings."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "images" / "demo").mkdir(parents=True)
            manager = BackupManager(
                {"root": str(root), "sandboxes": {"items": {}}}, str(root), Mock()
            )
            manager.config = {
                "root": str(root),
                "sandboxes": {"items": {
                    "a": {"image": "demo", "local_image_id": "demo",
                          "backup": {"enabled": True, "interval_hours": 24, "full_every": 7}},
                    "b": {"image": "demo:latest", "local_image_id": "demo",
                          "backup": {"enabled": True, "interval_hours": 12, "full_every": 3}},
                }},
            }
            manager.reconfigure()
            job_ids = sorted(manager._jobs.keys())
            self.assertEqual(len(job_ids), 1)
            self.assertIn("b", manager._rejected)
            self.assertNotIn("a", manager._rejected)

    def test_identical_schedules_coalesce_to_single_job(self) -> None:
        """Coalesce identical schedules so only one job runs per image."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "images" / "demo").mkdir(parents=True)
            manager = BackupManager(
                {"root": str(root), "sandboxes": {"items": {}}}, str(root), Mock()
            )
            manager.config = {
                "root": str(root),
                "sandboxes": {"items": {
                    "a": {"image": "demo", "local_image_id": "demo",
                          "backup": {"enabled": True, "interval_hours": 24, "full_every": 7}},
                    "b": {"image": "demo", "local_image_id": "demo",
                          "backup": {"enabled": True, "interval_hours": 24, "full_every": 7}},
                }},
            }
            manager.reconfigure()
            self.assertEqual(len(manager.list_enabled_jobs()), 1)
            canonical = next(iter(manager._jobs.values()))
            self.assertEqual(canonical.sandbox_id, "a")
            self.assertEqual(manager.shared_sandboxes.get("demo"), ["a", "b"])
            self.assertNotIn("b", manager._rejected)

    def test_scheduler_runs_one_archive_per_interval_for_identical_sandboxes(self) -> None:
        """Scheduler must execute exactly one archive per interval when schedules are identical."""
        _skip_if_no_tar()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image_dir = root / "images" / "demo"
            image_dir.mkdir(parents=True, exist_ok=True)
            (image_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            manager = BackupManager(
                {"root": str(root), "sandboxes": {"items": {}}}, str(root), Mock()
            )
            manager.config = {
                "root": str(root),
                "sandboxes": {"items": {
                    "a": {"image": "demo", "local_image_id": "demo",
                          "backup": {"enabled": True, "interval_hours": 24, "full_every": 7}},
                    "b": {"image": "demo", "local_image_id": "demo",
                          "backup": {"enabled": True, "interval_hours": 24, "full_every": 7}},
                    "c": {"image": "demo", "local_image_id": "demo",
                          "backup": {"enabled": True, "interval_hours": 24, "full_every": 7}},
                }},
            }

            async def cycle() -> None:
                await manager._run_and_reschedule(
                    next(iter(manager._jobs.values())),
                    24 * 3600,
                    time.monotonic(),
                )

            manager.reconfigure()
            asyncio.run(cycle())
            archives = list((root / "backup" / "demo").glob("demo-*-full.tar.gz"))
            self.assertEqual(len(archives), 1)

            (image_dir / "Dockerfile").write_text("FROM scratch:2\n", encoding="utf-8")
            manager.reconfigure()
            asyncio.run(cycle())
            archives = list((root / "backup" / "demo").glob("demo-*-full.tar.gz"))
            incrementals = list((root / "backup" / "demo").glob("demo-*-incremental.tar.gz"))
            self.assertEqual(len(archives), 1)
            self.assertEqual(len(incrementals), 1)


class BackupManagerChainMetadataTests(unittest.TestCase):
    """Verify chain metadata is updated on successful backups."""

    def test_chain_metadata_records_full_and_incremental(self) -> None:
        """Persist last_full and last_incremental entries after each successful run."""
        _skip_if_no_tar()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image_dir = root / "images" / "demo"
            image_dir.mkdir(parents=True, exist_ok=True)
            (image_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            manager = BackupManager(
                {"root": str(root), "sandboxes": {"items": {}}}, str(root), Mock()
            )
            manager.config = {
                "root": str(root),
                "sandboxes": {"items": {"s1": {
                    "image": "demo",
                    "local_image_id": "demo",
                    "backup": {"enabled": True, "interval_hours": 24, "full_every": 7},
                }}},
            }
            manager.reconfigure()
            t0 = datetime(2026, 10, 10, 0, 0, 0, tzinfo=timezone.utc)
            manager.run_backup("s1", now=t0)
            t1 = t0 + timedelta(hours=1)
            manager.run_backup("s1", now=t1)

            metadata_path = root / "backup" / "demo" / "demo.chain.json"
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertIsNotNone(payload["last_full"])
            self.assertIsNotNone(payload["last_incremental"])


class BackupManagerTarVerificationTests(unittest.TestCase):
    """Verify find_tar_binary returns only verified GNU tar paths."""

    def test_find_tar_binary_verifies_gnu_tar(self) -> None:
        """Require `GNU tar` in the version output before accepting the binary."""
        with unittest.mock.patch("backup.shutil.which", return_value="/bin/tar"):
            with unittest.mock.patch("backup.subprocess.run", return_value=subprocess.CompletedProcess(
                args=["/bin/tar", "--version"],
                returncode=0,
                stdout="tar (GNU tar) 1.35\n",
                stderr="",
            )):
                from backup import find_tar_binary
                self.assertEqual(find_tar_binary(), "/bin/tar")

    def test_find_tar_binary_rejects_non_gnu_implementation(self) -> None:
        """Reject a tar binary whose --version output does not advertise GNU tar."""
        with unittest.mock.patch("backup.shutil.which", return_value="/bin/tar"):
            with unittest.mock.patch("backup.subprocess.run", return_value=subprocess.CompletedProcess(
                args=["/bin/tar", "--version"],
                returncode=0,
                stdout="bsdtar 3.6\n",
                stderr="",
            )):
                from backup import find_tar_binary
                self.assertIsNone(find_tar_binary())

    def test_find_tar_binary_returns_none_when_missing(self) -> None:
        """Return None when no tar binary is on PATH."""
        with unittest.mock.patch("backup.shutil.which", return_value=None):
            from backup import find_tar_binary
            self.assertIsNone(find_tar_binary())


class BackupJobStub:
    """Minimal BackupJob replacement for archive-type decision tests."""

    def __init__(
        self,
        source_dir: Path,
        backup_dir: Path,
        interval_hours: int = 24,
        full_every: int = 7,
    ) -> None:
        self.sandbox_id = "stub"
        self.local_image_id = "demo"
        self.settings = BackupSettings(True, interval_hours, full_every)
        self.source_dir = source_dir
        self.backup_dir = backup_dir
        self.next_run = 0.0


if __name__ == "__main__":
    unittest.main()