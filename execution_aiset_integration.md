# Execution plan: aiset integration

File version 1.1.0

This document tracks the implementation of `proposal_aiset_integration.md`.
The proposal is the design of record; this file records what was actually done,
the commands that were run, test results, deviations and the remaining
operational step.

Status legend: `[ ]` not started, `[~]` in progress, `[x]` done, `[!]` blocked.

Pinned aiset revision: `11d8838efe9e5cc447a9211c655a431fab810e8e` (`0.1.0`).

## 1. Phase status

| Phase | State | Summary |
|---|---|---|
| 0 - pin and guards | [x] | submodule, sidecar ignores, dockerignore, test skeleton |
| 1 - descriptor | [x] | `config.descriptor.json5`, `config.types.json5`, strict gate |
| 2 - overlays and migration | [x] | 8 overlays / 9 ids, template renames, migration tool, docs sweep |
| 3 - runtime | [x] | `src/settings/`, root-wins merge, parity, aiset is the only backend |
| 4 - root trim waves | [x] | live root trimmed, auto-start imported, parity verified |
| 5 - Web UI | [x] | ASGI mount, principal, selector, editor, styles, HTTP tests |
| 6 - cutover and cleanup | [x] | legacy loader removed, docs/tests updated |

## 2. Execution log

### 2026-10-21 - start

- Read `proposal_aiset_integration.md` v1.3.0.
- Confirmed the submodule and pinned revision.
- Created this execution document.

### 2026-10-21 - phase 0: guards

- `.gitignore`:
  - added `config.json5.lock`, `config.json5.back*`,
    `config.json5.conflicted.*.back`;
  - kept `images/*/` ignored for nested image repositories. The proposed
    generic unignore was rejected because the image directories are separate
    Git repositories and the root would expose them as nested repos.
- `.dockerignore`: replaced `config.sndbx.json5` with `sndbx.overlay.json5` in
  `clawvm`, `denis_obsidian_assistant`, `open-webui` and `paperclip`.
- Added aiset sidecar ignores (`sndbx.overlay.json5.lock`, `.back*`,
  `.conflicted.*.back`) to the root `.gitignore` and to each nested image
  repository that owns an overlay.

### 2026-10-21 - phase 1: descriptor

- Created `config.descriptor.json5` and `config.types.json5`.
- Templates: `sandbox`, `shared_dir`, `managed_volume`, `port_binding`,
  `mcp_binding`, `readiness`, `runtime_contract`, `envid`, `user`,
  `webui_user`, `log_level`.
- Verified strict validation with known templates: **valid, 0 issues**.
- Verified `Service.register` + `reload` for:
  - `config.json5`;
  - `config.json5.example`;
  - all current `config.sndbx.json5` templates.
  Result: every document `loaded=file`, `invalid=False`, 0 errors.
- Added `tests/test_config_descriptor.py` (strict descriptor, document load,
  overlay coverage, no global overlay sections).

### 2026-10-21 - phase 2: overlays and migration

- Added `scripts/migrate_sandbox_overlays.py`:
  - parses the root with aiset's source-preserving `Document`;
  - keeps `${VAR}` tokens unresolved;
  - groups sandbox ids by image directory;
  - generates `images/<image-dir>/sndbx.overlay.json5`;
  - supports `--write-overlays`, `--import-startup-overrides`, `--trim-root`,
    `--dry-run`.
- Generated **8 overlay files for 9 sandbox ids**:
  - `default_sandbox_1`: `sandbox-1`, `default-sandbox-1`;
  - `mcp_toolbox_1`: `mcp-toolbox-1`;
  - `langvm`, `aibusiness`, `clawvm`, `denis_obsidian_assistant`,
    `open-webui`, `paperclip`.
- `aibusiness` was generated from the current root, not from the stale
  `images/aibusiness/config.sndbx.json5` (which declared `langvm`).
- Existing templates were moved to `sndbx.overlay.json5`; a
  `config.sndbx.json5.pre-overlay.bak` is kept in each image directory.
- `run_at_startup` was omitted from every overlay; `envids` were omitted
  because the root owns them.
- Updated current image documentation (`README.md`, `commands.md`,
  `GETTING_STARTED.md`, `TEST.md`, `DOCUMENTS.md`, `struct.md`) from
  `config.sndbx.json5` to `sndbx.overlay.json5`.
- Added `tests/test_settings_overlays.py` (discovery, ids, root-wins merge,
  array replacement, overlay-only keys, demo alias).

### 2026-10-21 - phase 3: runtime

- Added `src/settings/`:
  - `aiset_path.py`: import bootstrap and namespace-package trap handling;
  - `overlays.py`: discovery, host overlay rules, root-wins merge;
  - `principals.py`: session -> `Principal`;
  - `runtime.py`: service bootstrap, validation, merge, callbacks;
  - `facade.py` was not needed: `src/config.py` is the compatibility facade.
- `src/config.py` now returns `{"env", "config", "settings"}` from
  `SettingsRuntime`; the legacy JSON5 parser was removed.
- Root-wins merge:
  `aiset.merge(root_sandbox, overlay_sandbox, prefer="base")`; arrays in the
  root win whole; overlay-only keys are added.
- Startup policy: an invalid root/overlay or an LKG fallback refuses to start
  with the issue list.
- Legacy `data/sandbox_startup_overrides.json` is imported into the root at
  startup and renamed to `.migrated`; if the write fails, it is applied in
  memory and kept.
- Added:
  - `tests/test_settings_runtime.py`;
  - `tests/test_settings_parity.py` (independent reference reader);
  - `tests/test_settings_migration.py` (trim parity on a temp copy).
- Parity result on the live config copy: **0 differences**.

### 2026-10-21 - phase 4: trim

- `trim_root_at()` implemented in the migration tool.
- aiset refuses a whole-object `set` that would drop commented members
  (`Replacement drops commented members`). The migration tool therefore edits
  the source span of each sandbox object, validates the result with aiset and
  keeps `config.json5.pre-overlay.bak`.
- The trim was verified on a temporary copy of the live root:
  `tests/test_settings_migration.py` passes; the merged runtime config is
  identical before and after the trim.
- On 2026-10-22 the live migration was executed after the cutover:
  ```sh
  .venv/bin/python scripts/migrate_sandbox_overlays.py \
    --import-startup-overrides --trim-root
  ```
  Result: 4 auto-start overrides imported, 9 root sandbox entries trimmed,
  `data/sandbox_startup_overrides.json` renamed to `.migrated`, and
  `config.json5.pre-overlay.bak` created.
- Live parity against the pre-trim backup: **0 leaf differences**. The runtime
  config has all 9 sandboxes with their overlay values; for example
  `sandbox-1` is `ubuntu:22.04`, `langvm` is `langvm:latest`, `clawvm` is
  `clawvm:latest`, and `paperclip` is `paperclip:latest`.

### 2026-10-21 - phase 5: Web UI

- `SNDBXApp` passes the runtime to `WebUIServer`.
- Mounted `create_asgi_app` at `/api/settings` before the static mount.
- Added `GET /api/settings/documents` (session-protected) for the selector.
- Added async `principal_from_scope` backed by `SessionStore`:
  - `all` -> every settings permission;
  - other authenticated users -> read-only viewer;
  - anonymous -> no permissions.
- Replaced the Settings placeholder with the document selector and
  `#settings-tree`.
- Added `src/webui/frontend/settings.css` (dark sndbx palette, editor control
  padding after the global reset).
- `app.js` mounts/destroys the aiset tree, keeps the selector, and exposes the
  `Restart sndbx` lifecycle action.
- Dashboard Auto-start now writes the root `config.json5` through
  `apply_operations` with revisions; the file-based path remains only for a
  defensive fallback when no settings runtime is present.
- Added `tests/test_settings_http.py` and `tests/test_settings_styles.py`.

### 2026-10-21 - phase 6: cutover

- aiset is the only production configuration backend. `SNDBX_SETTINGS_BACKEND`
  and the legacy parser were removed.
- `README.md` documents the pinned revision, root/overlay model and root-wins
  precedence.
- `TEST.md` gained the settings suites, descriptor check and browser protocol.
- `config.json5.example` documents the overlay model.
- `SPEC_.md` records the required changes to the read-only `SPEC.md`: the
  three configuration locations and the aiset-backed Settings tab.

## 3. Verification

### 3.1 Settings suites

```sh
PYTHONPATH=src venv/bin/python -m unittest \
  tests.test_config_descriptor tests.test_settings_overlays \
  tests.test_settings_runtime tests.test_settings_parity \
  tests.test_settings_migration tests.test_settings_http \
  tests.test_settings_styles -v
```

Result: **29 tests, OK** (1 skip: `test_settings_parity` skips after the live
root was trimmed; trim parity is covered by `test_settings_migration`).

### 3.2 Full suite

```sh
PYTHONPATH=src venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

Result: **108 tests, 3 failures, 1 skip**. The failures are in `tests/test_backup.py`
(`test_run_creates_incremental_archive`,
`test_full_plus_incremental_restores_changes`,
`test_chain_metadata_records_full_and_incremental`). They were reproduced in a
clean `git worktree` at `HEAD` with no aiset changes, so they are pre-existing
and unrelated to this work.

### 3.3 Descriptor

Strict descriptor validation with `config.types.json5` as known templates:
**valid, 0 issues**. Root, example and all 8 overlays load with 0 errors.

### 3.4 Parity

The independent reference reader (regex substitution + `json5.loads` + legacy
auto-start overrides) and the aiset runtime produce **0 leaf differences** on a
copy of the live root.

### 3.5 Migration parity

The temp-copy trim test compares the runtime config before and after reducing
every root sandbox to `{run_at_startup}`: **0 differences**.

## 4. Deviations from the proposal

1. **Mount base.** The installed Starlette 1.0 `Mount` keeps the full path, so
   the aiset app is mounted with `base="/api/settings"`, not `base=""`. The
   HTTP integration test exercises this exact stack.
2. **Overlay tracking.** The generic `.gitignore` unignore was rejected because
   the image directories are separate Git repositories. Only the
   `default_sandbox_1` and `mcp_toolbox_1` overlays are tracked by the root.
   The overlay and documentation changes in the nested image repositories are
   in their working trees; they are not staged by the root repository.
3. **Root trim mechanism.** aiset refuses a whole-object `set` that would drop
   commented members. The one-time migration tool edits the source span and
   validates the result; the runtime never edits configuration sources.
4. **Legacy auto-start import.** It is automatic on the first aiset start
   (with a `.migrated` rename), with an in-memory fallback if the write fails.
5. **Live trim deferred — resolved on 2026-10-22.** The code and tool are
   complete and verified, but the live `config.json5` is unchanged because the
   old service is running. The live trim was executed later the same day; see
   §5.
6. **Facade module.** `src/settings/facade.py` was unnecessary; `src/config.py`
   is the facade.
7. **Pre-existing backup failures — resolved on 2026-10-23.** The 3 failing
   backup tests were unrelated to the settings work and reproduced at `HEAD`.
   Their cause was `_decide_archive_type()` using `datetime.now()` instead of
   the run's reference time; they now pass.

## 5. Live rollout and routine operations

Completed on 2026-10-22:

1. Imported the legacy auto-start file and trimmed the root:
   ```sh
   .venv/bin/python scripts/migrate_sandbox_overlays.py \
     --import-startup-overrides --trim-root
   ```
   Backups: `config.json5.pre-overlay.bak`, `config.json5.back`, aiset `.back`
   chain; the old state file is now `.migrated`.
2. Restarted the service without running the graceful shutdown that stops
   sandboxes:
   ```sh
   systemctl --user kill --kill-whom=main --signal=SIGKILL sndbx
   ```
   systemd restarted it after 5 seconds (`Restart=always`). New main PID
   `1242965`; startup completed with no configuration errors. All running
   sandboxes remained up.
3. Live checks:
   - `/api/settings/health` -> 200;
   - `/api/settings/assets/aiset.js` -> 200 `application/javascript`;
   - `/api/settings/assets/missing.js` -> 404;
   - `/api/settings/documents` anonymous -> 401;
   - authenticated documents -> 200 with `main` and 8 overlay documents;
   - `/api/settings/tree?document=main` and the
     `sandbox-group:default_sandbox_1` tree -> 200.

Routine operations:

- Edit settings in the Web UI. Logging and Auto-start apply live; other changes
  show `restart_required`.
- Restart from the editor's lifecycle button or with the kill command above.
- A future config change that needs root keys moved to an overlay uses the same
  migration tool; do not run `--write-overlays` after the root is trimmed
  (image ids are then unavailable in the root).
- To restore the pre-migration root, copy `config.json5.pre-overlay.bak` back to
  `config.json5` and restart. This also restores root-wins for every field.

## 6. Commands and environment notes

- Use `venv/bin/python` for the full suite; `.venv` lacks `aiohttp` and cannot
  import `webui_server`.
- Use `PYTHONPATH=src` for every host test command.
- `--trim-root` is a one-time migration for the initial full root. Do not run
  `--write-overlays` after the root is trimmed; it needs the image ids that now
  live in the overlays.
- The aiset submodule remains read-only from the host. Library changes belong in
  the aiset repository.
