# sndbx Minimal Image Contract for LangVM

## Goal

LangVM already owns projects, durable runs, artifacts, schedules, release
rollback, encrypted backup, and application policy. sndbx must not duplicate
those concerns. Its role is to provide a small, generic image runtime contract
that lets an optional LangVM image start predictably and prove that its declared
dependencies are usable.

The required outcome is a preflighted sandbox with immutable source/config
mounts, one writable data root containing the LangVM-owned PostgreSQL cluster,
an optional `.env` mount, loopback-default MCP publication, and a bounded
readiness result.
The design remains useful to images other than LangVM and does not make LangVM a
sndbx dependency.

## P0: Minimal Generic Contract

## Implementation Status

- [x] Stage 1 - mount preflight, optional-file omission, explicit mount types,
   and loopback-default LangVM port configuration.
- [x] Stage 2 - versioned runtime contract, bounded readiness, and companion
   environment injection.
- [x] Stage 3 - declare the LangVM runtime contract and run generic plus
   LangVM conformance checks.
- [x] Stage 4 - move PostgreSQL 16 into the LangVM image so `data/postgres/`
   remains the complete local database state in Compose and sndbx.

Validation completed: the focused sndbx contract suite passes 12 tests; the
LangVM concurrent project-migration regression passes; and
`scripts/release_matrix.sh sndbx` passes through the restarted production
controller. The live sandbox has read-only required mounts, omits a missing
optional `.env`, starts PostgreSQL 16 from its data root, reports ready at
`/readyz`, and runs its image-owned worker and scheduler.

### 1. Mount Preflight and Optional Inputs

Extend `shared_directories` with these additive fields:

- `required`: defaults to `true`.
- `create_if_missing`: defaults to `false`; valid only for writable directory
   mounts.
- `source_type`: `file` or `directory`, inferred only when unambiguous.

Before container creation, validate every required path, its type, and mount
permission. Omit a missing optional mount; never create an empty optional file.
Return one actionable preflight result and pass a redacted resolved mount list
to the image hook.

For LangVM this expresses exactly:

| Guest path | Required | Permission | Source type |
|---|---:|---|---|
| `/mnt/src` | yes | read-only | directory |
| `/opt/langvm/config.json5` | yes | read-only | file |
| `/var/lib/langvm` | yes, create root if absent | read-write | directory |
| `/opt/langvm/.env` | no | read-only | file |

The writable `data/` directory contains LangVM projects, artifacts, extensions,
backups, logs, workspaces, and the PostgreSQL 16 cluster at `data/postgres/`.
sndbx must not create or manage subdirectories inside it.

### 2. One Runtime Contract and Readiness Result

Add an optional versioned `runtime_contract` object to sandbox configuration.
Do not add a second manifest file or image-specific scheduler. The contract
declares only required/writable guest paths, optional mount targets, a startup
hook, readiness probes with bounded retries, named companion-service references,
and published ports.

Initial readiness probe types are HTTP, TCP, and command. sndbx marks a sandbox
ready only after preflight, its existing startup hook, and all probes succeed.
Hook input contains the sandbox ID, image reference, redacted resolved mounts,
and companion-service aliases. It never contains secret values.

The LangVM contract needs this HTTP probe:

```json
{"type": "http", "url": "http://127.0.0.1:8081/readyz"}
```

The existing `on_system_start` hook remains the compatibility hook. LangVM owns
its worker and scheduler in its image entrypoint; sndbx observes the resulting
container and HTTP readiness. A worker or scheduler failure terminates the
container, so normal sandbox restart policy remains the sole supervisor.
Additional hook phases, generic hook mutation APIs, and platform-owned service
orchestration are out of scope until a concrete second image requires them.

### 3. Port and Companion-Service References

Published ports default to loopback. A non-loopback bind must be explicit in
configuration and appear in sandbox status and audit logs. This changes the
LangVM example default from `0.0.0.0` to `127.0.0.1`.

Add a named companion-service reference with a host/port or Docker-network
alias and a readiness probe. Before composing runtime environment settings,
sndbx invokes the existing image hook in capability-discovery mode and reads its
JSON result. LangVM reports `{"provides_postgres": true}` only when its guest
configuration sets `SNDBX_PROVIDES_POSTGRES=true`; this declares that the guest
owns its PostgreSQL endpoint. In that case sndbx does not inject a companion
database setting. Otherwise, sndbx injects the reference as the stable
`DATABASE_HOST` and `DATABASE_PORT` settings, never inferring it from the
default gateway. Secret values remain in the configured secret input; hook and
status data contain only the boolean capability and reference name.

discovery. It does not need a platform database, queue, vector store, or job
LangVM does not use a companion-service reference. Its image starts PostgreSQL
16 before its app, worker, and scheduler; `DATABASE_HOST=auto` resolves to
`127.0.0.1:5432`, and the cluster is persisted below the one writable data root.
The generic companion mechanism remains available for other images but does not
apply to LangVM.

### 4. Optional-Image Conformance Test

Add one reusable conformance suite for a standard sample image. When the local
LangVM checkout is present, run the same generic checks conditionally:

1. Required/optional mount preflight.
2. Read-only source and writable data-root behavior.
3. Startup hook and `/readyz` readiness.
4. Worker and scheduler supervision by the image entrypoint.
5. Loopback port status and image-owned PostgreSQL readiness.
6. Stop/start persistence.

LangVM retains `scripts/release_matrix.sh` for builder, handoff, durable-run,
internal-database backup/restore, and update behavior. sndbx must not proxy or
duplicate LangVM's durable run lifecycle.

## Explicitly Deferred

The following are not required for the updated LangVM and should not be added
to the first platform contract:

- Managed per-run workspace/artifact volumes and their TTL lifecycle.
- A platform-neutral asynchronous job/event bridge.
- Resource/capability profile catalogues beyond existing sandbox limits.
- Egress profiles, ingress allow-lists, and reverse-proxy integration.
- Platform-wide secret storage or rotation.
- Image reconciliation, digest tracking, and automatic rollback.
- Multiple hook phases beyond startup compatibility and readiness.

These can be proposed separately when sndbx manages multiple revisions or a
second image demonstrates a concrete need. LangVM's release, backup, policy,
artifact, and durable run models remain authoritative in the meantime.

## Delivery Order

1. Implement mount preflight, optional-mount omission, and loopback port
   defaults without changing existing configurations.
2. Add `runtime_contract` readiness and optional companion-service references,
   then declare the LangVM contract in `config.sndbx.json5`.
3. Add the conditional generic conformance suite and run it with LangVM when
   available locally.

## Acceptance Criteria

With only the declared paths and secrets configured, sndbx can preflight a
LangVM sandbox, omit a missing optional `.env`, start its image-owned
PostgreSQL, worker, and scheduler, wait for LangVM `/readyz`, report the
loopback MCP endpoint, and preserve the writable data root across a stop/start.
No secret value appears in preflight results, hook input, status, or logs. A
sndbx installation without LangVM continues to use the same generic contract
and test suite.

## Confirmed Decisions

1. Optional mounts are omitted when absent; required mounts fail preflight.
2. sndbx creates only explicitly requested writable directories, never files.
3. `runtime_contract` is an additive JSON-compatible object in sandbox
   configuration, not a separate image manifest.
4. HTTP, TCP, and command readiness probes are sufficient for the first
   contract.
5. Published ports default to loopback; broader exposure is explicit.
6. LangVM durable jobs, workspaces, artifacts, secrets, and release rollback
   remain application-owned.
7. LangVM image entrypoint owns and supervises the durable worker and scheduler.
8. LangVM sets `SNDBX_PROVIDES_POSTGRES=true`, starts PostgreSQL 16 itself, and
   never receives a sndbx companion database endpoint.