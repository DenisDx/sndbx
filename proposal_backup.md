# Per-VM Incremental Backup Proposal

## Goal

Add an internal sndbx backup scheduler for local image directories. The scheduler
uses GNU tar and creates backups only for virtual machines that explicitly
enable backup in their sandbox configuration.

The feature does not use cron, a systemd timer, Docker image export, or a
separate daemon. It runs inside the existing `sndbx.service` process.

## Scope

The scheduler backs up a local image directory:

```text
images/<local_image_id>/
```

This includes the image source and its persistent `data/` directory. It does
not archive Docker storage under `/var/lib/docker`, Docker containers, or
external bind mounts outside the image directory.

For example, the Obsidian vault and `Agents_shared` are external mounts and
are intentionally not included in an Assistant image backup.

## Configuration

Backup settings belong to an individual sandbox. Backups are disabled when the
`backup` object is absent or `enabled` is false.

```json5
backup: {
  enabled: true,
  interval_hours: 24,
  full_every: 7,
}
```

Parameter semantics:

| Parameter | Type | Meaning |
|---|---:|---|
| `enabled` | boolean | Enables the scheduler for this sandbox. Default: `false`. |
| `interval_hours` | positive integer | Time between backup runs. |
| `full_every` | positive integer | Create a new full backup after this many backup intervals. |

The initial run always creates a full backup. Invalid backup settings are
reported in the sndbx log and skipped; they must not stop the rest of sndbx.

## Archive Layout

Each local image gets an isolated backup directory:

```text
backup/
  denis_obsidian_assistant/
    denis_obsidian_assistant-2026-10-10T110000Z-full.tar.gz
    denis_obsidian_assistant-2026-10-11T110000Z-incremental.tar.gz
    denis_obsidian_assistant.snar
```

The directory name is `local_image_id`, not a sandbox ID or Docker image tag.
This makes the archive location stable when a sandbox ID or tag changes.

Archive names use UTC timestamps and contain either `full` or `incremental`.
The snapshot file (`.snar`) is private scheduler state and is stored alongside
the archive chain.

## Backup Algorithm

For a configured sandbox, sndbx resolves its `local_image_id` and source
directory under `images/`.

1. Create `backup/<local_image_id>/` if it does not exist.
2. Determine whether a full archive is due:
   - no `.snar` file or no previous full archive: full backup;
   - otherwise, use the number of completed intervals since the latest full
     archive and compare it with `full_every`.
3. Run GNU tar with `--listed-incremental=<snapshot path>`.
4. Write the archive to a temporary file in the target directory.
5. Atomically rename the temporary archive after tar exits successfully.
6. Log the archive type, path, size, and duration.

Only one backup may run for one `local_image_id` at a time. A slow backup must
not overlap a later scheduled run.

The scheduler does not stop a sandbox before creating an archive. Therefore an
archive is a file-level backup, not an application-consistent database
snapshot. This is the least disruptive behavior and matches the requested
backup scope.

## Restore Procedure

Restore must occur into an empty target directory, not over a running image
directory.

1. Extract the most recent required full archive with GNU tar and its `.snar`
   semantics.
2. Apply every incremental archive after that full archive in chronological
   order.
3. Validate ownership and permissions before using the restored image.
4. Build the image and create a new sandbox from the restored directory.

The full archive and every subsequent incremental archive are required. Losing
an archive breaks restoration of later changes in that chain.

## Safety Rules

- The scheduler runs no shell through `shell=True`.
- Tar receives explicit argument lists and a resolved source directory.
- Archive and snapshot paths must remain below `backup/<local_image_id>/`.
- Local image IDs are validated as directory names before constructing paths.
- Failed tar commands leave no success-shaped archive.
- The `.snar` file is updated only by a successful tar invocation.
- Backup failures are logged with command output and do not terminate sndbx.
- `backup/` is ignored by Git.

## Implementation Plan

1. Create a `BackupManager` module.
   - Parse and validate per-sandbox backup settings.
   - Resolve local image source and backup directories.
   - Generate safe archive and snapshot paths.
   - Execute tar and report structured results.

2. Add scheduler lifecycle integration to `SNDBXApp`.
   - Start one asyncio background task after configuration is loaded.
   - Run enabled backup jobs on their configured intervals.
   - Ensure cancellation on sndbx shutdown.
   - Prevent concurrent jobs for the same image directory.

3. Add configuration documentation.
   - Keep backup absent/disabled by default in generic sandbox examples.
   - Enable the agreed daily incremental / weekly full configuration for
     `denis-obsidian-assistant`.
   - Document the archive layout and restoration chain in the main README.

4. Add automated tests.
   - Configuration validation and disabled-by-default behavior.
   - Full backup creation.
   - Incremental backup creation using a temporary image directory.
   - Full backup rotation after `full_every`.
   - Rejection of unsafe image IDs and paths.
   - Failure handling for tar errors and concurrent-job suppression.

5. Validate the feature.
   - Run focused backup tests.
   - Run Python compilation and existing sandbox tests.
   - Perform a manual full-plus-incremental restore into a temporary directory
     and verify its contents.

## Required Follow-up Work After Implementation Review

> **Status: implemented.** All nine items below were delivered and are covered
> by the focused backup suite (44 tests). The text is kept as the review
> record. See also §"Additional Follow-up Work After Re-Review".

The backup feature is implemented: `BackupManager`, the sndbx background task,
per-VM configuration, documentation, and 30 focused unit tests are present.
The review found the following required corrections before relying on backup
chains for recovery.

### Critical

1. Fix the scheduler's undefined `loop` reference.

   After the first due job completes, `BackupManager.run_loop()` calls
   `loop.time()` although no `loop` variable exists. This raises
   `NameError` and ends the scheduler task. Replace it with
   `asyncio.get_running_loop().time()` or the existing `time.monotonic()`,
   then add a scheduler integration test that runs an immediately due job and
   verifies a second scheduling cycle remains active.

2. Reset the tar snapshot before every scheduled full backup.

   The current full-backup decision only changes the archive filename to
   `-full.tar.gz`; it still passes the existing `.snar` file to GNU tar.
   GNU tar therefore creates an incremental archive content stream despite the
   `full` name. Create a fresh temporary snapshot for a full backup and replace
   the live `.snar` only after tar and archive finalization both succeed. Add a
   restore test that verifies a full archive can be extracted independently.

3. Preserve the last valid `.snar` when tar fails.

   GNU tar can update a `--listed-incremental` snapshot before exiting with an
   error. The current implementation removes the temporary archive but passes
   the live `.snar` directly to tar, which can corrupt the next incremental
   chain. Always pass tar a temporary copy of the snapshot and atomically
   promote it only after a successful archive rename. Add a regression test
   that induces a tar failure and compares the live snapshot before and after.

### High

4. Synchronise by `local_image_id`, not by sandbox ID.

   Archive directories and `.snar` files are shared by `local_image_id`, but
   the current asyncio locks are keyed by `sandbox_id`. Two enabled sandbox
   configurations referencing the same local image can run tar concurrently
   against one snapshot file. Deduplicate such jobs during configuration or
   use locks keyed by `local_image_id`; reject conflicting schedules with
   different intervals/settings.

5. Use and verify the resolved GNU tar binary.

   `find_tar_binary()` only checks that a command named `tar` exists, while
   backup execution invokes the literal `tar`. Store the resolved path and
   verify GNU tar support (for example `tar --version` containing `GNU tar`)
   before enabling the scheduler. This prevents unsupported implementations
   from silently accepting an incompatible `--listed-incremental` behavior.

6. Restore the complete archive chain in an automated test.

   Current tests create full and incremental archives, but do not restore a
   full archive followed by incrementals into an empty directory. Add a test
   that confirms added, modified, and deleted files have the expected final
   state after restoration.

### Medium

7. Handle archive name collisions without losing a scheduled run.

   Archive names have second precision. A collision causes the job to fail and
   defers it until the next configured interval. Add a collision-free suffix
   (for example milliseconds or a bounded numeric suffix), or retry a new name
   within the same run.

8. Track backup chain metadata explicitly.

   Full-backup rotation currently derives age only from archive filenames.
   Add a small atomic metadata file per `local_image_id` recording the last
   successful full and incremental archive. This supports reliable rotation,
   clearer diagnostics, and future retention without inferring state from
   filenames alone.

9. Clarify duplicate backup configuration.

   The active configuration currently contains more than one enabled `backup`
   block. Keep `backup.enabled: true` only for the intended
   `denis-obsidian-assistant` sandbox, as originally requested, unless each
   additional enabled VM has an explicit backup policy and independent local
   image directory.

## Additional Follow-up Work After Re-Review

> **Status: implemented.** The scheduler now keeps one canonical job per
> `local_image_id` and records the duplicate sandbox as sharing the schedule;
> the async lock is keyed by `local_image_id`. The text is kept as the review
> record.

The original nine follow-up items have been implemented and covered by the
current focused backup suite: scheduler continuity, fresh and protected
snapshots, full-plus-incremental restore, GNU tar verification, collision-free
names, chain metadata, and the active configuration containing only the
intended enabled backup job.

One additional scheduler behavior still requires correction.

### High

1. Coalesce identical schedules that target the same `local_image_id`.

   `BackupManager.reconfigure()` correctly rejects conflicting settings for
   the same image directory, but it retains two separate jobs when their
   settings are identical. `run_loop()` will then execute both due jobs in
   sequence: the first creates the intended backup and the second immediately
   creates an unnecessary incremental archive against the same `.snar` chain.

   Keep a single canonical job per `local_image_id` and record the duplicate
   sandbox as sharing that schedule, or exclude it from `_jobs` with an
   informational log. Key the async lock by `local_image_id` as a defensive
   second layer. Replace the current test, which manually invokes only one
   selected job, with a scheduler-level test proving that two identical
   sandbox configurations produce exactly one archive per interval.

## Root-Owned Backup Helper for Guest-Owned Files

Virtual machines can create files under host UIDs or GIDs that differ from the
user running `sndbx.service`. ACL repair is unsafe: guest processes can change
an ACL mask or recreate a file after repair but while tar is reading it. It can
also alter permissions that a virtual machine expects.

### Implemented Approach

1. `BackupManager` delegates archive creation for configured backups to a
   pre-authorized root helper with `sudo -n`; no interactive prompt is
   possible.
2. The helper is provisioned once by an administrator:

   ```bash
   sudo ./install_backup_acl_helper.sh <sndbx-service-user>
   ```

   Provisioning installs a root-owned helper, root-owned configuration for the
   canonical `images/`, `backup/`, and state roots, and a sudoers rule that
   permits only that helper for the service user.
   The standard installer offers this as an explicit opt-in step. Unattended
   installations can enable it with `INSTALL_BACKUP_ACL_HELPER=1 ./install.sh`.
3. The helper accepts only a validated local image ID, archive type, and
   timestamped archive filename. It validates every resolved path, reads only
   the configured image tree, writes only the corresponding backup directory,
   and keeps `.snar` state plus a per-image lock under root-owned
   `/var/lib/sndbx-backup/`. Under that lock it also removes stale temporary
   archive files left by a prior interrupted helper run.
4. The helper never changes ACLs, modes, owners, or any other metadata in the
   VM image. It runs GNU tar as root only to read files that the VM created
   with foreign ownership or restrictive permissions.

This eliminates ACL races and avoids touching VM permissions while allowing
tar to archive current and future guest-owned files without an interactive
runtime action.

The helper invokes GNU tar with `--ignore-failed-read`. A live backup remains
file-level rather than application-consistent: a database file that changes
while tar reads it can be omitted or captured at an arbitrary point in time,
but that expected live-write race must not discard the rest of a successful
archive or corrupt incremental state.
