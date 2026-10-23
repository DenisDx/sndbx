# sndbx — Status, Known Issues and Roadmap

> Part of the sndbx specification. See [`../SPEC_NEW.md`](../SPEC_NEW.md) for
> the architecture. This document is the honest inventory of what is not
> finished, what is broken, and where the project is heading.

---

## 1. Implemented and verified

A short list, so an agent can tell quickly what it may rely on.

| Area | State |
|---|---|
| Docker + Kata sandbox lifecycle (create/start/stop/restart/recreate) | Implemented, unit-tested. |
| `docker diff` guard on recreate | Implemented, unit-tested. |
| `persistent: false` disposes of the container on stop | Implemented, unit-tested. |
| Local image build / rebuild / update / full-update with Git guards | Implemented, unit-tested. |
| Image hook protocol (`on_system_start`) | Implemented, exercised by every image. |
| Runtime contract (environment, companion services, capability hook, readiness probes) | Implemented, unit-tested. |
| MCP HTTP server: streamable HTTP + legacy SSE, token auth, envid routing | Implemented. |
| MCP tools: `execute_command`, `read_file`, `write_file`, `patch_file`, `sandbox_status`, `sandbox_start`, `sandbox_stop` | Implemented; the file tools are shell-free. |
| `mcp_proxy_call`: real TCP/JSONL proxy to VM-side MCP backends | Implemented, unit-tested against a fake backend. |
| Web UI: login, dashboard, sandbox actions, SSH pool, console, system log | Implemented. |
| Console restricted to sessions with `permissions: ["all"]` | Implemented. |
| Console works without the CDN (vendored xterm.js fallback) | Implemented. |
| Settings editor over aiset: root + all overlays, schema validation, revision-checked writes, live logging and auto-start | Implemented, 29 settings tests. |
| Overlay discovery and merge with root-wins semantics | Implemented, unit-tested. |
| Per-VM monitoring with guest probe, thresholds and history | Implemented, 23 tests. |
| Per-VM incremental backup scheduler with the privileged root helper | Implemented; the archive-type decision now honours the run time (all 44 backup tests pass). |
| SSH forwarder session TTL | Implemented, unit-tested. |
| Centralized per-tag logging with `all.log`, timezone support and an MCP request log | Implemented. |
| Age-based log wiping and a size cap on the MCP request log | Implemented, unit-tested. |
| Kata runtime path discovery from `daemon.json` | Implemented, unit-tested. |
| Host installers (`install_prerequisites.sh`, `install.sh`, uninstall) | Implemented; the recorded defects are fixed (§9 of `HOST_OPERATIONS.md`). |
| Parallel `kata-acl` runtime, migration and canaries | Implemented and documented in `_kata_acl_install.md`. |

---

## 2. Declared but not implemented

Configuration keys that exist in the schema and are read by the code, but
whose promised behaviour does not exist. An agent must not assume these work.
The last column links each key to its task in §5 or records the decision not
to build it.

| Key | Promise | Reality | Task / decision |
|---|---|---|---|
| `network_traffic_max` | Per-VM traffic quota | Config-only placeholder. No accounting, no enforcement. The descriptor says so explicitly. | **T13** — needs a cgroup or nftables accounting layer; no demand yet. |
| `disk_max` | Writable-layer size cap | Passed as `--storage-opt size=`; silently dropped when the storage driver rejects it. Under Kata/VirtioFS the quota is generally not enforceable. | **T10** — blocked on Kata/VirtioFS support. |
| `mcp.items[]` | Multi-backend support | Reserved, empty, unused. | **No task.** Reserved for a second MCP backend; the single built-in backend does not need it. |
| `webui.auth.mode` | Multiple auth modes | Only `"login"` is implemented. | **T14** — a second mode (for example token or OIDC) needs a design first. |
| `webui.auth.users[].permissions` | Fine-grained permissions | Only `"all"` (full access) and "anything else" (read-only viewer) are distinguished. There is no per-tool policy engine. | **T6** — the policy engine for MCP tokens and the Web UI share the same model. |
| `envids` lifecycle | Create/revoke envids dynamically | Static entries only. Revocation = edit the config and restart. | **T7** — needs an API and a state store. |

The following keys used to be in this list and are now implemented:
`logging.wipe_period` / `wipe_max_age` / `mcp_request_log_max_mb`,
`persistent`, `ssh.session_ttl`, `mcp_bindings` and `mcp_proxy_call`.

## 3. Features from the original design that were never built

`SPEC.md` describes an ambitions that the code does not implement. Listed so
that nobody re-discovers them by reading the old document. The last column
links each feature to its task in §5 or records why it will not be built.

| Feature | Status | Note | Task / decision |
|---|---|---|---|
| Per-tool ACL matrix (`permissions: ["all"]` plus per-tool restrictions) | Not built | The MCP authorization model is "valid token + envid in the user's list". | **T6** |
| `request_ssh_access` / `release_ssh_access` MCP tools | Not built | SSH is a Web UI action, not an MCP tool. | **T12** |
| Dynamic envid creation ("create a new instance on demand") | Not built | — | **T7** |
| `network.internet` / `network.local` iptables control | Not built | Networking is plain Docker NAT. | **T9** |
| Files tab (browse/download the shared directory) | Not built | The Web UI has Dashboard, Settings, Console and MCP-test tabs. | **T8** |
| Console tunneled through MCP | Not built | The console uses a dedicated WebSocket PTY bridge. | **Will not be built.** A full interactive terminal cannot be expressed as a request/response MCP tool. |
| `firecracker` as the mandatory VMM | Optional | The shipped Kata configuration uses QEMU; Kata also bundles Firecracker. | **No task.** Firecracker stays optional; switching the VMM is a Kata configuration change. |
| Multi-host / multi-instance management | Out of scope | One manager per host. | **Out of scope.** The `instance` field is a label, not an isolation mechanism. |
| Sandboxed "users" inside the guest | Not built | Guests run as root (or a single application user). | **Will not be built.** Guest-side user management belongs to the image, not to the manager. |

---

## 4. Known defects

All application and host-tooling defects recorded here have been fixed. The
table is kept as a record of what was wrong and what replaced it.

### 4.1 Application defects

| # | Former defect | Fix |
|---|---|---|
| A1 | `POST /api/runtime/kata/repair` always failed with HTTP 500. `WebUIServer._repair_kata_runtime` was defined inside `_containers_view` after its `return` statement, so it was never bound to the class; the dead code below it referenced undefined names. | The method was moved into the class body and the unreachable block was deleted. A test asserts the endpoint answers. |
| A2 | The MCP test tab was not initialized after a manual login: `initMCPTest()` was called only from the boot IIFE. | `onLogin()` now calls it too. |
| A3 | The auto-start tooltip named the retired `data/sandbox_startup_overrides.json`. | It now names the root `config.json5`, which is where the value is stored. |
| A4 | The console depended on a CDN: `xterm@5.3.0` and `@xterm/addon-fit@0.10.0` were loaded from `cdn.jsdelivr.net`. | The assets are vendored in `src/webui/frontend/vendor/` and the frontend falls back to them when the CDN is unreachable. |
| A5 | Tool paths were not shell-escaped: `read_file`, `write_file` and `patch_file` interpolated the path into a shell command. | The file tools now use `docker exec -i` with an argument vector; the path is never shell text. |
| A6 | `patch_file` interpolated the search and replace text into a `python3 -c` command line. | The patch travels over stdin and the script is a constant. |
| A7 | The `mcp_requests.jsonl` log was never rotated and reached ~88 MB. | `logging.mcp_request_log_max_mb` (default 64) rotates it; the age-based wipe also applies. |
| A8 | The MCP server's `tools/list` does not require a token, but `tools/call` does. | Intentional and unchanged: tool discovery is not an authentication boundary. |
| A9 | The console WebSocket authenticated the cookie but did not check the permissions. | A session without `permissions: ["all"]` is refused with close code `4001`. |
| A10 | `install.sh` created the Docker network `sndbx-net`, which no source file references. | The network is no longer created. |

### 4.2 Test-suite baseline

The suite is green: **196 tests, 1 skip** (the parity test skips when the live
root is trimmed, which is covered by `test_settings_migration`).

The three `test_backup` failures that used to reproduce at `HEAD` are gone.
Their cause was `_decide_archive_type()` computing `now = datetime.now()` and
ignoring the `now=` override that `run_backup()` accepts, so an archive dated
in the past always looked older than `full_every` intervals and every later
run produced a *full* archive. The decision now uses the run's reference time.

### 4.3 Host tooling defects

All recorded defects are fixed; see
[`HOST_OPERATIONS.md` §9](HOST_OPERATIONS.md#9-known-defects-in-the-host-tooling).

### 4.4 Documentation drift

| Document | Problem |
|---|---|
| `SPEC.md` | **Superseded** by `SPEC_NEW.md`. Describes the pre-aiset, two-file configuration model and several unbuilt features. Kept for history. |
| `SPEC_.md` | Holds the corrections that would be required in `SPEC.md`. Still accurate as a record, but the corrections are now embodied in `SPEC_NEW.md`. |
| `README.md` | Accurate for installation and operation. Its "Next Steps" section was replaced by "Current Feature Set", which lists the current features and the remaining roadmap. |
| `MVP_SUMMARY.md`, `GETTING_STARTED.md` | Describe the MVP. Accurate as history, incomplete as current documentation. |
| `.env.example` | Reconciles with the configuration template: the dead `VM_ID`, `VM_SSH_USER`, `VM_SSH_PORT_RANGE_START` and `VM_SSH_PORT_RANGE_END` are gone, and `SNDBX_INSTANCE`, `LOG_TIMEZONE` and the log-management variables are declared. |

### 4.5 Where the other finding records live

This document is the task registry, but the other finding records live
elsewhere. Read them together:

| Record | Owns |
|---|---|
| [`SPEC_NEW.md` §7.3](../SPEC_NEW.md#73-decisions-that-were-not-implemented-the-same-way) | Every point where the original `SPEC.md` and the code disagree. §2 and §3 of this document are the same findings seen from the task side. |
| [`HOST_OPERATIONS.md` §9](HOST_OPERATIONS.md#9-known-defects-in-the-host-tooling) | The twelve host-tooling defects and their fixes. |
| [`IMAGES.md` §7.2](IMAGES.md#72-common-pitfalls) | The failure modes of a new image and how to avoid them. |
| [`proposal_monitoring.md` §14](../proposal_monitoring.md) | The monitoring design. Its deliberate deferrals are tracked here as **T15**, **T16**, **T17** and **T10**. |
| [`proposal_aiset_integration.md`](../proposal_aiset_integration.md) | The settings design. Its phase markers are historical; the phase status is in `execution_aiset_integration.md` §1. |
| [`execution_aiset_integration.md`](../execution_aiset_integration.md) | The settings delivery log, including the live rollout and the resolved deviations. |
| [`proposal_backup.md`](../proposal_backup.md) | The backup design. Its two follow-up sections are historical records of delivered work. |
| `images/clawvm/proposal_vmupdate1.md` | Owned by the `clawvm` image repository. Its open decisions are tracked here as **T18**. |
| `images/langvm/proposal_langvm_update.md` | Owned by the `langvm` image repository. Its deferrals are tracked there, not here. |

**Rule for the registry.** Every open item gets a task id in §5, or an entry
in §"Not planned", and the document that raised it is linked from the task.
A finding that lives only in a proposal document is not finished bookkeeping.

**What counts as an open item.** Three kinds, and all three are registered
here:

1. **Work** — a task with an id (**T1**–**T18**) in §5.
2. **A decision still to be made** — a question with an id (**D1**–**D4**) in
   §"Open design questions". These are not work items; they await a choice.
3. **A deliberate omission** — a row in §"Not planned".

Anything that is neither working, undecided, nor deliberately dropped is not
open. The sources that feed this registry are indexed in §4.5; a sweep of
them must not surface an item that is missing here.

---

## 5. Open tasks, by priority

Every task has an id. The ids are referenced from §2 and §3 so that no finding
is left without a task or an explicit decision.

### P0 — correctness and safety

Nothing outstanding. The P0 list is empty as of 2026-10-23.

### P1 — completeness

| Id | Task | Notes |
|---|---|---|
| T1 | Vendor the remaining third-party frontend assets, or move the whole frontend to a bundler. | Only xterm.js is vendored so far; the rest of the UI has no external dependencies. |
| T2 | Extend `uninstall.sh` to remove the `kata-acl` runtime and the `aiset` submodule. | Both are host-level or VCS-level state; the removal needs an explicit confirmation. |
| T3 | Add a live check for `mcp_proxy_call` against the `mcp_toolbox_1` image. | The proxy is unit-tested against a fake backend; the toolbox sandbox is stopped in the reference installation, so the live path was not exercised. |
| T4 | Clean up the inherited `aibusiness` compose file. | `images/aibusiness/docker-compose.yml` still names `image: langvm:latest` and `container_name: langvm-app`. Owned by the image repository, not by the root repository. |
| T5 | Give the `denis_obsidian_assistant` fork a clean history path. | It still points at the `clawvm` upstream and carries uncommitted local customizations. It must never be committed or pushed as-is; the local changes are intentional. |

### P2 — product

| Id | Task | Notes |
|---|---|---|
| T6 | Per-tool authorization. | The MCP server has the token, the envid and the tool name at the moment of dispatch (`src/mcp_server.py:425-433`) — that is exactly where a policy hook belongs. `webui.auth.users[].permissions` would need to be mirrored into `users.items[].permissions` or read from a separate policy document. |
| T7 | Dynamic envid creation and revocation. | Requires an API and a state store; the envid map is currently static configuration. |
| T8 | Files tab for the shared directory. | The old SPEC suggested reusing an existing component rather than building one. |
| T9 | Per-VM network policy (`network.internet`, `network.local`). | Host-side iptables rules keyed by the sandbox id; the container's IP is already discoverable (`get_container_ip`). |
| T10 | Per-VM disk quota enforcement. | Requires a Kata configuration that supports it; the current VirtioFS profile does not expose a reliable per-VM percentage. |
| T11 | Frontend tests. | The suite is entirely Python. `tests/test_settings_styles.py` only checks asset order, CSS scoping and DOM ids. |
| T12 | MCP tools to open and close SSH access. | The port pool exists and is now TTL-bounded, but it is only reachable from the Web UI. Exposing it as MCP tools would let an agent claim a sandbox directly. |
| T13 | Per-VM traffic accounting (`network_traffic_max`). | Needs a cgroup or nftables accounting layer that reports per-sandbox usage. Until then the key stays a documented placeholder; it must not be read as a quota. |
| T14 | Additional Web UI auth modes (`webui.auth.mode`). | Only `"login"` works. A second mode (token, OIDC, reverse-proxy headers) needs a design and a principal mapping first. |
| T15 | Always-on server sampler with persistent metric history. | Deferred by `proposal_monitoring.md` §14. Today metrics exist only while a Web UI user has switched monitoring on, and the 120-sample history is lost on restart. Needs a background sampler and a store. |
| T16 | Configurable monitoring thresholds. | Deferred by `proposal_monitoring.md` §14. The thresholds are hard-coded constants in `src/monitoring.py`. |
| T17 | Browser notifications for monitoring alerts. | Deferred by `proposal_monitoring.md` §14. |
| T18 | Resolve the OpenClaw native-service decisions. | `images/clawvm/proposal_vmupdate1.md` §11 lists seven open decisions (systemd delegation, user-manager-only arrangement, native update ownership, the conventional VM backend, read-only root, guest-local database state, acceptable host changes). The document is owned by the `clawvm` image repository; the root repository only tracks the outcome. |

### Open design questions (awaiting a decision)

These are not work items but decisions that are still open. Each one is
referenced from the "Planned directions" section that raises it.

| Id | Question | Raised by | Notes |
|---|---|---|---|
| D1 | How to version the *interface* between an image and sndbx — the hook contract and the runtime contract — so that a newer image can rely on a newer sndbx and vice versa. | §6.2 | Today the contracts are implicit in `src/sandbox.py` and in each `app.py`. |
| D2 | Whether `mcp_bindings` should become the standard way for an image to expose its own MCP tools to the host. | §6.2 | `mcp_toolbox_1` is the only image that uses it today. |
| D3 | Whether sndbx should keep supporting host ACL semantics at all, or move to a model where guest-owned files never need host ACLs — for example by owning the data directory through a dedicated host user with a fixed uid. | §6.4 | The `posix-acl-compat` profile and the per-container annotation are the current answer. |
| D4 | Whether the `aiset` library should gain the two capabilities sndbx needs from it: an MCP adapter and a lifecycle-hook dispatcher. | §6.1 | Both are known gaps in `aiset` itself, not in sndbx. `aiset` is a pinned submodule, so the work happens upstream. |

### Not planned

Recorded so that the absence is a decision rather than an oversight.

| Item | Decision |
|---|---|
| Tunneling the console through MCP | A full interactive terminal cannot be expressed as a request/response MCP tool. The console keeps its own WebSocket. |
| Multi-host or multi-instance management | Out of scope: one manager per host. `instance` is a label, not an isolation mechanism. |
| Guest-side user management | Belongs to the image, not to the manager. Guests run as root or as a single application user. |
| Making Firecracker the mandatory VMM | Firecracker stays optional. Switching the VMM is a Kata configuration change, not a manager feature. |
| `mcp.items[]` multi-backend support | Reserved for a second MCP backend. The single built-in backend does not need it. |
| A `SNDBX_SETTINGS_BACKEND=legacy\|aiset` switch | The dual-backend flag was planned in `proposal_aiset_integration.md` and never built. The decision was to cut over to aiset directly; the legacy loader was removed. A fallback switch would reintroduce the dead path. |
| Applying the `SPEC_.md` corrections to `SPEC.md` | `SPEC.md` is read-only for agents, so the two recorded corrections can never be applied by an agent. They are embodied in `SPEC_NEW.md` instead; `SPEC.md` stays as history. |
| Managed per-run workspace/artifact volumes, an asynchronous job/event bridge, egress profiles, ingress allow-lists and platform-wide secret storage | Deferred by `proposal_langvm_update.md` §"Explicitly Deferred". That document belongs to the `langvm` image repository, so the items are tracked there and not in this registry. |
| Per-VM disk quotas in the monitoring view | **T10** — the disk quota itself. The monitoring display of it is part of the same work. |

---

## 6. Planned directions

These are the directions the project has been moving in. They are recorded so
that a new contributor understands the intent, not to commit anyone to a date.

### 6.1 Settings as a first-class, shared subsystem

The move to `aiset` was the biggest structural change: the configuration
stopped being a hand-rolled JSON5 blob with a bespoke loader and became a
schema-validated, revision-checked, source-preserving document set shared with
aidir and other projects in the family.

The intended direction:

- keep the descriptor as the single source of truth for configuration shape;
- keep the root/overlay split, because it makes image repositories portable;
- extend `aiset` with capabilities sndbx needs (the MCP adapter and the
  lifecycle-hook dispatcher are the two known gaps in `aiset` itself) — **D4**;
- treat the Settings tab as the only configuration editor — no second path.

### 6.2 Images as independent, versioned products

The image directories are deliberately separate Git repositories. The
intended direction is that an image can be released on its own cadence, that
`sndbx` consumes it through the `full_update` path, and that the overlay is
the contract between the image and the host.

Open design questions, registered in §5 as **D1** and **D2**:

- How to version the *interface* between an image and sndbx (the hook
  contract, the runtime contract) so that a newer image can rely on a newer
  sndbx. — **D1**
- Whether `mcp_bindings` should become the standard way to expose an image's
  own MCP tools to the host. — **D2**

### 6.3 Observability

Monitoring exists but is deliberately minimal: no background sampler, no
persistence, no notifications. The likely direction is opt-in alerting on
top of the existing thresholds, plus a decision about persistence (the
current 120-sample in-memory history is lost on restart).

The `mcp_requests.jsonl` volume and the missing log wipe were the immediate
operational problems; both are now addressed — the request log has a size cap
and an age-based rotation, and the log files are wiped on a configurable
period. The remaining question is whether the per-VM metrics should be
persisted; that is **T15**.

### 6.4 Isolation depth

The ACL work (`kata-acl`, the canaries) established that POSIX ACLs do not
propagate through VirtioFS when submounts are announced. The
`posix-acl-compat` profile and the per-container annotation are the current
answer. The longer-term question is whether sndbx should keep supporting
host ACL semantics at all, or move to a model where guest-owned files never
need host ACLs — for example by owning the data directory through a
dedicated host user with a fixed uid. Registered as **D3**.

### 6.5 Multi-agent integration

sndbx is designed to be driven by aidir and to host OpenClaw instances. The
`envid` abstraction is the seam. The open question is whether the isolation
boundary should move up: an agent that can `rm -rf` its own sandbox is
contained, but an agent that can reach the *manager* API can start other
sandboxes. A policy layer that limits which sandboxes a given token can
*manage* (not merely *use*) would close that gap — it is the same work as
**T6** in the P2 list.

---

## 7. How to verify the current state

```bash
# Unit and regression suite
PYTHONPATH=src venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v

# Static check
venv/bin/python -m py_compile src/*.py

# Configuration loads
PYTHONPATH=src venv/bin/python -c "from config import load_config; c=load_config(); print(c['config']['title'], len(c['config']['sandboxes']['items']))"

# Service health
systemctl --user is-active sndbx
journalctl --user -u sndbx -n 100 --no-pager

# MCP
./test_mcp.sh http://127.0.0.1:30081 test-token-123456789 default-env-token

# Web UI
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:30080/api/auth/me   # 401 expected
```

The manual protocols — monitoring, browser settings acceptance, backup smoke
and restore, Docker/Kata smoke, service smoke, Web UI smoke, MCP smoke and a
selected-sandbox smoke test — are in `TEST.md`.

**Safety rule for testing.** Never run Recreate, Build, Update, Full update or
a restore against a production image as part of a test. Recreate destroys the
writable layer; the image-update path rewrites the image source.
