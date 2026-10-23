"""Per-VM incremental backup scheduler for local image directories."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from logging_utils import get_logger

logger = get_logger("backup")


class BackupError(Exception):
    """Backup configuration or execution error."""

    def __init__(self, message: str) -> None:
        """Store the original error message for later logging."""
        super().__init__(message)
        self.message = message


@dataclass
class BackupSettings:
    """Validated per-sandbox backup settings."""

    enabled: bool
    interval_hours: int
    full_every: int

    @classmethod
    def from_config(cls, value: Any) -> "BackupSettings":
        """Parse and validate a `backup` configuration block.

        input: raw config value (typically None or dict)
        output: BackupSettings instance with safe defaults
        """
        if value is None:
            return cls(enabled=False, interval_hours=24, full_every=7)
        if not isinstance(value, dict):
            raise BackupError("backup must be an object")

        enabled = value.get("enabled", False)
        if not isinstance(enabled, bool):
            raise BackupError("backup.enabled must be a boolean")

        interval_hours = value.get("interval_hours", 24)
        if not isinstance(interval_hours, int) or isinstance(interval_hours, bool):
            raise BackupError("backup.interval_hours must be a positive integer")
        if interval_hours <= 0:
            raise BackupError("backup.interval_hours must be a positive integer")

        full_every = value.get("full_every", 7)
        if not isinstance(full_every, int) or isinstance(full_every, bool):
            raise BackupError("backup.full_every must be a positive integer")
        if full_every <= 0:
            raise BackupError("backup.full_every must be a positive integer")

        return cls(enabled=enabled, interval_hours=interval_hours, full_every=full_every)


@dataclass
class BackupJob:
    """One configured backup target."""

    sandbox_id: str
    local_image_id: str
    settings: BackupSettings
    source_dir: Path
    backup_dir: Path
    next_run: float


@dataclass
class BackupResult:
    """Outcome of a single backup run."""

    archive_type: str
    archive_path: Path
    snapshot_path: Path
    size_bytes: int
    duration_seconds: float
    success: bool
    message: str = ""


_LOCAL_IMAGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def is_safe_local_image_id(local_image_id: Any) -> bool:
    """Return True when the value is a safe directory-name component."""
    if not isinstance(local_image_id, str):
        return False
    text = local_image_id.strip()
    if not text or text in {".", ".."}:
        return False
    if any(sep in text for sep in ("/", "\\")):
        return False
    if "\x00" in text:
        return False
    return bool(_LOCAL_IMAGE_ID_PATTERN.match(text))


def _archive_basename(local_image_id: str, archive_type: str, now: datetime) -> str:
    """Build the archive file name from a local id, type, and timestamp.

    The fractional suffix gives collision-free names within the same second.
    """
    stamp = now.strftime("%Y-%m-%dT%H%M%S")
    suffix = f".{now.microsecond // 1000:03d}Z"
    return f"{local_image_id}-{stamp}{suffix}-{archive_type}.tar.gz"


def _resolve_unique_basename(
    backup_dir: Path,
    local_image_id: str,
    archive_type: str,
    start: datetime,
) -> str:
    """Return a non-colliding archive basename within the backup directory."""
    candidate = _archive_basename(local_image_id, archive_type, start)
    if not (backup_dir / candidate).exists():
        return candidate
    base_dt = start
    for attempt in range(1, 1000):
        candidate_dt = base_dt + timedelta(milliseconds=attempt)
        candidate = _archive_basename(local_image_id, archive_type, candidate_dt)
        if not (backup_dir / candidate).exists():
            return candidate
    raise BackupError("could not allocate a unique archive name")


def _read_timestamp_from_name(name: str, local_image_id: str) -> Optional[datetime]:
    """Parse the UTC timestamp from an existing archive file name."""
    prefix = f"{local_image_id}-"
    suffix = ".tar.gz"
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    stamp = name[len(prefix):-len(suffix)]
    parts = stamp.rsplit("-", 1)
    if len(parts) != 2:
        return None
    if parts[1] not in {"full", "incremental"}:
        return None
    raw = parts[0]
    if not raw.endswith("Z"):
        return None
    body = raw[:-1]
    if "." in body:
        base, frac = body.split(".", 1)
        try:
            milliseconds = int(frac[:3].ljust(3, "0")[:3])
        except ValueError:
            return None
    else:
        base = body
        milliseconds = 0
    try:
        parsed = datetime.strptime(base, "%Y-%m-%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return parsed + timedelta(milliseconds=milliseconds)


class BackupManager:
    """Schedules and runs GNU tar incremental backups for local images.

    input: sndbx config dict and root_dir path
    output: tar archives and snapshot files under backup/<local_image_id>/
    """

    def __init__(
        self,
        config: Dict[str, Any],
        root_dir: str,
        sandbox_manager: Any,
        tar_binary: Optional[str] = None,
        backup_helper_path: Optional[str] = None,
        backup_state_root: str = "/var/lib/sndbx-backup",
    ) -> None:
        """Store config, resolved root, and sandbox manager reference."""
        self.config = config or {}
        root = str(self.config.get("root") or root_dir or "").strip() or root_dir
        self.root_dir = Path(root).resolve()
        self.images_dir = self.root_dir / "images"
        self.backup_root = self.root_dir / "backup"
        self.sandbox_manager = sandbox_manager
        self.tar_binary = tar_binary
        self.backup_helper_path = backup_helper_path
        self.backup_state_root = Path(backup_state_root)
        self._jobs: Dict[str, BackupJob] = {}
        self._running: Dict[str, asyncio.Lock] = {}
        self._rejected: Dict[str, str] = {}
        self._rejected_locks: Dict[str, str] = {}
        self._shared_sandboxes: Dict[str, List[str]] = {}

    @property
    def jobs(self) -> Dict[str, BackupJob]:
        """Return the currently scheduled backup jobs."""
        return dict(self._jobs)

    def reconfigure(self) -> None:
        """Re-parse the current config and update the schedule.

        input: none (reads self.config)
        output: updated self._jobs / self._rejected maps and _shared_sandboxes
        """
        items = self.config.get("sandboxes", {}).get("items", {}) or {}
        new_jobs: Dict[str, BackupJob] = {}
        new_shared: Dict[str, List[str]] = {}
        canonical_by_image: Dict[str, str] = {}
        canonical_settings: Dict[str, BackupSettings] = {}
        self._rejected.clear()
        self._rejected_locks.clear()

        for sandbox_id, sandbox_cfg in items.items():
            backup_value = sandbox_cfg.get("backup")
            try:
                settings = BackupSettings.from_config(backup_value)
            except BackupError as error:
                self._rejected[sandbox_id] = error.message
                logger.warning(
                    "Sandbox '%s': invalid backup settings: %s",
                    sandbox_id,
                    error.message,
                )
                continue

            if not settings.enabled:
                continue

            local_image_id = self._resolve_local_image_id(sandbox_id, sandbox_cfg)
            if not local_image_id:
                self._rejected[sandbox_id] = "no local image directory available for this sandbox"
                logger.warning(
                    "Sandbox '%s': backup is enabled but no images/<id> directory was found",
                    sandbox_id,
                )
                continue

            canonical_id = canonical_by_image.get(local_image_id)
            if canonical_id is not None:
                canonical = canonical_settings.get(local_image_id)
                if canonical != settings:
                    self._rejected[sandbox_id] = (
                        f"conflicting backup schedule with sandbox '{canonical_id}' "
                        f"using same images/{local_image_id}"
                    )
                    logger.warning(
                        "Sandbox '%s': backup is enabled but sandbox '%s' already targets "
                        "images/%s with a different schedule; rejecting this conflicting schedule",
                        sandbox_id,
                        canonical_id,
                        local_image_id,
                    )
                    continue
                new_shared.setdefault(local_image_id, [canonical_id]).append(sandbox_id)
                logger.info(
                    "Sandbox '%s' shares the backup schedule of sandbox '%s' for images/%s; "
                    "no extra job will be scheduled",
                    sandbox_id,
                    canonical_id,
                    local_image_id,
                )
                continue

            source_dir = (self.images_dir / local_image_id).resolve()
            backup_dir = (self.backup_root / local_image_id).resolve()

            previous = self._jobs.get(sandbox_id)
            next_run = previous.next_run if previous else time.monotonic()
            new_jobs[sandbox_id] = BackupJob(
                sandbox_id=sandbox_id,
                local_image_id=local_image_id,
                settings=settings,
                source_dir=source_dir,
                backup_dir=backup_dir,
                next_run=next_run,
            )
            canonical_by_image[local_image_id] = sandbox_id
            canonical_settings[local_image_id] = settings

        self._jobs = new_jobs
        self._shared_sandboxes = new_shared
        for sandbox_id in list(self._running):
            if sandbox_id not in self._jobs:
                self._running.pop(sandbox_id, None)

    @property
    def shared_sandboxes(self) -> Dict[str, List[str]]:
        """Return sandboxes that share a backup schedule by `local_image_id`."""
        return {image: list(ids) for image, ids in self._shared_sandboxes.items()}

    def _chain_metadata_path(self, job: BackupJob) -> Path:
        """Return the per-image chain metadata file path."""
        return job.backup_dir / f"{job.local_image_id}.chain.json"

    def _update_chain_metadata(
        self,
        job: BackupJob,
        archive_path: Path,
        archive_type: str,
    ) -> None:
        """Record the latest successful archive in the chain metadata file."""
        metadata_path = self._chain_metadata_path(job)
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        payload = {
            "local_image_id": job.local_image_id,
            "last_full": None,
            "last_incremental": None,
        }
        if metadata_path.exists():
            try:
                payload.update(json.loads(metadata_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                pass
        if archive_type == "full":
            payload["last_full"] = {
                "archive": archive_path.name,
                "timestamp": timestamp,
            }
        else:
            payload["last_incremental"] = {
                "archive": archive_path.name,
                "timestamp": timestamp,
            }
        tmp = metadata_path.with_name(metadata_path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(metadata_path)
        except OSError as error:
            logger.warning(
                "Could not update chain metadata for images/%s: %s",
                job.local_image_id,
                error,
            )

    def _resolve_local_image_id(self, sandbox_id: str, sandbox_cfg: Dict[str, Any]) -> Optional[str]:
        """Resolve a safe local image id for a configured sandbox."""
        configured = str(sandbox_cfg.get("local_image_id", "")).strip()
        if configured:
            if not is_safe_local_image_id(configured):
                return None
            if (self.images_dir / configured).is_dir():
                return configured

        image_ref = str(sandbox_cfg.get("image", "")).strip()
        if image_ref and self.sandbox_manager is not None:
            try:
                resolved = self.sandbox_manager._local_image_id_for_ref(image_ref)
            except Exception:
                resolved = None
            if resolved and is_safe_local_image_id(resolved):
                return resolved

        return None

    def list_enabled_jobs(self) -> List[BackupJob]:
        """Return currently enabled backup jobs."""
        return list(self._jobs.values())

    def _ensure_within_backup_dir(self, path: Path, backup_dir: Path) -> None:
        """Raise BackupError when the path escapes the backup directory."""
        try:
            resolved = path.resolve()
        except OSError as error:
            raise BackupError(f"could not resolve path: {error}")
        if backup_dir not in resolved.parents:
            raise BackupError("archive path escapes backup directory")

    def _run_root_backup_helper(
        self,
        local_image_id: str,
        archive_type: str,
        archive_basename: str,
    ) -> Optional[str]:
        """Run the pre-authorized helper that archives guest-owned files as root."""
        if not self.backup_helper_path:
            return None
        command = [
            "sudo",
            "-n",
            self.backup_helper_path,
            local_image_id,
            archive_type,
            archive_basename,
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except OSError as error:
            return f"could not start root backup helper: {error}"
        except subprocess.TimeoutExpired:
            return "root backup helper timed out"
        if completed.returncode == 0:
            return None
        detail = (completed.stderr or completed.stdout or "").strip()
        return (
            f"root backup helper failed: {detail or f'exit code {completed.returncode}'}. "
            "Install or update it with ./install_backup_acl_helper.sh <service-user>"
        )

    def run_backup(self, sandbox_id: str, *, now: Optional[datetime] = None) -> BackupResult:
        """Create one backup archive synchronously for a sandbox.

        input: sandbox_id and optional override `now` for tests
        output: BackupResult describing the run
        """
        job = self._jobs.get(sandbox_id)
        if not job:
            return BackupResult(
                archive_type="",
                archive_path=Path(),
                snapshot_path=Path(),
                size_bytes=0,
                duration_seconds=0.0,
                success=False,
                message=f"sandbox '{sandbox_id}' has no enabled backup job",
            )

        if not job.source_dir.is_dir():
            return BackupResult(
                archive_type="",
                archive_path=Path(),
                snapshot_path=Path(),
                size_bytes=0,
                duration_seconds=0.0,
                success=False,
                message=f"source directory missing: {job.source_dir}",
            )

        try:
            job.backup_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            return BackupResult(
                archive_type="",
                archive_path=Path(),
                snapshot_path=Path(),
                size_bytes=0,
                duration_seconds=0.0,
                success=False,
                message=f"could not create backup dir: {error}",
            )

        snapshot_path = (
            self.backup_state_root / job.local_image_id / f"{job.local_image_id}.snar"
            if self.backup_helper_path
            else job.backup_dir / f"{job.local_image_id}.snar"
        )
        if not self.backup_helper_path:
            self._ensure_within_backup_dir(snapshot_path, job.backup_dir)

        archive_type = self._decide_archive_type(job, now)
        current = now or datetime.now(timezone.utc)
        archive_basename = _resolve_unique_basename(
            job.backup_dir,
            job.local_image_id,
            archive_type,
            current,
        )
        final_path = job.backup_dir / archive_basename
        self._ensure_within_backup_dir(final_path, job.backup_dir)

        if self.backup_helper_path:
            started = time.monotonic()
            helper_error = self._run_root_backup_helper(
                job.local_image_id,
                archive_type,
                archive_basename,
            )
            duration = time.monotonic() - started
            if helper_error is not None:
                return BackupResult(
                    archive_type=archive_type,
                    archive_path=final_path,
                    snapshot_path=snapshot_path,
                    size_bytes=0,
                    duration_seconds=duration,
                    success=False,
                    message=helper_error,
                )
            self._update_chain_metadata(job, final_path, archive_type)
            size = final_path.stat().st_size if final_path.exists() else 0
            return BackupResult(
                archive_type=archive_type,
                archive_path=final_path,
                snapshot_path=snapshot_path,
                size_bytes=size,
                duration_seconds=duration,
                success=True,
                message="",
            )

        work_snapshot = job.backup_dir / f".{job.local_image_id}.snar.work"
        self._ensure_within_backup_dir(work_snapshot, job.backup_dir)

        if archive_type == "full":
            if work_snapshot.exists():
                try:
                    work_snapshot.unlink()
                except OSError:
                    pass
        else:
            if not snapshot_path.exists():
                return BackupResult(
                    archive_type="full",
                    archive_path=final_path,
                    snapshot_path=snapshot_path,
                    size_bytes=0,
                    duration_seconds=0.0,
                    success=False,
                    message="missing snapshot for incremental backup",
                )
            try:
                shutil.copy2(snapshot_path, work_snapshot)
            except OSError as error:
                return BackupResult(
                    archive_type=archive_type,
                    archive_path=final_path,
                    snapshot_path=snapshot_path,
                    size_bytes=0,
                    duration_seconds=0.0,
                    success=False,
                    message=f"could not stage snapshot: {error}",
                )

        temp_path = final_path.with_name(final_path.name + ".tmp")
        self._ensure_within_backup_dir(temp_path, job.backup_dir)

        tar_bin = self.tar_binary or "tar"
        cmd: List[str] = [
            tar_bin,
            "--create",
            "--gzip",
            f"--listed-incremental={str(work_snapshot)}",
            f"--file={str(temp_path)}",
            "-C",
            str(job.source_dir.parent),
            job.source_dir.name,
        ]

        started = time.monotonic()
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600,
                check=False,
            )
        except subprocess.TimeoutExpired:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            if work_snapshot.exists():
                try:
                    work_snapshot.unlink()
                except OSError:
                    pass
            duration = time.monotonic() - started
            return BackupResult(
                archive_type=archive_type,
                archive_path=final_path,
                snapshot_path=snapshot_path,
                size_bytes=0,
                duration_seconds=duration,
                success=False,
                message="tar command timed out",
            )
        except OSError as error:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            if work_snapshot.exists():
                try:
                    work_snapshot.unlink()
                except OSError:
                    pass
            duration = time.monotonic() - started
            return BackupResult(
                archive_type=archive_type,
                archive_path=final_path,
                snapshot_path=snapshot_path,
                size_bytes=0,
                duration_seconds=duration,
                success=False,
                message=f"could not run tar: {error}",
            )

        duration = time.monotonic() - started
        if completed.returncode != 0:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            if work_snapshot.exists():
                try:
                    work_snapshot.unlink()
                except OSError:
                    pass
            stderr = (completed.stderr or "").strip()
            return BackupResult(
                archive_type=archive_type,
                archive_path=final_path,
                snapshot_path=snapshot_path,
                size_bytes=0,
                duration_seconds=duration,
                success=False,
                message=f"tar failed: {stderr or 'no stderr'}",
            )

        try:
            temp_path.replace(final_path)
        except OSError as error:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            if work_snapshot.exists():
                try:
                    work_snapshot.unlink()
                except OSError:
                    pass
            return BackupResult(
                archive_type=archive_type,
                archive_path=final_path,
                snapshot_path=snapshot_path,
                size_bytes=0,
                duration_seconds=duration,
                success=False,
                message=f"could not finalize archive: {error}",
            )

        try:
            work_snapshot.replace(snapshot_path)
        except OSError as error:
            try:
                final_path.unlink()
            except OSError:
                pass
            return BackupResult(
                archive_type=archive_type,
                archive_path=final_path,
                snapshot_path=snapshot_path,
                size_bytes=0,
                duration_seconds=duration,
                success=False,
                message=f"could not promote snapshot: {error}",
            )

        self._update_chain_metadata(job, final_path, archive_type)
        size = final_path.stat().st_size if final_path.exists() else 0
        return BackupResult(
            archive_type=archive_type,
            archive_path=final_path,
            snapshot_path=snapshot_path,
            size_bytes=size,
            duration_seconds=duration,
            success=True,
            message="",
        )

    def _decide_archive_type(self, job: BackupJob, now: Optional[datetime] = None) -> str:
        """Return 'full' or 'incremental' for the next archive.

        input: backup job and the reference time of this run
        output: archive type string
        """
        snapshot = job.backup_dir / f"{job.local_image_id}.snar"
        if not self.backup_helper_path and not snapshot.exists():
            return "full"

        latest_full = self._latest_full_timestamp(job)
        if latest_full is None:
            return "full"

        reference = now or datetime.now(timezone.utc)
        elapsed_intervals = int(
            (reference - latest_full).total_seconds() // (job.settings.interval_hours * 3600)
        )
        if elapsed_intervals >= job.settings.full_every:
            return "full"
        return "incremental"

    def _latest_full_timestamp(self, job: BackupJob) -> Optional[datetime]:
        """Return the timestamp of the latest successful full archive."""
        metadata_path = self._chain_metadata_path(job)
        if metadata_path.exists():
            try:
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                entry = payload.get("last_full")
                if isinstance(entry, dict):
                    name = entry.get("archive")
                    if isinstance(name, str):
                        return _read_timestamp_from_name(name, job.local_image_id)

        latest_full: Optional[datetime] = None
        for entry in job.backup_dir.iterdir():
            if not entry.is_file():
                continue
            stamp = _read_timestamp_from_name(entry.name, job.local_image_id)
            if stamp is None:
                continue
            if not entry.name.endswith("-full.tar.gz"):
                continue
            if latest_full is None or stamp > latest_full:
                latest_full = stamp
        return latest_full

    async def run_backup_async(self, sandbox_id: str) -> BackupResult:
        """Run a backup with a per-image lock so concurrent calls are no-ops."""
        job = self._jobs.get(sandbox_id)
        if not job:
            return BackupResult(
                archive_type="",
                archive_path=Path(),
                snapshot_path=Path(),
                size_bytes=0,
                duration_seconds=0.0,
                success=False,
                message=f"sandbox '{sandbox_id}' has no enabled backup job",
            )
        lock_key = job.local_image_id
        lock = self._running.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            return BackupResult(
                archive_type="",
                archive_path=Path(),
                snapshot_path=Path(),
                size_bytes=0,
                duration_seconds=0.0,
                success=False,
                message="another backup is already running for this image",
            )
        try:
            async with lock:
                return await asyncio.to_thread(self.run_backup, sandbox_id)
        finally:
            if not lock.locked():
                self._running.pop(lock_key, None)

    async def run_loop(self) -> None:
        """Background loop that executes jobs on their configured intervals."""
        while True:
            try:
                self.reconfigure()
            except Exception as error:
                logger.warning("Backup reconfigure failed: %s", error)

            now = time.monotonic()
            sleep_for: Optional[float] = None
            for sandbox_id, job in list(self._jobs.items()):
                interval_seconds = job.settings.interval_hours * 3600
                wait = job.next_run - now
                if wait <= 0:
                    await self._run_and_reschedule(job, interval_seconds, now)
                    now = time.monotonic()
                    continue
                if sleep_for is None or wait < sleep_for:
                    sleep_for = wait

            if sleep_for is None:
                sleep_for = 60.0
            try:
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                raise

    async def _run_and_reschedule(self, job: BackupJob, interval_seconds: float, now: float) -> None:
        """Run one backup and update the next-run timestamp."""
        result = await self.run_backup_async(job.sandbox_id)
        if result.success:
            logger.info(
                "Backup ok: type=%s image=%s archive=%s size=%d duration=%.2fs",
                result.archive_type,
                job.local_image_id,
                result.archive_path.name,
                result.size_bytes,
                result.duration_seconds,
            )
        else:
            logger.warning(
                "Backup failed: image=%s message=%s",
                job.local_image_id,
                result.message or "unknown error",
            )
        job.next_run = now + interval_seconds

    def shutdown(self) -> None:
        """Clean up internal state on shutdown."""
        self._jobs.clear()
        self._running.clear()
        self._shared_sandboxes.clear()
        self._rejected.clear()
        self._rejected_locks.clear()


def find_tar_binary() -> Optional[str]:
    """Return the path to a verified GNU tar binary or None.

    output: resolved tar path with confirmed `GNU tar` support, or None
    """
    candidate = shutil.which("tar")
    if not candidate:
        return None
    try:
        completed = subprocess.run(
            [candidate, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (completed.stdout or "") + (completed.stderr or "")
    if "GNU tar" not in output:
        return None
    return candidate