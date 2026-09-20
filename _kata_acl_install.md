# Parallel Kata ACL Runtime

## Purpose

This setup installs Kata 4.1.0 as a separate Docker runtime named `kata-acl`.
It does not replace the existing `kata` runtime, change Docker's default runtime,
or modify `/etc/kata-containers/configuration.toml`.

`kata-acl` uses a separate full stack under `/opt/kata-acl`, including its QEMU,
guest kernel, agent, and `virtiofsd`. Its VirtioFS configuration enables
`--posix-acl=always`.

## Current Status

Installed successfully on 2026-09-20. The installer used the verified local
archive (969,565,384 bytes) with the fixed SHA-256 and completed its disposable
runtime smoke test. An earlier installer version removed its archive cache after
success; this was corrected before any future reinstall. The current archive is
not retained because that earlier cleanup already removed it.

Kata 4.1.0 ships the Rust `runtime-rs` implementation, not the older
`kata-runtime` executable. The installer uses the archive's actual shim at
`/opt/kata-acl/runtime-rs/bin/containerd-shim-kata-v2` and an isolated config
at `/etc/kata-containers/runtime-rs/configuration-acl.toml`. The existing
`kata` runtime and active sandboxes remain unchanged. The active
`sndbx-denis-obsidian-assistant` container still uses `kata`, not `kata-acl`.

Installation validation completed:

```text
kata_acl_shim=VALID
docker_runtime=REGISTERED
kata_acl_smoke=PASS
kata_acl_install=PASS
```

## Files Added

- `install_kata_acl.sh`: transactional privileged installer.
- `uninstall_kata_acl.sh`: guarded privileged remover.
- This file: installation, validation, and removal record.

## Installation

Run this exact command from the sndbx checkout:

```bash
cd /home/denis/sndbx
sudo bash ./install_kata_acl.sh
```

The installer performs these actions in order:

1. Downloads the official Kata 4.1.0 AMD64 static archive.
2. Verifies SHA-256 `3dc6b69c4acb787b967b04b64599a20d02a8beb1a8eaab3084110df9d0b08c96`.
3. Installs the extracted runtime only at `/opt/kata-acl`.
4. Creates `/etc/kata-containers/runtime-rs/configuration-acl.toml` from the
   active known-good QEMU configuration, with all Kata paths redirected to
   `/opt/kata-acl` and
   VirtioFS arguments set to `--thread-pool-size=1`, `--announce-submounts`,
   and `--posix-acl=always`.
5. Verifies the new runtime-rs shim binary.
6. Adds only `runtimes.kata-acl` to `/etc/docker/daemon.json`, pointing to
   `/opt/kata-acl/runtime-rs/bin/containerd-shim-kata-v2` and the new config.
7. Reloads Docker without restarting it, confirms that Docker lists `kata-acl`,
   and runs one disposable `--runtime kata-acl` `/bin/true` smoke container.

Expected final markers:

```text
kata_acl_shim=VALID
docker_runtime=REGISTERED
kata_acl_smoke=PASS
kata_acl_install=PASS
```

If an installation step fails, the script restores the original Docker JSON,
removes the new ACL configuration and runtime directory, and removes its
temporary staging directory. It never restarts Docker.

## Download Recovery

The official QEMU full-stack asset is
`kata-static-4.1.0-amd64.tar.zst` (969,565,384 bytes). Kata does not publish a
smaller QEMU-specific runtime archive or an official mirror for this release.

The installer downloads from the official GitHub release over HTTP/1.1 and
keeps an interrupted transfer at:

```text
/var/cache/sndbx-kata-acl/kata-static-4.1.0-amd64.tar.zst.part
```

Run the same installer command again after a network error. It reports
`resume_bytes=<number>` and resumes the partial archive. Once complete, it
prints `archive_cache=VALID` and uses the cached archive without downloading.
The SHA-256 check is always performed before extraction. A successful
installation retains the verified archive for a later controlled upgrade. A
failed installation retains only a nonempty download so it can resume.

To use a separately acquired archive, provide its absolute path. The same
fixed SHA-256 verification still applies:

```bash
sudo KATA_ACL_ARCHIVE=/absolute/path/kata-static-4.1.0-amd64.tar.zst \
   bash ./install_kata_acl.sh
```

## Verification

After a successful installation, confirm registration without changing any
sandbox:

```bash
docker info --format '{{json .Runtimes}}' | jq '."kata-acl"'
sudo /opt/kata-acl/runtime-rs/bin/containerd-shim-kata-v2 --version
```

The next required stage is a disposable full-stack ACL canary using
`docker run --runtime kata-acl`. Do not assign `kata-acl` to the assistant or
change the default Kata runtime until that canary confirms ACL metadata and UID
1002 access on a temporary share.

The manager supports `kata_runtime: "kata-acl"` only as an explicit per-sandbox
setting. No current sandbox configuration enables it.

## Full-Stack ACL Canary

The disposable full-stack canary was run on 2026-09-20 using
`run_kata_acl_fullstack_canary.sh`. It created only a temporary `/tmp` share and
a disposable `kata-acl` container, then removed both.

The container was confirmed to use `kata-acl` and its active host-side
`/opt/kata-acl/libexec/virtiofsd` process was confirmed to receive
`--posix-acl`. Despite this, guest access to
`system.posix_acl_access` returned `Operation not supported`. The canary ended
with `cleanup=PASS` on both runs.

Do not move the assistant to `kata-acl`: Kata 4.1.0 does not restore the needed
host POSIX ACL propagation in this environment. Retain `kata-acl` only until
the chosen alternative access model is implemented or until a separate upstream
investigation is complete; use the removal procedure below when it is no longer
needed.

## No-Submount ACL Canary

The upstream VirtioFS submount workaround was tested on 2026-09-20 with
`run_kata_acl_no_submount_canary.sh`. The wrapper temporarily changes only the
isolated `kata-acl` configuration to:

```toml
virtio_fs_extra_args = ["--thread-pool-size=1", "--no-announce-submounts", "--posix-acl=always"]
```

It then runs the full-stack disposable canary and restores the exact original
configuration by SHA-256. The canary passed twice: guest `getfattr` returned
`system.posix_acl_access`, `getfacl` exposed the host named/default UID 1002
ACLs, and UID 1002 could traverse and read the temporary share. Each run
reported both `cleanup=PASS` and `kata_acl_config_restored=PASS`.

This confirms that announced submounts trigger the ACL propagation failure in
this environment. Do not globally disable them in the original `kata` runtime.
Before moving the assistant to `kata-acl` with this setting, inspect its mounts
for required nested filesystem boundaries and explicitly accept the documented
submount behavior trade-off.

## Clean Removal

Before removal, delete every disposable container created with `--runtime
kata-acl`. The remover refuses to continue while such a container exists.

```bash
cd /home/denis/sndbx
sudo bash ./uninstall_kata_acl.sh
```

Type `yes` when prompted. The remover performs only these changes:

1. Removes `runtimes.kata-acl` from `/etc/docker/daemon.json`.
2. Reloads Docker without restarting it.
3. Removes `/etc/kata-containers/runtime-rs/configuration-acl.toml`.
4. Removes `/opt/kata-acl`.

It does not remove or alter the original Docker `kata` entry, `/opt/kata`, or
`/etc/kata-containers/configuration.toml`.
