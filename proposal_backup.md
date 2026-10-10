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
