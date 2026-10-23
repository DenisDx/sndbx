# sndbx — Project Specification

> **Status:** this document supersedes `SPEC.md`.
> `SPEC.md` describes the original design intentions and is kept only for
> historical reference. Where the two disagree, **this document is
> authoritative** — it was written against the code that actually exists.
>
> **Audience:** developers and agents working on sndbx itself. It is not a
> user guide; installation and day-to-day operation live in `README.md`,
> `GETTING_STARTED.md` and `TEST.md`.

## Companion documents

| Document | Owns |
|---|---|
| `SPEC_NEW.md` (this file) | Purpose, architecture, domain model, component catalogue, configuration model, filesystem layout, engineering decisions. |
| [`docs/IMAGES.md`](docs/IMAGES.md) | The image contract in depth: `images/<id>` layout, `app.py` hook protocol, runtime contract, per-image catalogue, how to add an image. |
| [`docs/INTERFACES.md`](docs/INTERFACES.md) | Complete protocol reference: MCP HTTP/SSE, Web UI REST, WebSocket console, settings ASGI, image hook, runtime contract. |
| [`docs/HOST_OPERATIONS.md`](docs/HOST_OPERATIONS.md) | Host-side provisioning: prerequisites, Kata runtimes and the ACL variant, service units, backup ACL helper, canaries. |
| [`docs/STATUS_AND_ROADMAP.md`](docs/STATUS_AND_ROADMAP.md) | Open tasks, known problems, unimplemented configuration keys, planned directions. |

---

## 1. Purpose

### 1.1 What sndbx is

sndbx is a **host-side manager for isolated, disposable virtual machines that
agents can drive**. It creates microVMs from local image sources, exposes them
through two control surfaces (an MCP server for programmatic agents and a Web UI
for humans), and keeps their declarative configuration in a single, auditable,
schema-validated place.

The name is a mnemonic for *sandbox box*: a container that is a **box** (full
OS, full root, real filesystem) whose walls are a **sandbox** (hardware-level
virtualization boundary).

### 1.2 The problems it solves

| Problem | How sndbx addresses it |
|---|---|
| An LLM agent needs to run code, install packages, and keep files, without being able to damage the host. | Each sandbox is a Kata Containers microVM (QEMU/VMM-backed), not a shared-kernel container. A `rm -rf /` inside the guest destroys the guest's writable layer only. |
| Several agents must work simultaneously and independently. | Every sandbox is a separate microVM with its own root filesystem, network stack, cgroup budget and published ports. They can share host directories through explicit bind mounts. |
| Work must be able to survive a crash, a restart of the manager, or a reboot — or must not. | Both are expressible. The writable layer of a container persists across `stop`/`start`; `read_only_rootfs` plus explicit mounts gives a hermetic, disposable guest. `recreate` returns a sandbox to a pristine state. |
| Agents need a stable, scriptable control channel. | An MCP server exposes sandbox lifecycle, command execution and file I/O as tools, addressed by `envid` and authenticated by token. |
| Humans need to see and steer the same objects. | The Web UI drives the *same* core library as the MCP server (direct in-process calls, not HTTP self-calls) and adds a console, monitoring and configuration editing. |
| Configuration drifts between "the machine" and "the repo". | All configuration is declarative JSON5 validated by a schema descriptor, split into a root override layer and per-image overlay defaults, with `.env` holding secrets and machine-specific paths. |
| The environment must be reproducible and disposable. | Sandboxes are built from `images/<id>/Dockerfile` sources that live in Git, and can be rebuilt or fast-forwarded from the Web UI. |

### 1.3 Non-goals

These are **not** goals of the project, and their absence is deliberate:

- **Not a general container orchestrator.** There is no scheduler, no replica
  management, no service mesh. Sandboxes are configured statically.
- **Not a multi-tenant SaaS.** There is one host-level installation; the
  multi-user model exists to scope MCP tokens and Web UI logins, not to run
  competing instances of the manager.
- **Not a container registry or image distribution system.** Images are local
  build contexts under `images/`.
- **Not an agent runtime.** sndbx provides the *box* an agent works in; it does
  not run the agent, plan its work, or reason about its output.
- **Not an audit/compliance product.** Request logging exists for
  troubleshooting (`mcp_requests.jsonl`), not for tamper-evident audit.

### 1.4 Ecosystem position

sndbx is one component of a wider family of projects by the same author:

- **aidir** — multi-agent system orchestration. sndbx is designed to be
  driven *by* aidir: aidir calls MCP tools to create and drive sandboxes, and
  the Web UI deliberately mirrors aidir's interaction model.
- **colloc** — externally open systems, where the per-agent isolation of sndbx
  is the mitigation for prompt injection.
- **OpenClaw / ClawVM** — a consumer of sndbx: the ClawVM image runs an
  OpenClaw instance inside a sndbx-managed microVM.

The `envid` concept is the integration seam. An `envid` is a long-lived token
that maps to exactly one sandbox; aidir uses the same `envid` value in its own
resource addressing, so resources created by aidir and the sandbox that hosts
them stay associated.

---

## 2. Architecture

### 2.1 Layered stack

The isolation stack is built bottom-up; each layer is a standard project.

```text
┌──────────────────────────────────────────────────────────────┐
│ Guest OS (Ubuntu / Debian / node:24 / postgres:16 / …)       │  ← full root, package manager, systemd
├──────────────────────────────────────────────────────────────┤
│ Kata Containers runtime-rs shim  (containerd-shim-kata-v2)  │  ← OCI runtime shim, pod/VM semantics
├──────────────────────────────────────────────────────────────┤
│ QEMU (KVM)  —  Firecracker optional alternative VMM           │  ← hardware virtualization boundary
├──────────────────────────────────────────────────────────────┤
│ Host kernel  (KVM, /dev/kvm, cgroup v2, virtiofs, iptables) │
├──────────────────────────────────────────────────────────────┤
│ Docker Engine  —  runtime "kata" registered in daemon.json   │
├──────────────────────────────────────────────────────────────┤
│ sndbx manager process (src/app.py)                          │
│   ├── MCP HTTP server        (aiohttp)      :30081           │
│   ├── Web UI server          (FastAPI)      :30080           │
│   ├── Backup scheduler       (asyncio task)                 │
│   └── Sandbox manager        (docker CLI subprocess calls)   │
└──────────────────────────────────────────────────────────────┘
```

**Why this stack.** Docker is used purely as a *lifecycle and packaging
front-end*: it provides image builds, declarative container spec, port
publishing, bind mounts, resource limits and a stable container naming
convention. It is deliberately *not* the isolation boundary — the boundary is
Kata, which gives each container its own kernel. This buys real multi-tenant
safety (a kernel exploit in one guest does not reach the host) at the cost of
much higher per-VM resource use and slower boot.

**Firecracker** is optional in the current deployment: the Kata 4.1.0 bundle
ships its own `firecracker` binary under `/opt/kata/bin/`, and the QEMU
hypervisor is the default in the shipped Kata configuration. Installing
Firecracker separately (see `README.md`) only matters when Kata is configured to
use it as the VMM backend.

### 2.2 Process topology

sndbx runs as **one OS process** that hosts everything:

```text
src/app.py  (SNDBXApp)
│
├── asyncio loop
│   ├── task "sndbx-start"        → SNDBXApp.start()
│   ├── task "sndbx-stop-wait"    → waits for SIGINT/SIGTERM
│   ├── task "sndbx-backup-scheduler" → BackupManager.run_loop()
│   └── uvicorn server task       → Web UI (FastAPI app)
│
├── MCPServer (aiohttp AppRunner) → own TCP listener on mcp.bind:mcp.port
└── DockerSandboxManager          → stateless helper; shells out to `docker`
```

Why one process:

- **Simplest deployment.** One systemd unit, one venv, one log directory.
- **Shared state without IPC.** The Web UI and the MCP server act on the same
  `DockerSandboxManager` instance and the same in-memory configuration. A
  `run_at_startup` toggle written through the Web UI is visible to the MCP
  server immediately, with no cache invalidation protocol.
- **Trade-off — blast radius.** A crash in any component takes down all of
  them. This is mitigated by the global exception hooks in
  `src/app.py:38-57` (which log rather than silently die) and by systemd's
  `Restart=always`, but it is a real limitation. Splitting into separate
  processes was rejected as unnecessary complexity for a single-host tool.

**Signals.** `SIGINT`/`SIGTERM` set an asyncio stop event; `SNDBX_STOP_REASON`
in the environment overrides the logged reason. Shutdown order is: backup
scheduler → MCP server → Web UI → **stop all running sandboxes**. Stopping the
sandboxes on manager shutdown is deliberate: it makes the host state
deterministic, and container restart is cheap relative to VM boot.

### 2.3 Request flows

**MCP tool call** (the agent path):

```text
client ──HTTP POST /mcp──▶ MCPServer.handle_streamable_http
                              │  parse JSON-RPC (single object or array)
                              │  extract token (body / auth.token / Authorization: Bearer)
                              │  resolve envid against users.items[].envids
                              ▼
                         ToolHandlers.tool_*
                              │  envid → sandbox id (config.envids)
                              ▼
                    DockerSandboxManager.execute_command
                              │  docker exec sndbx-<id> bash -c "<command>"
                              ▼
                         JSON-RPC result
```

**Web UI action** (the human path) — note it does **not** loop back through
MCP:

```text
browser ──fetch /api/sandbox/<id>/action──▶ FastAPI route
                                                │  session cookie check
                                                ▼
                                    DockerSandboxManager.<action>
```

The console tab is the exception: it opens a WebSocket and drives a PTY, not an
MCP tool. The original design intended the console to tunnel through MCP
"for unification"; that was **not implemented**, because a full interactive
PTY cannot be expressed as a single request/response MCP tool call. See
[§7.3](#73-decisions-that-were-not-implemented-the-same-way) for the full
list of SPEC-vs-reality deltas.

**Settings write** (the configuration path):

```text
browser ──POST /api/settings/operations──▶ aiset ASGI app (mounted)
                                             │  revision check
                                             │  descriptor validation
                                             ▼
                                    config.json5 / overlay rewritten atomically
                                             │
                                             ▼
                                  SettingsRuntime._after_save
                                             │  logging → live reconfigure
                                             │  run_at_startup → in-memory update
```

### 2.4 Isolation boundaries and what crosses them

| Boundary | Direction | Mechanism |
|---|---|---|
| Host → guest | writes | `docker cp`, bind mounts (host path → guest path), `docker exec` |
| Guest → host | writes | bind mounts only (guest writes land in the host directory) |
| Host → guest | config | environment variables at `docker run`, OCI annotations, `authorized_keys` written by the image hook |
| Guest → host | network | NAT through the host; published ports via `port_bindings` |
| Guest → host | none | no guest agent, no guest-initiated RPC into the manager |

The manager never reads guest state except through `docker exec` of a **fixed,
read-only** probe script (monitoring) and through `docker inspect`. The
console uses `docker exec -it` and is the only interactive channel.

---

## 3. Domain model

Five objects carry all the meaning in the system.

### 3.1 Sandbox

A **sandbox** is a named, configured microVM. It is identified by a string id
that must match `^[A-Za-z0-9][A-Za-z0-9_.-]*$` (the descriptor's
`key_pattern`). The id appears in three places and they must agree:

1. `sandboxes.items.<id>` in the root `config.json5` — the registry entry.
2. `images/<dir>/sndbx.overlay.json5` — the overlay that declares `<id>`.
3. The Docker container name `sndbx-<id>`.

A sandbox is **not** the same thing as a Docker container: the container is the
runtime artifact, the sandbox is the declarative definition. `recreate` throws
away the container and rebuilds it from the definition; `stop`/`start` keeps the
container and its writable layer.

### 3.2 Image

An **image** is a local build context under `images/<image-id>/` containing a
`Dockerfile`, an optional `app.py` lifecycle hook and an optional
`sndbx.overlay.json5`. The image id and the Docker tag are related but not
identical — `local_image_id` exists precisely to decouple them (for example
directory `denis_obsidian_assistant` → tag `denis-obsidian-assistant:latest`).

Full details: [`docs/IMAGES.md`](docs/IMAGES.md).

### 3.3 Overlay and root configuration

sndbx uses a **three-layer configuration model** with a strict precedence:

```text
highest   .env                          secrets, machine-specific paths
          ────────────────────────────────────────────────────
          config.json5  (root)          registry + global settings + overrides
          ────────────────────────────────────────────────────
lowest    images/<id>/sndbx.overlay.json5   per-image defaults
```

The root wins **every** conflict. An overlay only supplies keys the root does
not define. Removing a key from the root is how you "let the overlay speak".
Arrays are replaced wholesale by the root — an empty root array still wins.

### 3.4 envid

An **envid** is a long-lived opaque token that names *an environment*, i.e. a
sandbox, for clients that are not allowed to know sandbox ids.

```json5
envids: {
  "clawvm-env-token": {
    sandbox: "clawvm",
    description: "ClawVM environment"
  }
}
```

The same value is listed in `users.items[].envids` for every user allowed to
use it. An MCP request carries an `envid`; the server resolves it to a sandbox
**only if** the authenticated token's owner has that envid in their list. This
is the entire authorization model today.

### 3.5 User, token, session

Three distinct credentials exist and are often confused:

| Credential | Where it lives | Purpose | Lifetime |
|---|---|---|---|
| MCP token | `mcp.auth.tokens[]` in config; `users.items[].token` | Authenticates MCP calls and carries the envid allow-list. | Static, until config edit + restart. |
| Web UI password | `webui.auth.users[].password` | Authenticates the Web UI login form. | Static. |
| Web UI session | `sessions/<sha256(token)>.json` | Cookie-borne proof of a past login. | `webui.auth.session_ttl` seconds (default 24 h). |

`webui.auth.users[].permissions` currently accepts `"all"` or a list. Only
`"all"` is meaningful: it maps to *all* aiset permissions in the settings
editor. Any other value yields the read-only viewer principal
(`settings_read_source`, `settings_read_effective`). The old SPEC's plan of a
per-tool ACL matrix was **not** implemented — see
[§7.3](#73-decisions-that-were-not-implemented-the-same-way).

---

## 4. Component catalogue

Logical decomposition of `src/`. Each entry lists the responsibility, the
public surface, and the failure modes an operator will actually observe.

### 4.1 `src/app.py` — application bootstrap

**Responsibility.** Load configuration, construct the sandbox manager, build
missing images, start auto-start sandboxes, start the MCP and Web UI servers,
start the backup scheduler, and shut everything down cleanly on a signal.

**Public surface.** `SNDBXApp`, `main()`.

**Notable behaviour.**

- Startup is strictly ordered: config → sandbox manager → image builds →
  auto-start sandboxes → MCP server → Web UI server → backup scheduler. The
  MCP and Web UI listeners only come up after their dependencies are ready.
- `_build_startup_images()` builds every configured image that is not yet in
  the Docker image store. This can take minutes on a cold host and is logged
  per image.
- `_start_startup_sandboxes()` starts sandboxes whose `run_at_startup` is
  true. It tries `start` first and falls back to `create`.
- `_stop_all_sandboxes()` stops every sandbox that is *configured or
  discovered*, so a container left over from a removed config entry is still
  cleaned up.
- Global `sys.excepthook`, `threading.excepthook` and the asyncio exception
  handler route unhandled errors into the `core` log instead of losing them.

**Failure modes.** An invalid root or overlay configuration aborts startup
with `ConfigError` — this is deliberate: a manager running against a config it
cannot validate is worse than a manager that refuses to start.

### 4.2 `src/settings/` — configuration subsystem

**Responsibility.** Parse, validate, merge and persist the three-layer
configuration, and expose the merged result to the rest of the application.

**Modules.**

| Module | Responsibility |
|---|---|
| `settings/aiset_path.py` | Makes the pinned `aiset` submodule importable; raises a clear error if the submodule is missing. |
| `settings/runtime.py` | `SettingsRuntime`: registers documents with aiset, validates them, discovers overlays, merges, preflights, and applies live changes after a write. |
| `settings/overlays.py` | `discover_overlays()` and `merge_runtime()`: the overlay discovery and merge rules. |
| `settings/principals.py` | Maps a Web UI session to an aiset `Principal`. |
| `config.py` | Thin compatibility facade: `load_config(root_dir)` returns `{env, config, settings}`. |

**Public surface.**

```python
runtime = get_runtime(root_dir)          # cached per resolved root
runtime.config                           # merged effective configuration
runtime.env                              # parsed .env
runtime.document_catalog()               # [{id, label, sandboxes}] for the Web UI
runtime.document_for_sandbox(sandbox_id) # overlay document id, or None
runtime.apply_auto_start(sandbox_id, enabled, principal)
```

**Merge semantics.** Implemented by `merge_runtime()` in
`settings/overlays.py:127`:

- Root scalars win; overlay scalars fill gaps.
- Objects deep-merge (a root object and an overlay object are combined).
- Arrays are replaced wholesale by the root.
- `envids` merges with the same "root wins" rule.
- The root's `run_at_startup` is authoritative and is never taken from an
  overlay (the migration tool strips it from overlays when generating them).

**Overlay discovery rules** (`settings/overlays.py:77`):

- Only `images/*/sndbx.overlay.json5` is considered.
- Allowed top-level sections: `version`, `sandboxes`, `envids`. Anything else
  is an error.
- An overlay must declare at least one sandbox.
- Every declared sandbox id must be registered in the root config.
- A sandbox may be covered by exactly one overlay.
- The overlay's directory must match the directory the sandbox's image
  resolves to (`local_image_id` → `image` tag → sandbox id → demo aliases).

**Failure modes.** `ConfigError` for a missing file, an invalid document, an
unknown key, a bad overlay layout, or an empty `root` value. The service does
not start in any of these cases.

**Design rationale.** The legacy implementation had a hand-written JSON5
loader, a separate overlay convention, and no schema validation. Replacing it
with `aiset` (a shared library used by aidir and others) bought: source-
preserving edits that keep comments, schema validation with typed ranges,
revision-checked atomic writes with automatic backups, and a reusable browser
tree editor — at the cost of a pinned submodule dependency and a migration
step. The design is recorded in `proposal_aiset_integration.md`; the actual
delivery, deviations and verification are in `execution_aiset_integration.md`.

### 4.3 `src/sandbox.py` — sandbox manager

**Responsibility.** Everything that touches Docker: build images, create, start,
stop, restart and recreate sandboxes, execute commands, publish ports, run the
image hook, evaluate the runtime contract, and probe guest state.

**Public surface.**

```python
DockerSandboxManager(config)
  .create_sandbox(id) / .start_sandbox(id) / .stop_sandbox(id)
  .restart_sandbox(id) / .recreate_sandbox(id)
  .execute_command(id, command) -> (ok, output)
  .get_status(id) -> SandboxStatus(id, running, container_id, ip, error)
  .list_sandboxes() -> (ok, [ {sandbox_id, container_name, image, status, ports} ])
  .list_local_images() / .build_configured_image() / .update_configured_image()
  .get_container_stats(ids) / .probe_guest_metrics(id) / .get_container_disk_size(id)
  .exec_ssh_setup(id, authorized_keys)
```

**Container naming.** Every container is named `sndbx-<sandbox_id>`. This is
the anchor for the whole system: the MCP console, the monitoring collector and
the image-update guard all parse the id back out of the container name.

**Creation sequence** (`create_sandbox`, `src/sandbox.py:949`):

1. Validate the VirtioFS profile (`posix-acl-compat` or none).
2. Run the optional privileged host preparation script
   (`host_prepare_script`) — must live inside the image directory.
3. Ensure the Docker image exists; build it from `images/<id>` if missing.
4. Preflight shared mounts: existence, type match, optional creation,
   `host_mode`, `bind_propagation`, and (for the ACL profile) nested-mount
   rejection.
5. Build the Docker argument vector: runtime, memory, CPUs, tmpfs, shm size,
   Kata annotations, VirtioFS profile, bind mounts, managed volumes, published
   ports, host aliases, runtime environment, read-only rootfs.
6. `docker run --detach`.
7. On failure, retry without `--storage-opt` if the storage driver rejects it,
   and remove a stale container on a name conflict.
8. Configure the apt mirror (unless `configure_apt_mirror: false`).
9. Run the image hook (`on_system_start`).
10. Validate the runtime contract (capability hook + readiness probes).

**Notable rules.**

- **Recreate is guarded.** `recreate_sandbox` runs `docker diff` and refuses
  to destroy a container whose root filesystem has been modified (ignoring
  `/etc`, `/etc/hosts`, `/etc/hostname`, `/etc/resolv.conf`, which Docker
  always rewrites). The rationale is data safety: the writable layer is not
  backed up automatically, so a silent recreate could destroy hours of work.
  Persistent state must live in a mount or a volume. The guard does not apply
  to a sandbox with `persistent: false` — its layer is disposable by
  definition.
- **`persistent: false` makes a sandbox disposable.** `stop_sandbox` stops the
  container gracefully and then removes it, so the next start creates a fresh
  sandbox from the image. The default (`true`) keeps the writable layer.
  Every currently configured sandbox uses `true`, so this changes no existing
  deployment.
- **Image update is guarded.** `update_configured_image` requires a clean
  Git worktree, an upstream branch, a fast-forward-only merge, and refuses any
  incoming change under `data/`. The persistent data directory is protected
  from accidental upstream overwrite.
- **The apt mirror.** By default sndbx rewrites `/etc/apt/sources.list` inside
  the guest to `mirrors.aliyun.com`. This was a host-specific performance fix
  (see `README.md` troubleshooting) and is a **hard-coded constant**
  (`DockerSandboxManager.APT_MIRROR`), not a configuration key. Images that
  are not Ubuntu Jammy must set `configure_apt_mirror: false`.
- **apt lists are tmpfs.** `/var/lib/apt/lists` and `/var/cache/apt` are
  mounted as tmpfs because the guest root is virtiofs-backed and writing
  hundreds of index files over virtiofs is very slow.
- **The Kata runtime path is discovered, not assumed.** `src/kata.py` reads
  `runtimes.kata` from `/etc/docker/daemon.json`, so an installation that
  used a non-default `--kata_path` is handled correctly. The historical
  `/opt/kata` paths remain the fallback.

### 4.4 `src/mcp_server.py` + `src/tools.py` + `src/mcp_proxy.py` — MCP server

**Responsibility.** Accept MCP requests over HTTP, authenticate them, resolve
the target sandbox, dispatch to a tool handler, and format the response. The
proxy module forwards a call to an MCP server running inside a guest.

**Public surface.** `MCPServer(host, port, config)`, `register_tool(name,
handler, description, input_schema)`, `start()`, `stop()`.

**Tool handler contract.**

```python
async def handler(params: dict, envid: str, token: str) -> dict
```

The handler receives the *resolved* envid (already validated against the
token owner's allow-list), not the raw request value.

**Tool catalogue** (all defined in one place, `src/tools.py:17`):

| Tool | Purpose |
|---|---|
| `execute_command` | Run a shell command in the sandbox. |
| `read_file` | Read a text file with optional line offset/limit. |
| `write_file` | Write a text file, creating parent directories. |
| `patch_file` | Apply a SEARCH/REPLACE block patch. |
| `sandbox_status` | Runtime status of the resolved sandbox. |
| `sandbox_start` | Start (creating if needed) the resolved sandbox. |
| `sandbox_stop` | Stop the resolved sandbox. |
| `mcp_proxy_call` | Forwards a JSON-RPC call to a VM-side MCP backend over TCP/JSONL. |

**Authentication and routing.**

- Token sources, in order: `body.token`, `body.auth.token`,
  `Authorization: Bearer <token>`.
- A token is valid if it appears in `mcp.auth.tokens[]`.
- `resolve_envid(token, envid)` returns the envid if the token's owner lists
  it, else the owner's *first* envid, else `None`. A request that resolves to
  no envid is rejected with `"No access to requested envid or sandbox"`.
- The tool handler maps envid → sandbox id via `config.envids[envid].sandbox`.

**Response format.** Handler output is wrapped into the MCP tool-result shape:
`{"content": [{"type": "text", "text": ...}], "structuredContent": <raw>}`.
A handler that already returns a `content` list is passed through unchanged.

**File tools are shell-free.** `read_file`, `write_file` and `patch_file` do
not build a shell command. They call
`DockerSandboxManager.exec_argv(sandbox_id, argv, stdin_data)` which runs
`docker exec -i` with an argument vector. Paths are argv entries, and file
content travels over stdin, so neither a path nor a file body can be
interpreted by a shell. `patch_file` reads the file through `cat`, applies the
replacement on the host and writes the result back, so the guest needs no
interpreter — only `sh` and the coreutils it already has. `execute_command`
still uses `bash -c`, because a shell command is exactly what it promises.

**The VM-side proxy.** `src/mcp_proxy.py` implements the client for MCP
backends published by a sandbox. The transport is TCP with newline-delimited
JSON and a mandatory handshake on every connection: `initialize` →
`notifications/initialized` → the real request. `mcp_proxy_call` selects the
binding by `backend_id`, enforces the binding's `tools` allow-list, forwards
the caller's token when `auth_mode` is `token_passthrough`, and resolves the
endpoint from the sandbox's `port_bindings` (falling back to the container IP
and the guest port).

**Protocol surface.** `POST /`, `POST /mcp`, `POST /mcp/v1` (streamable HTTP);
`GET /sse` + `POST /messages` (legacy). Full reference:
[`docs/INTERFACES.md`](docs/INTERFACES.md).

### 4.5 `src/webui_server.py` — Web UI backend

**Responsibility.** Serve the single-page frontend, authenticate users, expose
the dashboard, drive sandbox actions, bridge a WebSocket console, mount the
settings editor, and manage the SSH port pool.

**Public surface.** `WebUIServer(root_dir, config, sandbox_manager, settings)`.

**Internal classes.**

| Class | Responsibility |
|---|---|
| `SessionStore` | File-backed sessions in `sessions/<sha256(token)>.json`; TTL enforcement and periodic cleanup. |
| `SSHManager` | Reserves a host port from `ssh.port_range`, starts a `socat` forwarder to the container IP, persists allocations in `data/ssh_allocations.json`, and reaps dead forwarders at startup. |
| `MCPClient` | Minimal aiohttp JSON-RPC client used by the "MCP test" tab to call the MCP server over HTTP. |

**Endpoint groups.** Auth (`/api/auth/*`), status and health (`/api/status`),
monitoring (`/api/monitor`), images (`/api/images`, `/api/image/<ref>/action`),
sandboxes (`/api/sandbox/<id>/action`), service (`/api/service/restart`,
`/api/system-log`, `/api/runtime/kata/repair`), MCP passthrough
(`/api/mcp/*`), settings (`/api/settings/*`), and the console WebSocket
(`/ws/console/<id>`). Full table in
[`docs/INTERFACES.md`](docs/INTERFACES.md).

**Notable behaviour.**

- **Sessions are files, not tokens.** The session token is never stored in
  cleartext on disk; the filename is the SHA-256 digest. `HttpOnly`,
  `SameSite=Strict`, `Secure` when the request is HTTPS.
- **The service restart is a `SIGKILL`.** `POST /api/service/restart` does
  `systemctl --user kill --kill-whom=main --signal=SIGKILL <unit>` (falling
  back to `os.kill(SIGKILL)`). The rationale: the normal shutdown path stops
  every sandbox, which would cause unnecessary downtime. Killing the main
  process lets systemd restart it while the containers keep running. The
  `_service_state()` helper reports `"restarting"` for 120 s so the UI can show
  a banner instead of logging the user out.
- **Image builds run in background threads.** `build`, `rebuild`, `update` and
  `full_update` return immediately; progress is visible through the images
  table's build-status column. One build per image at a time.
- **The console is a PTY bridge.** The backend opens a host PTY, runs
  `docker exec -it sndbx-<id> bash -lc 'exec bash -i'`, and pumps bytes over a
  WebSocket. The host PTY master is put into raw mode so that only the
  container's line discipline handles echo and editing — otherwise every
  character would be echoed twice. Resize messages are converted into a
  `TIOCSWINSZ` ioctl plus a `SIGWINCH` to the container process.
- **The console requires full permissions.** A session whose `permissions` do
  not contain `"all"` is refused with close code `4001`. A console gives full
  root access inside the guest, so it is reserved for administrators; a
  read-only viewer principal can use the dashboard and the settings editor
  but not the shell.
- **The settings editor is mounted, not reimplemented.** `aiset.asgi.create_asgi_app`
  is mounted at `/api/settings`, and the frontend loads `aiset.js` /
  `aiset-tree.js` from it.
- **The console works offline.** The frontend loads xterm.js from a CDN and
  falls back to the copies in `src/webui/frontend/vendor/` when the CDN is
  unreachable.

### 4.6 `src/monitoring.py` — runtime monitoring

**Responsibility.** Collect read-only per-VM metrics (memory, CPU, disk,
network, processes, host pressure), keep a short in-memory history, and
evaluate health thresholds.

**Public surface.** `SandboxMonitor(sandbox_manager, config).collect(ids)`.

**Design.**

- **5 s** primary cadence via `docker stats --no-stream`.
- **15 s** deep guest probe via a fixed read-only `docker exec` script that
  reads `/proc/meminfo`, `/sys/fs/cgroup/memory.events`,
  `/sys/fs/cgroup/memory.swap.current`, PSI files, `/proc/uptime` and `ps`.
- **60 s** disk refresh (host free space via `shutil.disk_usage` plus Docker
  `SizeRw`).
- **2 s** response cache, so several browser tabs share one Docker round trip.
- **120 samples** of history (~10 minutes) held in memory only.
- 5-minute guest-pressure staleness window; 60 s failure cooldown.

**Thresholds.** Memory available < 20 % / < 10 %, memory used ≥ 85 % / ≥ 95 %,
swap ≥ 10 % / ≥ 25 %, any OOM kill, CPU allocated ≥ 80 % / ≥ 95 % (with a
3-sample streak), CPU PSI `avg60` ≥ 20, host memory available < 15 % / < 8 %,
host swap used ≥ 25 % / ≥ 50 %, host disk free < 15 % / < 5 %. Warnings are
debounced (memory needs 2 consecutive samples, CPU needs 3) to avoid
flapping.

**Explicit non-goals.** No background sampler, no persistence, no log entries,
no browser notifications. Metrics exist only while a Web UI user has switched
monitoring on for a VM.

**Caveat.** The per-VM disk percentage is not reliable under Kata/VirtioFS:
the guest's `df` reports the host filesystem, and `disk_max` is not enforced
by the storage driver. The UI therefore shows host free space and the Docker
writable-layer size and labels them as best effort.

Design record: `proposal_monitoring.md`.

### 4.7 `src/backup.py` — per-VM backup scheduler

**Responsibility.** Schedule and execute incremental file-level backups of the
persistent `images/<local_image_id>/` directory of each opted-in sandbox.

**Public surface.**

```python
BackupManager(config, root_dir, sandbox_manager, tar_binary, backup_helper_path)
  .reconfigure()                 # re-read the config and rebuild the schedule
  .list_enabled_jobs()
  .run_backup(sandbox_id) -> BackupResult
  .run_loop()                    # the scheduler loop
```

**Algorithm.** GNU `tar --listed-incremental` with a `.snar` snapshot file.
The first run and every `full_every`-th interval produce a *full* archive;
otherwise an *incremental* archive is produced against the snapshot. Archives
are named `<local_image_id>-<UTC timestamp>.<ms>Z-<full|incremental>.tar.gz`
and live in `backup/<local_image_id>/`. A `<local_image_id>.chain.json` file
records the last full and last incremental for the restore chain.

**Root helper.** Files created inside the VM by a foreign host UID/GID cannot
be read by the service user. `install_backup_acl_helper.sh` installs a
root-owned helper at `/usr/local/libexec/sndbx-backup-helper` plus a narrow
sudoers rule; the scheduler then invokes it with `sudo -n`. The helper:

- reads only the configured image directory,
- writes only the configured backup directory and a root-only state directory
  (`/var/lib/sndbx-backup/`),
- takes a per-image `flock` so two schedulers cannot race,
- never modifies ACLs or permissions inside the image,
- uses `--ignore-failed-read` so a file changing under a running VM does not
  discard the whole archive.

**Limitations.** The backup is file-level, not application-consistent: the VM
is not stopped, so a database may be captured mid-write. External bind mounts
outside `images/<id>/` are **not** included. Losing one archive breaks
restoration of every later archive in that chain.

Design record: `proposal_backup.md`. Operations: `docs/HOST_OPERATIONS.md`.

### 4.8 `src/logging_utils.py` — centralized logging

**Responsibility.** Route every log record to a per-tag file, to `all.log`, and
to stdout/stderr.

**Model.**

- Every subsystem gets a **tag**: `core`, `webui`, `mcp`, `sandbox_manager`,
  `ssh`, `backup`, `monitoring`, `access`.
- Each tag has its own file `logs/<tag>.log`; everything also goes to
  `logs/all.log`.
- The line format is
  `2026-05-15T02:53:50.345+08:00 [INFO] [tag] message` — ISO-8601 with
  milliseconds, in the configured timezone (`local`, `UTC` or an IANA name).
- `logging.level` is the global threshold; `logging.levels.<tag>` overrides it
  per tag.
- `get_logger(tag)` returns a `TagLogger` adapter with the usual
  `debug/info/warning/error/exception` methods.
- A `_StdlibBridgeHandler` on the root logger converts third-party stdlib
  records (uvicorn, aiohttp) into the same format, so everything lands in the
  same files.

**Log wiping.** `logging.wipe_period` (seconds, `0` disables) schedules a wipe
run; `logging.wipe_max_age` (defaults to `wipe_period`) is how much history is
kept. Each run drops the entries older than the cutoff from every
`logs/*.log` file and from `logs/mcp_requests.jsonl`. The files are rewritten
**in place** so open handles stay valid. The scheduler is an asyncio task in
`src/app.py` that re-reads the period on every tick, so a live configuration
change takes effect without a restart.

**MCP request log.** When `logging.log_mcp_requests` is true, every JSON-RPC
request and response is appended to `logs/mcp_requests.jsonl` as
`{"type": "request"|"response", "ts": ..., "data": ...}`.
`logging.mcp_request_log_max_mb` (default 64, `0` disables) caps the file size:
when the cap is reached the log is rotated to `mcp_requests.jsonl.1` and a
fresh one is started.

---

## 5. Configuration model

### 5.1 The three locations

| Location | Contents | Edited by |
|---|---|---|
| `.env` | `KEY=VALUE`. Secrets (passwords, tokens), machine-specific paths, ports, per-image environment namespaces. | Humans, `install.sh`. |
| `config.json5` (root) | Sandbox registry, global settings (logging, webui, mcp, ssh), and the **override layer**. | Web UI Settings tab, humans. |
| `images/<id>/sndbx.overlay.json5` | Per-image sandbox defaults. | Web UI Settings tab, humans, the migration tool. |

`.env` is excluded from Git. `.env.example` lists every variable with dummy
values and is the reference for what can be overridden.

### 5.2 Schema

`config.descriptor.json5` describes the shape of the root document **and** of
every overlay. `config.types.json5` holds the reusable type templates that the
descriptor references.

Descriptor node attributes that matter:

| Attribute | Meaning |
|---|---|
| `type` | Value type or a typedef name from `config.types.json5`. |
| `env: {allowed: true}` | The field may hold a `${VAR}` / `${VAR:-default}` placeholder. |
| `secret: true` | The value is redacted in the settings tree and in API responses. |
| `read_only: true` | Derived; the user must not edit it. |
| `apply: "live"` | A write takes effect without a restart (currently `logging` and `run_at_startup`). |
| `apply: "restart"` | Default; the UI reports `restart_required`. |
| `key_pattern` | Regex constraint on dynamic object keys. |

**Unknown keys are errors.** Adding a configuration key requires updating the
descriptor first — `tests/test_config_descriptor.py` loads every shipped
document through the descriptor and fails on any unexpected key.

### 5.3 Placeholders and resolution order

A value may be a literal or a placeholder:

```json5
memory: "${LANGVM_MEMORY:-8G}"    // with a default
root:   "${SNDBX_ROOT}"            // without a default
```

Resolution order: explicit overrides → process environment → `.env` → the
default in the placeholder. The placeholder is **stored, not resolved**:
the editor shows and preserves `${...}` text, and resolution happens at read
time.

### 5.4 Merge rules

Applied by `merge_runtime()`:

| Situation | Result |
|---|---|
| Key in root only | Root value. |
| Key in overlay only | Overlay value is added. |
| Key in both, scalar | Root value. |
| Key in both, object | Deep merge; root members win individually. |
| Key in both, array | **Root array replaces the overlay array entirely.** |
| `envids` in both | Merged per key; root entries win. |

To let an overlay provide a value, the key must be **absent** from the root.
An empty root array or a root `false` still wins.

### 5.5 Sandbox configuration reference

Per-sandbox keys (the `sandbox` typedef). All are optional; the overlay
usually supplies most of them.

| Key | Type | Meaning |
|---|---|---|
| `title` | string | Human-readable name shown in the UI. |
| `image` | string | Docker image reference (`tag` or `tag:version`). |
| `local_image_id` | string | Directory under `images/` that owns this sandbox. Defaults to the image tag prefix. |
| `memory` | string | Guest memory limit, Docker syntax (`2G`, `8G`). |
| `cpus` | integer | Number of vCPUs (≥ 1). |
| `disk_max` | string | `--storage-opt size=` for the writable layer. Silently dropped if the storage driver rejects it. |
| `network_traffic_max` | string | **Advisory only.** Configuration placeholder; no accounting, no enforcement. |
| `shm_size` | string | `--shm-size` for the container. |
| `stop_grace_seconds` | integer | 1–3600; grace period for `docker stop`. Default 30. |
| `persistent` | boolean | `true` keeps the writable layer between runs. `false` removes the container on stop, so the next start is a fresh sandbox. |
| `run_at_startup` | boolean | Start the sandbox when the manager starts. **Owned by the root config.** |
| `read_only_rootfs` | boolean | `--read-only` root filesystem. |
| `configure_apt_mirror` | boolean | Rewrite the guest apt sources to the Aliyun mirror. Default `true`. |
| `ssh_setup_via_image_hook` | boolean | The image hook configures sshd and `authorized_keys`; sndbx does not. |
| `virtiofs_profile` | string | `posix-acl-compat` enables the ACL annotation and rejects nested mounts. |
| `ssh_user` | string | Login name shown in the "connect with ssh" hint. |
| `ssh_keys` | string[] | Public keys written to `authorized_keys` by the image hook. |
| `tmpfs` | string[] | Absolute paths mounted as tmpfs. |
| `shared_directories` | object[] | Bind mounts; see below. |
| `managed_volumes` | object[] | Docker-managed volumes (`name`, `guest_path`). |
| `port_bindings` | object[] | Published ports (`bind_host`, `vm_port`, `publish_port`). |
| `mcp_bindings` | object[] | VM-side MCP backends (`backend_id`, `transport`, `vm_port`, `tools`, `auth_mode`, timeouts). |
| `runtime_contract` | object | Runtime environment, companion services, capability hook and readiness probes. |
| `backup` | object | `{enabled, interval_hours, full_every}`. |

**`shared_directories` entry.**

| Key | Type | Meaning |
|---|---|---|
| `host_path` | string | Absolute, or relative to `root`. |
| `guest_path` | string | Absolute path inside the guest. |
| `permission` | `ro`\|`rw` | Mount mode. |
| `source_type` / `mount_type` | `directory`\|`file` | Type of the source. Defaults to `directory`. |
| `required` | boolean | If `true` (default), a missing source aborts sandbox creation. |
| `create_if_missing` | boolean | Create the host directory when missing. Only valid for a writable directory. |
| `host_mode` | octal string | `chmod` applied to the host path before mounting. |
| `bind_propagation` | string | `private`/`rprivate`/`slave`/`rslave`/`shared`/`rshared`. Directories only. |

### 5.6 Top-level configuration reference

| Section | Keys |
|---|---|
| root | `version` (const `"1.0"`), `title`, `instance`, `root` (read-only, from `SNDBX_ROOT`). |
| `logging` | `level`, `timezone`, `levels` (per-tag), `log_mcp_requests`, `wipe_period`, `wipe_max_age`, `mcp_request_log_max_mb`. |
| `webui` | `port`, `bind`, `auth{mode, session_ttl, users[{login, password, permissions[]}]}`. |
| `mcp` | `port`, `bind`, `auth{mode, tokens[]}`, `items[]` (reserved). |
| `sandboxes` | `items` — map of sandbox id → sandbox definition. |
| `envids` | Map of envid → `{sandbox, description}`. Marked secret. |
| `users` | `items[]` — `{login, password, token, envids[]}`. |
| `ssh` | `port_range` (exactly two integers), `session_ttl` (seconds; `0` keeps the relay until it is closed manually). |

### 5.7 Environment variables

`.env` is read by `settings/runtime.py:36` (`read_env_file`) and is *not*
injected into the systemd unit — the manager parses it itself.

| Variable | Default | Purpose |
|---|---|---|
| `SNDBX_ROOT` | — (required) | Absolute checkout path; becomes `config.root`. |
| `SNDBX_INSTANCE` | `sndbx-dev` | Instance label. |
| `LOG_LEVEL` | `info` | Global log level. |
| `LOG_TIMEZONE` | `local` | `local`, `UTC` or an IANA zone. |
| `LOG_WIPE_PERIOD` | `0` | Declared but not implemented. |
| `WEBUI_PORT` / `WEBUI_HOST` | `30080` / `0.0.0.0` | Web UI listener. |
| `MCP_PORT` / `MCP_HOST` | `30081` / `0.0.0.0` | MCP listener. |
| `MCP_TOKENS` | `test-token-123456789` | Comma-separated accepted tokens. |
| `SSH_PORT_MIN` / `SSH_PORT_MAX` | `30200` / `30210` | SSH port pool. |
| `ROOT_USER` / `ROOT_PASSWORD` | `admin` / `admin` | Web UI administrator. |
| `MY_SSH_PUBLIC_KEY` | — | Public key injected into local-image sandboxes. |
| `VM_IMAGE`, `VM_MEMORY`, `VM_CPUS`, `VM_DISK_MAX`, `VM_NETWORK_TRAFFIC_MAX`, `VM_SHARED_DIR` | see `.env.example` | Legacy defaults for the demo sandbox. |
| `MCP_TOOLBOX_BIND_HOST`, `MCP_TOOLBOX_FS_PORT`, `MCP_TOOLBOX_BASH_PORT`, `MCP_TOOLBOX_GIT_PORT` | `127.0.0.1`, `39011`–`39013` | MCP toolbox published ports. |

Each image additionally owns a namespace (`LANGVM_*`, `CLAWVM_*`,
`OBSIDIAN_ASSISTANT_*`, `PAPERCLIP_*`, `OPEN_WEBUI_*`, `AIBUSINESS_*`) used by
its overlay.

### 5.8 Write path

The Settings tab never writes a file directly. Writes go through aiset:

1. The frontend stages an edit and posts an operation to
   `/api/settings/operations/validate`, then `/api/settings/operations`.
2. The service checks the expected document revision; a mismatch is `409`,
   a missing revision is `428`.
3. The descriptor validates the new value (type, range, enum, `env.allowed`).
4. The file is rewritten atomically with a source-preserving edit (comments
   and untouched bytes survive) and a `.back` copy is kept.
5. `SettingsRuntime._after_save` applies the live parts: a changed
   `logging` section reconfigures logging immediately; a changed
   `run_at_startup` updates the in-memory sandbox config.

---

## 6. Filesystem layout

### 6.1 Repository root

```text
sndbx/
├── SPEC_NEW.md              ← this document (entry point)
├── README.md                ← installation and operations guide
├── DOCUMENTS.MD             ← document index
├── AGENTS.md                ← agent workflow rules (read-only)
├── TEST.md                  ← test protocols
├── SPEC.md                  ← historical specification (superseded)
├── SPEC_.md                 ← required corrections to SPEC.md
├── GETTING_STARTED.md       ← first-run walkthrough
├── MCP_USAGE.md             ← MCP usage examples
├── docs/                    ← topic documents for this specification
├── src/                     ← the manager (Python package)
│   ├── app.py               ← entry point and orchestration
│   ├── config.py            ← configuration facade
│   ├── logging_utils.py     ← centralized logging
│   ├── sandbox.py           ← Docker/Kata sandbox manager
│   ├── mcp_server.py        ← MCP HTTP/SSE server
│   ├── tools.py             ← MCP tool catalogue and handlers
│   ├── mcp_proxy.py         ← client for VM-side MCP backends
│   ├── kata.py              ← Kata runtime path discovery
│   ├── webui_server.py      ← FastAPI Web UI backend
│   ├── monitoring.py        ← read-only runtime monitoring
│   ├── backup.py            ← per-VM backup scheduler
│   ├── settings/            ← aiset-backed configuration subsystem
│   │   ├── runtime.py       ← SettingsRuntime
│   │   ├── overlays.py      ← overlay discovery and merge
│   │   ├── principals.py    ← session → aiset principal
│   │   └── aiset_path.py    ← submodule import bootstrap
│   └── webui/frontend/      ← single-page frontend (vanilla JS)
│       ├── index.html
│       ├── app.js
│       ├── settings.css
│       └── vendor/           ← vendored xterm.js fallback for offline use
├── images/<image-id>/       ← image build contexts (see docs/IMAGES.md)
├── aiset/                   ← pinned submodule (settings library)
├── scripts/
│   └── migrate_sandbox_overlays.py   ← one-time overlay migration
├── tests/                   ← unittest suite
├── config.json5             ← root configuration (registry + overrides)
├── config.descriptor.json5  ← schema for root and overlays
├── config.types.json5        ← schema type templates
├── .env / .env.example      ← environment variables
├── requirements.txt         ← Python dependencies
├── install.sh               ← application installer
├── install_prerequisites.sh ← host bootstrap (Docker, Kata)
├── uninstall.sh             ← service and data removal
├── install_kata_acl.sh      ← parallel kata-acl runtime installer
├── uninstall_kata_acl.sh    ← its remover
├── migrate_to_kata_4_1.sh   ← promote kata-acl to the sole runtime
├── install_backup_acl_helper.sh      ← root backup helper installer
├── prepare_backup_permissions.sh     ← the root backup helper itself
├── run_kata_*_canary.sh     ← ACL propagation canaries
├── data/                    ← mutable host state (git-ignored)
├── logs/                    ← log files (git-ignored)
├── sessions/                ← Web UI sessions (git-ignored)
├── shared/                  ← host directories bind-mounted into VMs
├── backup/                  ← per-image backup archives (git-ignored)
└── venv/                    ← Python virtualenv (git-ignored)
```

### 6.2 Runtime directories

| Path | Owner | Contents | Written by |
|---|---|---|---|
| `data/` | manager | `ssh_allocations.json` (port reservations + socat PIDs), `sandbox_startup_overrides.json.migrated` (retired legacy file). | `SSHManager`, the aiset migration. |
| `logs/` | manager | `all.log`, `<tag>.log`, `mcp_requests.jsonl`. | `logging_utils`, `MCPServer`. |
| `sessions/` | manager | `<sha256(token)>.json` session records. | `SessionStore`. |
| `shared/` | operator | Directories bind-mounted into guests. | The manager creates missing ones; the guests write content. |
| `backup/` | manager + helper | `<image-id>/<archives>` and `<image-id>.chain.json`. | `BackupManager`, the root helper. |
| `venv/` | installer | Python virtualenv. | `install.sh`. |

All are excluded by `.gitignore`.

### 6.3 Image directories

```text
images/<image-id>/
├── Dockerfile            ← build recipe (required)
├── app.py                ← lifecycle hook, copied to /opt/sndbx-image/app.py (optional)
├── sndbx.overlay.json5   ← sandbox defaults for this image (optional but expected)
├── data/                 ← persistent guest home, bind-mounted (optional)
├── .env                  ← guest secrets, bind-mounted read-only (optional)
├── README.md / DOCUMENTS.md / TEST.md / commands.md   ← per-image documentation
└── docker/               ← entrypoint helpers
```

Only `images/default_sandbox_1/` and `images/mcp_toolbox_1/` are tracked by
the root repository; the other six are **independent Git repositories** with
their own remotes, and the root `.gitignore` excludes them. See
[`docs/IMAGES.md`](docs/IMAGES.md) for the full catalogue and the rules for
adding an image.

### 6.4 Host paths outside the checkout

| Path | Purpose |
|---|---|
| `/etc/docker/daemon.json` | Docker runtime registration (`kata`, optionally `kata-acl`), optional `data-root`. |
| `/etc/kata-containers/runtime-rs/configuration.toml` | Kata runtime-rs configuration. |
| `/etc/kata-containers/runtime-rs/configuration-acl.toml` | Configuration of the parallel `kata-acl` runtime. |
| `/opt/kata/` | Kata 4.1.0 static bundle (runtime-rs shim, QEMU, virtiofsd, vmlinux). |
| `/opt/kata-acl/` | The parallel ACL runtime bundle, if installed. |
| `/usr/local/libexec/sndbx-backup-helper` | Root-owned backup helper. |
| `/etc/sndbx-backup-helper/` | Helper configuration (`images-root`, `backup-root`, `state-root`, `service-uid`). |
| `/etc/sudoers.d/sndbx-backup-helper` | Narrow sudoers rule for the helper. |
| `/var/lib/sndbx-backup/` | Root-only incremental snapshot state. |
| `/etc/systemd/system/sndbx.service` or `~/.config/systemd/user/sndbx.service` | Service unit. |

---

## 7. Engineering decisions

### 7.1 Decisions worth explaining

**Docker as a front-end, Kata as the boundary.** Docker gives a mature,
well-understood lifecycle API and image build system for free. Making it the
isolation boundary would have been cheaper but would have provided
shared-kernel isolation, which does not answer the prompt-injection threat
model. The cost is boot time, memory overhead and the need for KVM on the
host.

**One process, two listeners.** See [§2.2](#22-process-topology). The
alternative (separate Web UI and MCP processes with an internal REST API)
would have required a serialization boundary for the shared configuration and
introduced a class of "the UI shows stale state" bugs.

**Overlay configuration with a root override layer.** Keeping the *defaults*
next to the image they describe, and the *overrides* in one root file, means:
an image repository is self-contained and portable; a host can adjust any
value without touching image repositories; and there is exactly one place
that records host-specific decisions. The cost is a two-level lookup for
"what is the effective value of X", mitigated by the merged view the manager
holds in memory and by the descriptor's explicit merge rules.

**The merged runtime view is in-memory only.** The manager keeps one
`self.config` dict. A write through the settings editor updates it in place
for the fields that apply live. There is no file-watcher and no reload
mechanism: changing a file on disk requires a service restart. This is
deliberate — watching JSON5 files reliably is hard, and restart is already a
first-class, supported operation with a UI button.

**Recreate is guarded by `docker diff`.** The writable layer is not
automatically backed up, so a destructive recreate must be explicit. The
guard refuses when the layer has changed and tells the operator to move
persistent state into a mount or a volume first. The alternative (allow
recreate always) would silently destroy agent work.

**The apt mirror is hard-coded.** It is a host-specific performance fix
baked into the manager rather than a configuration key. This is a known wart;
it is called out in the roadmap.

**SSH access is a host-side `socat` forwarder, not a VM-side agent.** The
manager reserves a host port from a configured pool and proxies it to the
container IP. No agent runs inside the guest. Consequence: the forwarder dies
with the manager (mitigated by persisting the allocation map and reaping dead
forwarders on the next start) and the pool is small by default (11 ports).

**The console bypasses MCP.** A full interactive PTY cannot be modelled as a
request/response MCP tool. The console therefore uses a dedicated WebSocket.
The original design goal of "everything through MCP" was not achievable and
was dropped.

**One codebase per host, configuration per installation.** The manager does not
support running multiple instances on one host with different configs. The
`instance` field is a label, not an isolation mechanism.

### 7.2 Known trade-offs

| Trade-off | Chosen | Rejected alternative | Consequence |
|---|---|---|---|
| Isolation | Kata microVM | Plain Docker container | Higher resource use, slower boot, needs KVM. |
| Config edits | aiset, schema-validated | Hand-rolled JSON5 patcher | Submodule dependency, migration cost. |
| Config split | Root + per-image overlays | Single flat root file | Two-level lookup; enforced by descriptor + discovery rules. |
| Process model | Single process | Multi-process with IPC | Shared state is trivial; blast radius is large. |
| Image distribution | Local build contexts | Registry | No multi-host story; images are Git repositories. |
| Backup trigger | In-process scheduler | cron / systemd timer | No external dependency; the scheduler dies with the service. |
| Log storage | Plain text files | Structured logging backend | Easy to read; no rotation (unimplemented wipe). |
| Frontend | Vanilla JS, no build step | Bundler + framework | No toolchain, instant edits; but xterm.js comes from a CDN. |

### 7.3 Decisions that were *not* implemented the same way

This section exists because the old `SPEC.md` describes intentions that the
code does not implement. Agents reading `SPEC.md` will get these wrong.

| Original `SPEC.md` intent | Reality |
|---|---|
| Configuration lives in two files (`.env` + `config.json5`) | **Three**: `.env`, root `config.json5`, per-image `sndbx.overlay.json5`. |
| Settings tab edits `config.json5` keys | Settings tab edits the root **and** every overlay through aiset. |
| MCP tool `request_ssh_access(envid)` / `release_ssh_access()` | **Not MCP tools.** SSH is opened from the Web UI via a `socat` port pool. |
| Per-tool ACL matrix, `permissions: ["all"]` semantics | Only `"all"` vs. a read-only viewer principal is implemented. No per-tool policy engine. |
| `envids` created and managed dynamically | Static entries in `config.json5`; revocation = edit the config and restart. |
| `network.internet` / `network.local` iptables rules | **Not implemented.** Networking is plain Docker NAT. |
| `disk_max` enforced | Passed as `--storage-opt size=`; silently dropped if the storage driver rejects it. Under Kata/VirtioFS the quota is generally not enforceable. |
| `network_traffic_max` | Advisory only; no enforcement. |
| `persistent` flag controls persistence | **Implemented.** `persistent: false` removes the container on stop, so the next start is a fresh sandbox. |
| Log wiping via `wipe_period` | **Implemented.** `wipe_max_age` controls how much history is kept. |
| `mcp_bindings` proxying to VM-side MCP backends | **Implemented.** `mcp_proxy_call` speaks TCP/JSONL with the initialization handshake. |
| Files tab for browsing shared files | Not implemented. The Web UI has Dashboard, Settings, Console and MCP-test tabs. |
| Firecracker as the VMM backend | Optional. The shipped Kata configuration uses QEMU; Kata also bundles Firecracker. |
| `ssh.session_ttl` | **Implemented.** The forwarder is released after the TTL; `0` keeps it until it is closed manually. |
| `webui.auth.mode` alternatives | Only `"login"` is implemented. |

Each row above has a task id or an explicit decision in
[`docs/STATUS_AND_ROADMAP.md` §2, §3 and §5](docs/STATUS_AND_ROADMAP.md#5-open-tasks-by-priority).

---

## 8. Operations

### 8.1 Installation chain

```text
1. install_prerequisites.sh   → host packages, Docker, KVM check, Kata 4.1.0,
                                Docker runtime registration, /etc/docker/daemon.json
2. install.sh                 → .env from .env.example, config.json5 from template,
                                venv + requirements, directories, systemd unit,
                                optional backup ACL helper, enable + start service
3. systemctl start sndbx      → manager boots: config → images → sandboxes → servers
```

Full detail, including the Kata ACL variant and the canaries:
[`docs/HOST_OPERATIONS.md`](docs/HOST_OPERATIONS.md).

### 8.2 Service unit

`install.sh` writes the unit to `/etc/systemd/system/sndbx.service` when that
directory is writable, otherwise to `~/.config/systemd/user/sndbx.service`.

```ini
[Unit]
After=docker.service
Wants=docker.service

[Service]
Type=simple
Restart=always
RestartSec=5s
WorkingDirectory=<SNDBX_ROOT>
Environment="PATH=<SNDBX_ROOT>/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=<SNDBX_ROOT>/venv/bin/python3 <SNDBX_ROOT>/src/app.py
```

The system-wide variant adds `User=<user>`. The unit does **not** use
`EnvironmentFile=` — the manager parses `.env` itself.

### 8.3 Sandbox lifecycle

| Operation | Effect | Guard |
|---|---|---|
| `start` | `docker start`; creates the container if it is missing. | Runs the host prepare script and the image hook. |
| `stop` | `docker stop --time <stop_grace_seconds>`. | — |
| `restart` | `stop` then `start`. | — |
| `recreate` | `docker rm -f` then `create`. | **Refused** if `docker diff` shows rootfs changes. |
| Auto-start | Started by the manager at boot when `run_at_startup: true`. | — |

### 8.4 Image lifecycle

| Action | What it does | Guards |
|---|---|---|
| Build | `docker build -t <ref> --build-arg APT_MIRROR=… images/<id>` | — |
| Rebuild | The same with `--no-cache`. | — |
| Update & rebuild | `--no-cache --pull` — refreshes the base image, rebuilds from the existing source. | Does not touch Git. |
| Full update & rebuild | `git fetch --prune` + `git merge --ff-only <upstream>`, then rebuild with `--no-cache --pull`. | Clean worktree required; upstream branch required; fast-forward only; **incoming changes under `data/` are refused**. |

Running containers are never modified by a rebuild. Use **Recreate** to pick up
a new image.

### 8.5 SSH access

1. The operator clicks **Open SSH** in the dashboard.
2. The manager verifies that `ssh_keys` is non-empty and the sandbox is
   running, and determines the container IP.
3. If `ssh_setup_via_image_hook` is false, sndbx installs and configures sshd
   itself (`exec_ssh_setup`).
4. A host port is taken from `ssh.port_range` and a `socat` forwarder
   `TCP-LISTEN:<port>,fork,reuseaddr → TCP:<container_ip>:22` is started.
5. The allocation (`port`, `socat_pid`, `container_ip`, `allocated_at`) is
   written to `data/ssh_allocations.json`.
6. **Close SSH** kills the forwarder and releases the port. Dead forwarders
   left by a crash are reaped at the next manager start, and reservations
   older than `ssh.session_ttl` are released by a periodic sweep (every 60 s).

`ssh.session_ttl` defaults to 3600 seconds. `0` disables the expiry, so the
forwarder is only released manually.

---

## 9. Testing

**Canonical command** (from `TEST.md` §4):

```bash
PYTHONPATH=src venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

The suite uses the standard library `unittest` — no pytest dependency.
`tests/` has no `__init__.py`; the imports work through implicit namespace
packages with the repository root on `sys.path`.

| Test file | Focus |
|---|---|
| `tests/test_backup.py` | Backup settings validation, archive naming, full/incremental decisions, tar behaviour, chain metadata, concurrency, GNU tar detection. |
| `tests/test_sandbox_apt_mirror.py` | Docker command construction: apt mirror, mount preflight, tmpfs, shm size, VirtioFS/ACL profile, runtime contract, recreate guard, image-update guard. |
| `tests/test_monitoring.py` | Metric parsers, thresholds, history and caching, debounce rules, guest probe contract, `/api/monitor` authentication. |
| `tests/test_config_descriptor.py` | The shipped descriptor validates every shipped document; overlay coverage is complete. |
| `tests/test_settings_runtime.py` | Runtime bootstrap, merge behaviour, `apply_auto_start` writing to the root, `load_config` facade. |
| `tests/test_settings_overlays.py` | Overlay discovery rules and merge semantics. |
| `tests/test_settings_http.py` | The mounted aiset ASGI app: documents, tree, assets, revision checks (409/428), principal permissions. |
| `tests/test_settings_migration.py` | The root-trim migration preserves the effective configuration. |
| `tests/test_settings_parity.py` | The merged aiset view matches an independent legacy reader leaf-for-leaf. |
| `tests/test_settings_styles.py` | Asset load order, CSS scoping, required DOM ids. |
| `tests/settings_fixtures.py` | Shared fixtures: `ROOT_CONFIG`, `OVERLAY_CONFIG`, `TempRoot`, `copy_live_root`. |

**Known baseline failures** (reproduce at `HEAD`, unrelated to new work):

- `test_backup.BackupManagerRunTests.test_run_creates_incremental_archive`
- `test_backup.BackupManagerRestoreChainTests.test_full_plus_incremental_restores_changes`
- `test_backup.BackupManagerChainMetadataTests.test_chain_metadata_records_full_and_incremental`

All three fail with `'full' != 'incremental'` — the archive-type decision
under the root-helper path disagrees with the expectation.
`test_settings_parity` skips when the live root is trimmed.

**Static checks** (from `TEST.md` §3):

```bash
venv/bin/python -m py_compile src/*.py
```

`TEST.md` also contains manual protocols: runtime monitoring, browser settings
acceptance, backup smoke and restore, Docker/Kata smoke, service smoke, Web UI
smoke, MCP smoke, and a selected-sandbox smoke test.

---

## 10. Extending sndbx

### 10.1 Adding a sandbox

1. Create `images/<image-id>/` with a `Dockerfile`.
2. Add `images/<image-id>/sndbx.overlay.json5` declaring the sandbox id and
   its defaults.
3. Register the sandbox id in the root `config.json5`
   (`sandboxes.items.<id>.run_at_startup`).
4. Restart the manager. Overlay discovery will fail loudly if the id is not
   registered or if the directory does not match.

### 10.2 Adding a configuration key

1. Add the field to the relevant typedef in `config.types.json5`.
2. Add the node to `config.descriptor.json5` with the correct `env.allowed`,
   `secret` and `apply` attributes.
3. Read it in the code. `tests/test_config_descriptor.py` will fail for any
   document that uses a key the descriptor does not declare.

### 10.3 Adding an MCP tool

1. Add an entry to `TOOL_CATALOG` in `src/tools.py` with `name`, `handler`,
   `description` and `inputSchema`.
2. Implement the handler as
   `async def handler(self, params, envid, token) -> dict`.
3. The handler receives an **already-resolved** envid; map it to a sandbox id
   with `self._get_sandbox_for_envid(envid)`.

### 10.4 Adding a Web UI endpoint

Add a route inside `WebUIServer._create_app()`. The frontend is mounted at
`/` **last**, so API routes always take priority. Every route except
`/api/auth/login` and `/api/auth/logout` should depend on
`self._require_session`.

---

## 11. Glossary

| Term | Meaning |
|---|---|
| **sandbox** | A configured microVM instance, identified by a string id. |
| **image** | A local build context under `images/<id>` that produces a Docker image and optionally a hook script. |
| **overlay** | `images/<id>/sndbx.overlay.json5` — per-image sandbox defaults merged under the root. |
| **envid** | A long-lived token that maps to one sandbox and scopes a user's access. |
| **token** | A shared secret authenticating MCP calls. |
| **session** | A file-backed Web UI login record. |
| **principal** | The aiset permission set derived from a session. |
| **runtime contract** | The declared environment, companion services and readiness probes of a sandbox. |
| **capability hook** | An image hook that must print a JSON capability line. |
| **image hook** | `images/<id>/app.py`, executed inside the guest after create/start. |
