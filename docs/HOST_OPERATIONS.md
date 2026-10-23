# sndbx — Host Operations

> Part of the sndbx specification. See [`../SPEC_NEW.md`](../SPEC_NEW.md) for
> the architecture and [`../README.md`](../README.md) for the end-user
> installation walkthrough. This document describes what the host-side tooling
> does, what it changes, and how it fails.

## 1. Scope

sndbx touches the host in three ways:

1. **It consumes host services** — Docker, the Kata runtime, KVM, `socat`,
   GNU tar, `jq`, `curl`.
2. **It is installed as a service** — a systemd unit plus a Python virtualenv
   inside the checkout.
3. **It modifies host configuration** — Docker runtime registration, Kata
   configuration, and (optionally) a privileged backup helper.

Everything in this document is **host-side**. Everything inside a guest is
described in [`IMAGES.md`](IMAGES.md).

## 2. The installation chain

```text
install_prerequisites.sh          ← host bootstrap (root)
        │
        ▼
install.sh                        ← application install (root or user)
        │
        ▼
systemctl start sndbx             ← the manager
```

### 2.1 `install_prerequisites.sh`

Prepares the host so that sndbx can run. Declared `set -euo pipefail`.

**Options.**

| Option | Default | Meaning |
|---|---|---|
| `--kata_path <abs>` | `/opt/kata` | Where the Kata bundle is installed. |
| `--docker_path <abs>` | — | Docker `data-root`. |
| `--tmp_path <abs>` | `/tmp` | Download and extraction scratch space. |
| `--kata_archive <abs>` | — | Install from a pre-downloaded archive (offline). |
| `--apt_mirror <url>` | — | Use a mirror for host package installation. |
| `--help`, `-h` | — | Usage. |

All path options must be absolute.

**Pinned versions.** Kata `4.1.0`. The release archive's SHA-256 is pinned per
architecture and always verified before extraction.

**Steps.**

1. **Summary and confirmation.** The plan is printed and a `Y` confirmation is
   required. This makes unattended runs impossible without a TTY; use
   `yes | ./install_prerequisites.sh` only if that is acceptable.
2. **Preflight.** `sudo` access; Ubuntu ≥ 22.04 (read from `/etc/os-release`);
   version conflicts (Python ≥ 3.10, Docker ≥ 20.10) — a conflict is a hard
   error listing all conflicts; path conflicts (`--kata_path` when `/opt/kata`
   already exists is refused); a warning if Docker came from snap.
3. **KVM verification.** `modprobe kvm`, `kvm_intel`/`kvm_amd`, then `kvm-ok`.
   Failure prints platform-specific hints (VMware, Hyper-V, VirtualBox,
   nested KVM, LXC, Xen, BIOS).
4. **System packages.** `python3 python3-venv python3-pip curl jq tar zstd
   socat qemu-system-x86 qemu-utils cpu-checker`, plus `docker.io` only when no
   Docker stack is present. Only missing packages are installed.
   With `--apt_mirror`, `/etc/apt/sources.list` is backed up, replaced,
   used, and **restored**.
5. **Docker service.** `systemctl enable --now docker` only when needed. With
   `--docker_path`, `data-root` is merged into `/etc/docker/daemon.json` via
   `jq`; a *different* existing `data-root` is a hard error. The current user
   is added to the `docker` group only if missing.
6. **Kata.** If the shim already reports exactly `4.1.0`, the step is skipped.
   If `/opt/kata` exists but is non-empty and is not the pinned install, the
   installer **refuses** — it never replaces an existing runtime. Otherwise the
   archive is downloaded (resumable, `--http1.1`), checksum-verified,
   extracted with `tar --zstd`, layout-asserted, and copied into place. A
   symlink `/usr/local/bin/containerd-shim-kata-v2` is created.
7. **Docker runtime registration.** `/etc/docker/daemon.json` gains:

   ```json
   "runtimes": {
     "kata": {
       "runtimeType": "/opt/kata/runtime-rs/bin/containerd-shim-kata-v2",
       "options": { "ConfigPath": "/etc/kata-containers/runtime-rs/configuration.toml" }
     }
   }
   ```

   The default Kata configuration is installed to
   `/etc/kata-containers/runtime-rs/configuration.toml` if missing. For a
   custom `--kata_path`, the `/opt/kata/` prefix inside the configuration is
   rewritten. `enable_annotations` is ensured to contain
   `virtio_fs_extra_args`. `disable_image_nvdimm = true` is always set under
   `[hypervisor.qemu]`. On hosts reporting ≤ 36 physical address bits, a
   `kata-qemu-wrapper` shim is created (`-global
   q35-pcihost.pci-hole64-size=1073741824`) and pointed at by
   `machine_type = "q35"`, `path` and `valid_hypervisor_paths`. Docker is
   reloaded (not restarted) when something changed.
8. **Verification.** The Kata configuration must exist and
   `docker info --format '{{json .Runtimes}}'` must contain `"kata"`.

**Idempotency.** Yes, by design: every step is guarded by an existence or
state check, and Docker is reloaded only when the configuration actually
changed.

### 2.2 `install.sh`

Brings the checkout to a running service.

**Steps.**

1. **Config template.** An optional first argument selects
   `<name>.json5.example`. The template is applied when the argument is given
   **or** when `config.json5` does not exist. A backup chain is rotated:
   `config.json5` → `.bak` → `.bak.bak` → `.bak.bak.bak`.
   **Warning:** passing an explicit template argument *overwrites* the live
   `config.json5` without merging.
2. **Prerequisite checks.** `python3`, `docker`, the Kata runtime-rs shim as
   registered with Docker, a `docker run --runtime kata alpine echo OK` smoke
   test (warn-only), and a `vmx`/`svm` check in `/proc/cpuinfo` (warn-only).
   The shim path is read from `runtimes.kata.runtimeType` in
   `/etc/docker/daemon.json`, so a non-default `--kata_path` from
   `install_prerequisites.sh` is honoured.
3. **Docker access helper.** `docker_cmd()` retries through `sudo docker` when
   the error matches `permission denied` / `cannot connect to the docker
   daemon`. A failing Docker command returns its real exit status.
4. **Directories.** `shared/`, `data/`, `logs/`, `sessions/` and `backup/` are
   created and set to mode 755.
5. **`.env`.** Created from `.env.example` **only if absent**, with the
   `SNDBX_ROOT=` line rewritten to the absolute checkout path. An existing
   `.env` is never modified.
6. **Environment.** The `.env` file is **sourced** by the shell
   (`set -a; source .env; set +a`), so it must remain shell-safe.
7. **Git submodules.** `git submodule update --init --recursive` is run when a
   `.gitmodules` file is present. The `aiset` submodule is **required**: the
   configuration subsystem imports it from `<root>/aiset/src`, and
   `requirements.txt` does not list it. The installer fails with a clear
   message when `aiset/src/aiset` is missing.
8. **Virtualenv.** `python3 -m venv <root>/venv` (skipped if it exists), then
   `pip install --upgrade pip` and `pip install -r requirements.txt`.
9. **Port check.** `lsof` is used to check `MCP_PORT` and `WEBUI_PORT`
   (warn-only; silently "available" when `lsof` is missing).
10. **UFW.** When `WEBUI_HOST` is `0.0.0.0`/`::`, UFW is active and the port
    is not already allowed, the operator is prompted to allow it.
11. **systemd unit.** Written to `/etc/systemd/system/sndbx.service` when that
    directory is writable, otherwise to
    `~/.config/systemd/user/sndbx.service`. The system variant gets a `User=`
    line; the user variant does not.
12. **Backup ACL helper (optional).** Enabled by `INSTALL_BACKUP_ACL_HELPER=1`
    or an interactive `y/N` prompt. Any other non-empty value is a hard error.
13. **Enable and start.** `systemctl enable --now sndbx` (system or user
    scope). A failed start is a **warning**, not an error.

**Idempotency.** Mostly. The config-template overwrite and the UFW prompt are
the exceptions.

### 2.3 The service unit

```ini
[Unit]
Description=sndbx Sandbox Management Service
After=docker.service
Wants=docker.service

[Service]
Type=simple
Restart=always
RestartSec=5s
StandardOutput=journal
StandardError=journal
User=<user>                 # system-wide variant only
WorkingDirectory=<SNDBX_ROOT>
Environment="PATH=<SNDBX_ROOT>/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=<SNDBX_ROOT>/venv/bin/python3 <SNDBX_ROOT>/src/app.py

[Install]
WantedBy=multi-user.target   # default.target in the user variant
```

Notes:

- The unit has **no `EnvironmentFile=`** — the manager parses `.env` itself.
- `Restart=always` with `Type=simple` means even a clean exit is restarted.
- The `Documentation=` URL in the template is a placeholder.

### 2.4 `uninstall.sh`

1. Confirmation — the literal word `yes` (case-insensitive) proceeds;
   anything else cancels with exit code 0.
2. Stops and disables the system unit and the user unit, then removes both
   unit files.
3. Interactive data removal menu:

   | Choice | Removes |
   |---|---|
   | 1 | `.env`, `config.json5`, `config.json5.bak*` |
   | 2 | `venv/` |
   | 3 | `logs/`, `data/`, `shared/`, `sessions/`, `backup/` |
   | 4 | Everything above plus `__pycache__` |
   | 5 | Nothing |

4. A separate opt-in prompt removes the **privileged backup helper** and its
   sudoers rule (needs `sudo`): `/usr/local/libexec/sndbx-backup-helper`,
   `/etc/sndbx-backup-helper/`, `/etc/sudoers.d/sndbx-backup-helper` and
   `/var/lib/sndbx-backup/`.
5. Optionally removes the Docker network `sndbx-net`.

**Not removed** (known gap): `images/`, the `aiset` submodule, the
descriptor and types files, and the `kata-acl` runtime.

## 3. The `aiset` dependency

sndbx depends on the `aiset` settings library, consumed as a **git submodule**
pinned at `11d8838efe9e5cc447a9211c655a431fab810e8e` (`0.1.0`).

**How it is imported.** `src/settings/aiset_path.py`:

1. Try a normal `importlib.import_module("aiset.resolver")` and friends — this
   succeeds if `aiset` was pip-installed into the venv.
2. Otherwise purge any partial `aiset*` modules from `sys.modules`, insert
   `<root>/aiset/src` at the front of `sys.path`, and retry.
3. If the directory is missing, raise `ImportError` with the instruction
   `Run 'git submodule update --init --recursive' or install aiset with pip.`

**Operational consequences.**

- `requirements.txt` deliberately does **not** list `aiset` — it is a path
  dependency, not a wheel dependency.
- `install.sh` does **not** run `git submodule update --init --recursive`.
  A deployment that copies only `src/` and `requirements.txt` fails at import
  time.
- The submodule must not be edited from this repository; advance it instead.

**What `aiset` provides** (module map):

| Module | Role |
|---|---|
| `jsonsrc.py` | Source-preserving JSON5 parser and editor (comments survive). |
| `descriptor.py` | Descriptor load, validate, compose, compile. |
| `validator.py` | Value validation and operational limits. |
| `resolver.py` | `${VAR}` resolution with a single global precedence policy. |
| `store.py` | Revisions, locking, atomic writes, backups, last-known-good fallback. |
| `service.py` | The shared read/validate/set pipeline (`Service`, `Principal`). |
| `merge.py` | Pure `merge(base, update)`. |
| `pointer.py` | JSON Pointer parsing and traversal. |
| `tree.py` | Tree snapshots for the editor. |
| `errors.py` | Stable error codes mapped to HTTP statuses. |
| `httpd.py` / `asgi.py` | Dependency-free WSGI and ASGI adapters over one dispatcher. |
| `doctor.py` | Drift reporting and repair classes (`clamp`, `coerce`, `drop_unknown`). |
| `cli.py` | `aiset validate-descriptor`, `aiset validate-document`, `aiset doctor`. |
| `assets/` | `aiset.js`, `aiset-tree.js`, `aiset.css`, `aiset-host-theme.css`. |

## 4. The Kata runtimes

### 4.1 The primary `kata` runtime

Installed by `install_prerequisites.sh` at `/opt/kata` and registered as the
Docker runtime `kata`. It uses the **runtime-rs** shim
(`containerd-shim-kata-v2`) and the configuration at
`/etc/kata-containers/runtime-rs/configuration.toml`.

### 4.2 The parallel `kata-acl` runtime

`install_kata_acl.sh` installs a **second, isolated** Kata 4.1.0 bundle at
`/opt/kata-acl` and registers it as the Docker runtime `kata-acl`, without
touching the primary runtime, Docker's default runtime, or the primary Kata
configuration.

**Pre-flight guards** (all checked before any mutation): root required;
`x86_64`; the commands `curl jq sha256sum tar python3 systemctl docker` must
be present; `/etc/docker/daemon.json` must exist and be valid JSON;
`/opt/kata-acl` must not exist; `configuration-acl.toml` must not exist; the
`kata-acl` runtime must not already be registered.

**Steps.**

1. Stage a temporary directory under `/var/tmp`.
2. Obtain the archive — from `KATA_ACL_ARCHIVE`, or download it into the cache
   `/var/cache/sndbx-kata-acl/` (resumable). The pinned SHA-256 is always
   verified.
3. Assert the bundle layout: `runtime-rs/bin/containerd-shim-kata-v2`,
   `libexec/virtiofsd`, `bin/qemu-system-x86_64`,
   `share/kata-containers/vmlinux.container`,
   `share/kata-containers/kata-containers.img`.
4. Install by atomic rename into `/opt/kata-acl`.
5. Generate `/etc/kata-containers/runtime-rs/configuration-acl.toml` from the
   primary configuration: rewrite `/opt/kata/` → `/opt/kata-acl/`, require
   exactly one `virtio_fs_extra_args` entry and replace it with
   `["--thread-pool-size=1", "--announce-submounts", "--posix-acl=always"]`,
   convert legacy `dial_timeout`/`cdh_api_timeout` (seconds) to the
   millisecond forms, and replace the `[runtime]` section with
   `name = "virt_container"`, `hypervisor_name = "qemu"`, `agent_name = "kata"`.
6. Validate the shim (`--version` must print `kata_acl_shim=VALID`).
7. Register the runtime in `/etc/docker/daemon.json` (backing it up first) and
   `systemctl reload docker`.
8. Smoke test: run `denis-obsidian-assistant:latest` with `--runtime kata-acl`.

**Transactionality.** A `trap` on `EXIT HUP INT TERM` restores the backed-up
`daemon.json`, removes the generated configuration and `/opt/kata-acl`, and
reloads Docker on any failure. A verified archive is retained in the cache on
success; a partial download is kept for resume on failure.

**Idempotency.** Deliberately **not** idempotent: the pre-flight guards abort
if anything already exists. Re-running requires `uninstall_kata_acl.sh` first.

### 4.3 `uninstall_kata_acl.sh`

Removes the parallel runtime without touching the primary one.

- Root required.
- **Any container — running or stopped — whose `HostConfig.Runtime` is
  `kata-acl` blocks the operation** (`container_uses_runtime=<name>`).
- The confirmation prompt requires the literal string `yes`.
- `daemon.json` is edited with `jq` (`del(.runtimes["kata-acl"])`, and the
  `runtimes` object is removed if it becomes empty), then Docker is reloaded
  and the absence of the runtime is verified.
- The ACL configuration and `/opt/kata-acl` are removed.

### 4.4 `migrate_to_kata_4_1.sh`

A **one-shot, destructive** migration that promotes the staged `kata-acl`
bundle to be the *only* Kata runtime.

**Guards.** Root; `--yes` is mandatory; `docker jq systemctl sha256sum sed`
must be present; the candidate shim must exist and report `4.1.0`; the ACL
configuration must exist; `/opt/kata` and `/etc/docker/daemon.json` must
exist.

**Steps.**

1. Stage copies of `daemon.json` and both Kata configurations.
2. **Enumerate every container using runtime `kata`, stop the running ones and
   `docker rm` all of them.** This destroys sandbox containers.
3. `mv /opt/kata → <stage>/legacy-kata`, then `mv /opt/kata-acl → /opt/kata`.
4. Generate the runtime configuration with `sed`: rewrite the `/opt/kata-acl/`
   prefix, set
   `virtio_fs_extra_args = ["--thread-pool-size=1", "-o", "announce_submounts"]`,
   `memory_slots = 10`, `static_sandbox_resource_mgmt = true`.
5. Rewrite `daemon.json` to point `runtimes.kata` at the promoted shim and
   delete `runtimes["kata-acl"]`; reload Docker; verify; smoke-test.
6. Delete the legacy `/etc/kata-containers/configuration.toml`, the ACL
   configuration and `/usr/local/bin/kata-runtime`; re-point the shim symlink.

**Rollback.** A failure trap restores `daemon.json`, moves both bundles back,
restores the configurations, reloads Docker and prints
`kata_migration_rollback=ATTEMPTED`. The rollback is best-effort: **the
destroyed containers are not recreated.**

### 4.5 ACL canaries

Four scripts validate POSIX-ACL propagation through VirtioFS. All are
re-runnable and leave nothing behind: each uses a random container name and a
`mktemp -d` share, and the cleanup trap removes both.

| Script | Runtime | Root | What it proves |
|---|---|---|---|
| `run_kata_acl_fullstack_canary.sh` | `kata-acl` | yes | Baseline ACL propagation with `--announce-submounts`. |
| `run_kata_acl_annotation_canary.sh` | `kata-acl` | yes | A per-container `virtio_fs_extra_args` annotation reaches the host `virtiofsd` process. |
| `run_kata_acl_no_submount_canary.sh` | `kata-acl` | yes | The same with `--no-announce-submounts`; temporarily rewrites and then restores `configuration-acl.toml`. |
| `run_kata_virtiofs_acl_canary.sh` | `kata` (live) | yes | A standalone newer `virtiofsd` (extracted from the Kata archive) with `--posix-acl` works against the live runtime. |

**Common mechanics.** A host share with a nested subdirectory and a probe file
is created under `/tmp`; `setfacl` grants UID 1002 traversal and read access
and sets a default ACL; a container runs `--read-only` with the share mounted
read-only; the assertions check the container's runtime, the host
`virtiofsd` arguments (`ps -eo args=`), the guest's
`system.posix_acl_access` xattr (`getfattr -e hex`), the default ACL
(`getfacl -cpn`) and actual access as UID 1002 (`docker exec --user 1002:1002`).

**Recorded outcome** (`_kata_acl_install.md`): the full-stack canary
**failed** — the guest reported `Operation not supported` for
`system.posix_acl_access` — because `--announce-submounts` breaks host POSIX
ACL propagation. The `--no-announce-submounts` variant **passed** twice. The
conclusion was: do **not** globally disable announced submounts in the primary
`kata` runtime; use the per-container annotation or the parallel runtime
instead.

The two configuration-rewriting canaries refuse to run when any container
already uses the target runtime, and they verify the restored configuration
by SHA-256.

## 5. The privileged backup helper

### 5.1 Why it exists

Files created inside a VM by a different host UID/GID cannot be read by the
sndbx service user. Without help, `tar` fails on those files and the archive
is incomplete. The helper performs exactly one archive operation as root.

### 5.2 `install_backup_acl_helper.sh`

```bash
sudo ./install_backup_acl_helper.sh <sndbx-service-user>
```

Requires root and exactly one argument.

**Steps.**

1. Resolve `images/` and `backup/` under the checkout; create `backup/` with
   mode `0750` if absent. Both must exist afterwards.
2. `install -d` `/usr/local/libexec` and `/etc/sndbx-backup-helper` (root,
   0755).
3. Install `prepare_backup_permissions.sh` as
   `/usr/local/libexec/sndbx-backup-helper` (root, 0755).
4. Write four root-owned configuration files (0644) in
   `/etc/sndbx-backup-helper/`: `images-root`, `backup-root`, `state-root`
   (`/var/lib/sndbx-backup`) and `service-uid`.
5. `install -d /var/lib/sndbx-backup` (root, 0700) — the root-only state
   directory for `.snar` snapshots and lock files.
6. Write `/etc/sudoers.d/sndbx-backup-helper` (0440):

   ```sudoers
   <service_user> ALL=(root) NOPASSWD: /usr/local/libexec/sndbx-backup-helper *
   ```

   Validate it with `visudo --check`.
7. Remove the legacy artifacts `/usr/local/libexec/sndbx-backup-acl`,
   `/etc/sudoers.d/sndbx-backup-acl` and `/etc/sndbx-backup-acl`.

**Idempotency.** Yes — every step overwrites in place and the sudoers file is
re-validated.

### 5.3 The helper itself

`prepare_backup_permissions.sh <local-image-id> <full|incremental> <archive-name>`

**Argument validation.** The image id must match
`^[A-Za-z0-9][A-Za-z0-9_.-]*$`; the type must be `full` or `incremental`; the
archive name must match
`^<image-id>-\d{4}-\d{2}-\d{2}T\d{6}\.\d{3}Z-<type>\.tar\.gz$` with the type
matching argument 2.

**Operations.**

1. `source_dir = realpath <images-root>/<image-id>` — must exist, must not be
   a symlink, must be under `images-root`.
2. `backup_dir = <backup-root>/<image-id>` — created with
   `install -d --owner <uid> --group <uid> --mode 0750`, then re-validated.
3. `state_dir = <state-root>/<image-id>` — root-owned, 0700. A `flock` on
   `backup.lock` provides per-image mutual exclusion.
4. Delete stale `.<image-id>.tar.*` temporary archives.
5. Incremental mode requires the root-owned `<image-id>.snar` snapshot;
   full mode starts without one.
6. `tar --create --gzip --ignore-failed-read --listed-incremental=<work.snar>
   --file=<temp> --directory <dirname(source_dir)> <basename(source_dir)>` —
   the archive is relative to the parent, so it contains `<image-id>/…`.
7. `mv` the temporary archive into `backup_dir` and the working snapshot into
   `state_dir`; `chown <uid>:<uid>`, `chmod 0640` the archive.

**Safety properties.**

- Reads only the configured image.
- Writes only the configured backup directory and the root-only state
  directory.
- Never modifies ACLs or permissions inside the image.
- `--ignore-failed-read` tolerates files mutated by a running VM.
- A `trap` removes the temporary snapshot and archive on failure; the live
  snapshot is advanced only after a successful `mv`.

## 6. The backup scheduler at runtime

`src/backup.py` runs inside the manager process. It is **not** cron, not a
systemd timer and not a separate daemon.

**Schedule.** Per sandbox with `backup.enabled: true`:

```json5
backup: { enabled: true, interval_hours: 24, full_every: 7 }
```

- The first run is always **full**.
- A **full** archive is produced again once `full_every` intervals have
  elapsed since the last full archive.
- Otherwise the run produces an **incremental** archive against the `.snar`
  snapshot.

**Archive layout.**

```text
backup/<local_image_id>/
  <local_image_id>-<UTC>.<ms>Z-full.tar.gz
  <local_image_id>-<UTC>.<ms>Z-incremental.tar.gz
  <local_image_id>.chain.json        # last_full / last_incremental
  <local_image_id>.snar               # snapshot, only without the root helper
```

With the root helper installed, the snapshot lives in the root-only
`/var/lib/sndbx-backup/<local_image_id>/` instead.

**Conflict rule.** Two sandboxes sharing one `local_image_id` must declare
identical backup settings. If they do, only one job is scheduled and the
second sandbox is recorded as sharing the schedule. If they differ, the
second is **rejected** and logged.

**Restore.** The full archive and every subsequent incremental must be applied
in chronological order against the same snapshot file, into an empty
directory. The procedure is in `README.md` and `proposal_backup.md`.

**Limitations.** The archive is file-level, not application-consistent — the
VM is not stopped. External bind mounts outside `images/<id>/` are not
included. Losing one archive breaks the chain from that point on.

## 7. Host state inventory

| Path | Created / modified by | Removed by |
|---|---|---|
| `/etc/systemd/system/sndbx.service` | `install.sh` | `uninstall.sh` |
| `~/.config/systemd/user/sndbx.service` | `install.sh` | `uninstall.sh` |
| `/etc/docker/daemon.json` | `install_prerequisites.sh`, `install_kata_acl.sh` | — (entries removed by the Kata scripts) |
| `/etc/kata-containers/runtime-rs/configuration.toml` | `install_prerequisites.sh` | `migrate_to_kata_4_1.sh` |
| `/etc/kata-containers/runtime-rs/configuration-acl.toml` | `install_kata_acl.sh` | `uninstall_kata_acl.sh`, `migrate_to_kata_4_1.sh` |
| `/etc/kata-containers/configuration.toml` | legacy | `migrate_to_kata_4_1.sh` |
| `/opt/kata` | `install_prerequisites.sh` | replaced by `migrate_to_kata_4_1.sh` |
| `/opt/kata-acl` | `install_kata_acl.sh` | `uninstall_kata_acl.sh` |
| `/usr/local/bin/containerd-shim-kata-v2` | `install_prerequisites.sh` | — |
| `/usr/local/bin/kata-qemu-wrapper` | `install_prerequisites.sh` (low phys-bits hosts) | — |
| `/usr/local/bin/kata-runtime` | legacy | `migrate_to_kata_4_1.sh` |
| `/usr/local/libexec/sndbx-backup-helper` | `install_backup_acl_helper.sh` | — (not removed by `uninstall.sh`) |
| `/etc/sndbx-backup-helper/*` | `install_backup_acl_helper.sh` | — |
| `/etc/sudoers.d/sndbx-backup-helper` | `install_backup_acl_helper.sh` | — |
| `/var/lib/sndbx-backup/` | `install_backup_acl_helper.sh` | — |
| `/var/cache/sndbx-kata-acl/` | `install_kata_acl.sh` | — (cache is reused) |
| `/etc/apt/sources.list` (+ `.sndbx-backup`) | `install_prerequisites.sh --apt_mirror` | restored by the script |
| Docker network `sndbx-net` | `install.sh` | `uninstall.sh` (optional) |
| Docker group membership | `install_prerequisites.sh` | — |
| UFW rules | `install.sh` (interactive) | — |

## 8. Idempotency matrix

| Script | Re-runnable | Notes |
|---|---|---|
| `install.sh` | Mostly | An explicit config argument overwrites `config.json5`; the UFW prompt repeats. |
| `uninstall.sh` | Yes | Interactive; leaves `images/`, `backup/`, `sessions/`, the backup helper and `kata-acl` behind. |
| `install_prerequisites.sh` | **Yes** | Every step is guarded; Docker is reloaded only on change. |
| `install_kata_acl.sh` | **No** | One-shot; the guards abort if anything already exists. |
| `uninstall_kata_acl.sh` | Yes | Apart from the confirmation prompt and the container guard. |
| `migrate_to_kata_4_1.sh` | **No** | One-shot and destructive. |
| `install_backup_acl_helper.sh` | **Yes** | Overwrites in place; removes legacy artifacts. |
| `prepare_backup_permissions.sh` | Yes | `flock`-protected; the snapshot advances only on success. |
| `run_kata_*_canary.sh` | Yes | Disposable resources only; configurations restored by SHA-256. |
| `test_mcp.sh` | Yes | Read-only HTTP calls. |

## 9. Known defects in the host tooling

All defects that were recorded here have been fixed. The table is kept as a
record of what was wrong and what replaced it.

| # | Former defect | Fix |
|---|---|---|
| 1 | `install.sh`: `docker_cmd()` captured `rc=$?` **after** the `if docker …; then … fi` compound. When the condition fails and there is no `else`, the compound's status is 0, so `rc` was always 0 and the function returned success. | The status is now captured directly from the command. A failing Docker command returns its real exit code, so the Kata readiness smoke test warns correctly. |
| 2 | `install.sh` hard-coded the Kata shim path `/opt/kata/runtime-rs/bin/containerd-shim-kata-v2`. | The shim path is read from `runtimes.kata.runtimeType` in `/etc/docker/daemon.json`, with the historical path as the fallback. A `--kata_path` override is honoured. |
| 3 | `install.sh` did not initialise the `aiset` submodule. | `git submodule update --init --recursive` runs during installation, and the installer aborts with a clear message when `aiset/src/aiset` is missing. |
| 4 | `install.sh` created the Docker network `sndbx-net`, which nothing in `src/` uses. | The network is no longer created. `uninstall.sh` still offers to remove an existing one. |
| 5 | `install.sh` never created `sessions/` or `backup/`. | Both are created during installation. |
| 6 | `install_kata_acl.sh` required the **legacy** `/etc/kata-containers/configuration.toml` as its template source. | It now uses the runtime-rs configuration when present and falls back to the legacy path. |
| 7 | `run_kata_virtiofs_acl_canary.sh` expected the legacy `/usr/local/bin/kata-runtime`, which `migrate_to_kata_4_1.sh` deletes. | The canary resolves the configuration from `daemon.json` and validates it by parsing the TOML with `tomllib` instead of calling the removed binary. |
| 8 | `install_kata_acl.sh` and `migrate_to_kata_4_1.sh` hard-coded `denis-obsidian-assistant:latest` for their smoke tests. | The image is configurable through `SNDBX_SMOKE_IMAGE`. |
| 9 | `uninstall.sh` left `sessions/`, `backup/`, `config.json5.bak*`, the backup helper and its sudoers rule behind. | They are now removed: the in-tree paths by the removal menu, the privileged helper by a separate opt-in prompt. `images/` and the `kata-acl` runtime are still left alone. |
| 10 | `.env.example` declared `VM_ID`, `VM_SSH_USER`, `VM_SSH_PORT_RANGE_START` and `VM_SSH_PORT_RANGE_END`, which nothing consumes. | Removed. |
| 11 | `config.json5` consumed `SNDBX_INSTANCE` and `LOG_TIMEZONE`, which `.env.example` did not declare. | Both are declared now, together with the new log-management variables. |
| 12 | `logging.wipe_period` / `LOG_WIPE_PERIOD` was plumbed into the configuration but had no implementation. | Implemented: `wipe_expired_logs()` removes expired entries and an asyncio task schedules the runs. The MCP request log additionally has a size cap. |

## 10. Smoke tests

| Check | Command |
|---|---|
| Kata shim present | `/opt/kata/runtime-rs/bin/containerd-shim-kata-v2 --version` |
| Kata runtime registered | `docker info --format '{{json .Runtimes}}' \| grep -q '"kata"'` |
| Kata can start a container | `docker run --rm --runtime kata alpine echo OK` |
| Docker healthy | `docker info` |
| Service running | `systemctl --user is-active sndbx` |
| Service logs | `journalctl --user -u sndbx -n 200 --no-pager` |
| Web UI | `curl -s http://127.0.0.1:30080/api/auth/me` → `401` without a cookie |
| MCP | `./test_mcp.sh http://127.0.0.1:30081 <token> <envid>` |
| Config loads | `PYTHONPATH=src venv/bin/python -c "from config import load_config; print(load_config()['config']['title'])"` |

`test_mcp.sh` issues three requests: a `sandbox_status` with a valid token, a
`sandbox_status` with an invalid token (expected to fail), and an
`execute_command`. It is a connectivity and parse check, not an assertion
harness — a JSON-RPC error still exits 0.

Note that `test_mcp.sh` posts to the bare URL. The MCP server accepts `POST
/`, `POST /mcp` and `POST /mcp/v1`, so the bare URL works, but `POST /mcp` is
the canonical path.
