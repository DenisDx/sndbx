# Test Protocol

This document defines the repeatable test procedure for sndbx. Run commands
from the repository root unless a command explicitly says otherwise.

## 1. Scope and Safety

The test suite has three levels:

| Level | Purpose | Changes to host or Docker |
|---|---|---|
| Static checks | Verify Python syntax and imports. | None |
| Unit and regression tests | Validate configuration, sandbox command construction, and backup behavior in temporary directories. | None |
| Smoke and integration checks | Verify the installed sndbx service, Docker, Kata, Web UI, MCP, and a selected sandbox. | May read or start configured services; do not run on production without review. |

The automated tests use temporary directories. Do not use `Recreate`, `Build`,
`Update & rebuild`, `Full update & rebuild`, or a backup restore procedure
against a production sandbox solely for test purposes.

## 2. Prerequisites

Install the project dependencies and activate the project virtual environment:

```bash
python3 -m venv venv
venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install -r requirements.txt
```

For backup tests, GNU tar must be installed:

```bash
tar --version
```

The output must identify GNU tar. Backup scheduling is intentionally disabled
when GNU tar is missing or unsupported.

For Docker/Kata smoke tests, the current user must be allowed to access Docker:

```bash
docker version
docker info --format '{{json .Runtimes}}'
```

## 3. Static Checks

Run this before every code change that affects Python:

```bash
venv/bin/python -m py_compile src/*.py
```

Expected result: exit code `0`. A `SyntaxWarning` is not a successful
correction; record it as a follow-up issue and fix it in the changed code when
applicable.

Check that the main configuration parses without starting services:

```bash
PYTHONPATH=src venv/bin/python - <<'PY'
from config import load_config

config = load_config(".")["config"]
print(f"config title: {config.get('title')}")
print(f"sandboxes: {len(config.get('sandboxes', {}).get('items', {}))}")
PY
```

Expected result: configuration title and sandbox count are printed without an
exception.

## 4. Unit and Regression Tests

Run the complete project regression suite:

```bash
PYTHONPATH=src venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

Expected result: all tests pass. Known pre-existing failures in this checkout:
`test_run_creates_incremental_archive`,
`test_full_plus_incremental_restores_changes` and
`test_chain_metadata_records_full_and_incremental` in `tests/test_backup.py`
reproduce at `HEAD` without the aiset changes and are unrelated to settings.
After the live root trim, `test_settings_parity` skips (trim parity is covered by
`test_settings_migration`).

The current suite covers:

- sandbox lifecycle and Docker command construction;
- Kata runtime and virtiofs profile options;
- read-only root filesystem and tmpfs configuration;
- mount validation and safe recreate behavior;
- image update safety, including protection of `data/`;
- backup configuration validation and disabled-by-default behavior;
- GNU tar verification;
- full and incremental archive generation;
- full backup rotation, snapshot protection, restore chains, archive naming,
  same-image scheduling, and scheduler continuity;
- runtime monitoring parsers, thresholds, debounce, history, caching, and
  authenticated HTTP endpoint behavior.

Run focused suites when changing a specific subsystem:

```bash
# Backup manager and scheduler
PYTHONPATH=src venv/bin/python -m unittest tests/test_backup.py -v

# Sandbox lifecycle, Kata, mounts, images, and runtime contracts
PYTHONPATH=src venv/bin/python -m unittest tests/test_sandbox_apt_mirror.py -v
```

When a test fails, preserve the complete output. Do not rerun a destructive
operation against a real sandbox as a substitute for diagnosing the failure.

Focused settings suites:

```bash
# Descriptor, overlay model, runtime, HTTP mount and styles
PYTHONPATH=src venv/bin/python -m unittest \
  tests.test_config_descriptor tests.test_settings_overlays \
  tests.test_settings_runtime tests.test_settings_parity \
  tests.test_settings_migration tests.test_settings_http \
  tests.test_settings_styles -v
```

Focused runtime monitoring suite:

```bash
PYTHONPATH=src venv/bin/python -m unittest tests/test_monitoring.py -v
```

The monitor tests use captured Docker/guest output and a temporary root. They
verify byte/percentage parsing, meminfo/OOM parsing, process ranking, host
thresholds, warning debounce, CPU streaks, history, response caching, stopped
containers, unknown IDs, authentication, and HTTP 400 responses.

### Runtime monitoring manual protocol

Use a disposable sandbox for these checks. Do not create memory or CPU pressure
on a production VM.

1. Open Dashboard -> Current containers -> Advance.
2. Enable **Monitor** for one running VM. Confirm that a detail row appears and
   the card grows without breaking the table.
3. Compare the memory and CPU values with `docker stats` and with `free -m`
   inside the guest.
4. Run a bounded memory workload inside the disposable VM and verify that the
   memory graph rises, then shows a warning after two low-availability samples.
   A critical condition appears immediately below 10 % `MemAvailable`.
5. If the disposable VM is safe to lose, trigger a small OOM and confirm the
   `OOM` badge and the guest OOM counter. Never do this on a production VM.
6. Run a bounded CPU workload and verify the CPU graph and the `CPU` badge.
7. Turn the switch off and confirm that `/api/monitor` requests stop in the
   browser network panel.
8. Hide the browser tab and confirm that polling pauses; show it again and
   confirm that it resumes.
9. Stop the VM. The detail row must show `not running`; start it and confirm
   that collection resumes.
10. Open two authenticated dashboards and confirm that both use the same
    backend response cache rather than multiplying Docker calls.

Descriptor validation (strict; typedefs are supplied through the Python API):

```bash
PYTHONPATH=src venv/bin/python - <<'PY'
from pathlib import Path
from settings.aiset_path import ensure_aiset
ensure_aiset(Path("."))
from aiset.descriptor import _collect_typedefs, parse_descriptor, validate_descriptor
types = Path("config.types.json5").read_text()
templates = _collect_typedefs(parse_descriptor(types))
result = validate_descriptor(
    Path("config.descriptor.json5").read_text(),
    mode="strict",
    known_templates=templates,
)
print("valid:", result.valid)
for issue in result.issues:
    print(issue.to_dict())
PY
```

Settings acceptance in a browser:

1. Open **Settings**; the document selector lists the root configuration and
   every `images/<dir>/sndbx.overlay.json5`.
2. Change `logging.level` in the root; Apply; confirm the new level without a
   restart.
3. Change a sandbox field in an overlay; Apply; expect `restart_required`; press
   Restart.
4. Add the same key to the root and confirm the root value wins after restart.
5. Change Auto-start on the dashboard; confirm it is written to the root and
   survives a restart.
6. Request `/api/settings/assets/aiset.js` (JavaScript, 200) and a missing asset
   (404, not the SPA).
7. Log in without `permissions: ["all"]`; the editor is read-only and secret
   subtrees are denied.


## 5. Backup Smoke Test

Run this in a temporary directory. It creates no archive in `./backup` and
does not use a configured VM:

```bash
tmp_root="$(mktemp -d)"
mkdir -p "$tmp_root/images/demo/data"
printf 'first\n' > "$tmp_root/images/demo/data/example.txt"

PYTHONPATH=src venv/bin/python - "$tmp_root" <<'PY'
import sys
from pathlib import Path
from unittest.mock import Mock

from backup import BackupManager

root = Path(sys.argv[1])
config = {
    "root": str(root),
    "sandboxes": {
        "items": {
            "demo": {
                "image": "demo:latest",
                "local_image_id": "demo",
                "backup": {"enabled": True, "interval_hours": 24, "full_every": 7},
            },
        },
    },
}
manager = BackupManager(config, str(root), Mock())
manager.reconfigure()
result = manager.run_backup("demo")
assert result.success, result.message
assert result.archive_type == "full", result
print(result.archive_path)
PY

find "$tmp_root/backup/demo" -maxdepth 1 -type f -printf '%f\n'
rm -rf "$tmp_root"
```

Expected result:

- one `demo-<UTC>-full.tar.gz` archive;
- one `demo.snar` snapshot;
- the Python command exits successfully.

For a configured backup job, inspect scheduler status after restarting sndbx:

```bash
systemctl --user restart sndbx.service
journalctl --user-unit=sndbx.service --since '5 minutes ago' --no-pager \
  | grep -E 'Backup scheduler|Backup ok|Backup failed'
```

Expected result: an enabled job is logged with its local image ID, interval,
and full-backup interval. A first backup is due immediately after scheduler
startup; later backups run according to `interval_hours`.

Before testing backup of VM-created files, install the non-interactive root
backup helper once as an administrator:

```bash
sudo ./install_backup_acl_helper.sh <sndbx-service-user>
```

Alternatively, select the optional helper step during `./install.sh`, or run
`INSTALL_BACKUP_ACL_HELPER=1 ./install.sh` for an unattended installation.

There must be no password prompt during the later backup run. The scheduler
invokes the installed root-owned helper with `sudo -n`; the helper reads the
configured image and creates its archive without changing VM ACLs.

## 6. Manual Backup Restore Test

Perform this only with a backup set created for a non-production test image or
with a copied backup directory. Never extract into a running image directory.

```bash
restore="$(mktemp -d)"
backup_dir="$SNDBX_ROOT/backup/<local_image_id>"
snapshot="$restore/<local_image_id>.snar"

# Extract the selected full archive first.
tar --extract --gzip \
  --listed-incremental="$snapshot" \
  --file="$backup_dir/<local_image_id>-<UTC>-full.tar.gz" \
  --directory="$restore"

# Apply every later incremental archive in chronological order.
for archive in "$backup_dir"/<local_image_id>-<UTC>-incremental.tar.gz; do
  tar --extract --gzip \
    --listed-incremental="$snapshot" \
    --file="$archive" \
    --directory="$restore"
done

find "$restore/<local_image_id>" -maxdepth 2 -print
```

Verify the final tree contains expected additions and modifications and omits
files deleted after the full archive. Verify ownership and permissions before
building or deploying from the restored directory. Remove the temporary
restore directory only after recording the result.

## 7. Docker and Kata Smoke Tests

These checks do not create a configured sandbox:

```bash
docker version
docker info --format '{{json .Runtimes}}' | grep -q '"kata"'
docker run --rm --runtime kata alpine:latest true
```

Expected result: all commands exit with code `0`. If the final command fails,
repair the Kata runtime before testing sndbx sandbox creation.

## 8. sndbx Service Smoke Test

After installing or changing `src/`, restart and inspect the user service:

```bash
systemctl --user restart sndbx.service
systemctl --user is-active sndbx.service
journalctl --user-unit=sndbx.service --since '2 minutes ago' --no-pager
```

Expected result:

- service state is `active`;
- no Python traceback appears in the new log entries;
- MCP and Web UI report successful startup.

## 9. Web UI Smoke Test

Open the configured Web UI URL, normally:

```text
http://127.0.0.1:30080/
```

For a LAN binding, use the configured host address instead. Verify:

1. Login succeeds.
2. Dashboard lists configured sandboxes.
3. Status refresh completes.
4. Console opens for a running sandbox and displays an interactive shell.
5. Disconnecting the Console closes the WebSocket cleanly.

If Console fails, capture its corresponding server-side log entries:

```bash
journalctl --user-unit=sndbx.service --since '5 minutes ago' --no-pager \
  | grep -E -C 5 'ws_console|Traceback|WebSocket'
```

## 10. MCP Smoke Test

Use the configured MCP host, port, and token. The standard endpoint is:

```text
http://127.0.0.1:30081/mcp
```

Send an authenticated `tools/list` request using the configured token and
confirm an HTTP `200` response with the expected tool list. Do not execute
state-changing MCP tools as part of a smoke test.

## 11. Selected Sandbox Smoke Test

Only after Docker/Kata and service smoke tests pass, test one non-critical
sandbox:

1. Start it from the Web UI or the supported sndbx action.
2. Confirm its status is running.
3. Open Console and run:

   ```bash
   id
   pwd
   uname -a
   ```

4. Stop it through sndbx and confirm the status changes to stopped.
5. Start it again and verify Console remains usable.

For the Denis Obsidian Assistant, additionally verify:

```bash
docker exec sndbx-denis-obsidian-assistant \
  sh -lc 'ss -ltn "sport = :18789"; openclaw gateway status --deep'

curl --silent --show-error --insecure --max-time 15 \
  --output /dev/null --write-out 'https_status=%{http_code}\n' \
  https://192.168.1.111:11443/
```

Expected result: Gateway listens on `127.0.0.1:18789`, its connectivity probe
is successful, and HTTPS returns `200`.

## 12. Test Result Record

For each meaningful test run, record:

```text
Date and time (UTC):
Git revision:
Environment (host, Python, Docker, Kata versions):
Commands executed:
Result (pass/fail):
Relevant output or log location:
Follow-up issue, if any:
```

Do not mark a change complete when a required check is skipped, fails, or
cannot be interpreted. Record the reason and the mitigation instead.
