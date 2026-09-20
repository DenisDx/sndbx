# Proposal Update 2: Kata 4.1 Default Runtime and Selective ACL Compatibility

## Goal

Replace the legacy Kata 3.30 installation with one maintained Kata 4.1
installation for sndbx. A clean sndbx installation must install and register
Kata 4.1 as Docker runtime `kata`.

The default Kata 4.1 configuration must retain normal VirtioFS behavior. The
POSIX ACL workaround is an explicit per-sandbox compatibility profile, not a
global default.

## Problem

The existing Kata runtime does not expose host POSIX ACL metadata through
VirtioFS. The assistant needs host ACL entries for UID 1002 to access its
Obsidian share without weakening host permissions.

An isolated Kata 4.1 canary established the following behavior:

- `--posix-acl=always` with announced submounts still fails with
  `EOPNOTSUPP` for `system.posix_acl_access`.
- `--posix-acl=always --no-announce-submounts` exposes host access/default
  ACLs and permits the intended UID access.

Upstream attributes this to the Linux FUSE submount path not setting the
POSIX ACL superblock flag. Disabling announced submounts is therefore a
compatibility workaround with a real limitation: nested host mount points are
not announced as guest filesystem boundaries.

## Decisions

1. sndbx will support one installed Kata version: Kata 4.1, located at
   `/opt/kata`, and Docker runtime name `kata`.
2. The main prerequisite installer will be migrated from the legacy Go
   `kata-runtime` layout to the Kata 4.1 `runtime-rs` shim and configuration
   schema.
3. Default sandboxes will use standard Kata 4.1 VirtioFS behavior. They will
   not receive POSIX ACL or no-submount arguments.
4. A sandbox may explicitly select `virtiofs_profile: "posix-acl-compat"`.
   That profile must use only the fixed VirtioFS arguments
   `--thread-pool-size=1`, `--no-announce-submounts`, and
   `--posix-acl=always`.
5. The manager must reject the compatibility profile when any shared source
   directory contains a nested mount point. This avoids silently changing the
   guest view of host filesystems.
6. Arbitrary Kata annotations remain prohibited. The manager owns the
   allow-listed profile and its arguments.
7. Existing sandboxes are never changed in place. A migration requires an
   explicit recreate and accepts a rollback procedure.

## Mandatory Validation Gate

Before implementing the default-runtime migration, a disposable Kata 4.1
canary must prove that the runtime-rs configuration accepts the allow-listed
container annotation for `virtio_fs_extra_args`. The canary must leave the
base `kata-acl` configuration on normal announced-submount behavior and prove
inside one disposable guest that the compatibility profile exposes POSIX ACLs.

If the annotation does not override the configuration reliably, the fallback
is one installed Kata 4.1 bundle with two named Docker runtime profiles. This
is not the preferred architecture, but it isolates the workaround without
changing default shares.

## Implementation Stages

### Stage 1: Per-Sandbox Compatibility Validation

- Add a self-cleaning annotation canary using only the disposable `kata-acl`
  runtime.
- Verify ACL xattr visibility, `getfacl`, UID 1002 traversal/read access,
  cleanup, and unchanged base configuration.
- Add focused manager tests for the opt-in profile and nested-mount rejection.

### Stage 2: Manager Contract

- Add the `virtiofs_profile` allow-list to `DockerSandboxManager`.
- Reject unknown profiles and reject a compatibility-profile directory that
  contains nested mount points.
- Keep the profile absent by default in all configuration templates.

### Stage 3: Kata 4.1 Installer Migration

- Rewrite `install_prerequisites.sh` for the runtime-rs shim and Kata 4.1
  configuration fields, retaining amd64 and arm64 support.
- Pin a tested Kata release and verify every archive checksum, including
  supplied offline archives.
- Register only Docker runtime `kata`, validate Docker configuration before
  reload, and run a disposable normal-profile smoke container.
- Provide an explicit migration/rollback command. The installer must refuse
  to replace an active legacy Kata runtime while containers still use it.

### Stage 4: Controlled Rollout

- Recreate one disposable default sandbox and validate normal shared mounts.
- Recreate only the Obsidian assistant with `virtiofs_profile:
  "posix-acl-compat"`.
- Validate actual Obsidian ACL visibility and UID 1002 access, SSH on 11022,
  HTTPS on 11443, and Gateway availability.
- Retire the parallel `kata-acl` runtime only after all migration acceptance
  checks pass.

## Non-Goals

- Do not disable announced submounts globally.
- Do not permit user-supplied arbitrary Kata annotations.
- Do not alter `/home/denis/Obsidian` or other external host shares during
  canaries.
- Do not restart `sndbx.service` as part of runtime validation.

## Acceptance Criteria

- A clean Ubuntu 22.04+ installation provisions Kata 4.1 and Docker runtime
  `kata` through the main sndbx installer.
- Default sandboxes retain announced-submount behavior and pass their normal
  mount smoke tests.
- Only explicitly configured compatibility sandboxes get the ACL workaround.
- Compatibility sandboxes refuse nested host mount sources before Docker
  creation.
- The assistant reads the host ACL on the real Obsidian mount as UID 1002
  without changing host permissions.
- Every privileged installer action has a tested rollback path and does not
  remove active runtime assets.

## Migration Result

The host migration completed on 2026-09-20. The transactional migration
stopped and removed every Docker container using the legacy `kata` runtime,
promoted the staged Kata 4.1 bundle to `/opt/kata`, replaced the Docker `kata`
entry with the runtime-rs shim/configuration, and removed the `kata-acl`
runtime, legacy bundle, and legacy configuration only after a disposable smoke
container passed.

Kata 4.1 runtime-rs requires `static_sandbox_resource_mgmt = true` and a
nonzero `memory_slots` value for Docker memory limits. The migration and the
main installer preserve the Kata 4.1 default `memory_slots = 10`; they must
not inherit the legacy `memory_slots = 0` compatibility setting.

The two sandboxes that were running before migration were recreated:

- `sandbox-1` runs with the normal VirtioFS profile.
- `denis-obsidian-assistant` runs with `posix-acl-compat`.

Post-migration checks confirmed Kata 4.1.0, no remaining `kata-acl` runtime or
legacy Kata assets, host Obsidian ACL visibility inside the assistant, UID 1002
access, SSH on port 11022, and HTTPS on port 11443.