# sndbx — Image Subsystem

> Part of the sndbx specification. See [`../SPEC_NEW.md`](../SPEC_NEW.md) for
> the architecture and configuration model, and
> [`INTERFACES.md`](INTERFACES.md) for the hook protocol reference.

## 1. What an image is

An **image** is a local build context that produces a Docker image usable as a
sandbox. It lives in its own directory under `images/`:

```text
images/<image-id>/
├── Dockerfile            ← required
├── app.py                ← optional lifecycle hook
├── sndbx.overlay.json5   ← sandbox defaults owned by this image
├── data/                 ← persistent guest state (optional)
├── .env                  ← guest secrets, mounted read-only (optional)
├── docker/               ← entrypoint helpers (optional)
└── *.md                  ← per-image documentation (optional)
```

Three identifiers are involved and they are **not** interchangeable:

| Identifier | Where it is used | Example |
|---|---|---|
| **image id** | Directory name under `images/`. | `denis_obsidian_assistant` |
| **image tag** | `sandboxes.items.<id>.image`. | `denis-obsidian-assistant:latest` |
| **sandbox id** | `sandboxes.items.<id>` key, container name suffix. | `denis-obsidian-assistant` |

The **directory name** and the **tag** usually differ. `local_image_id` exists
to break the tie explicitly; without it, sndbx derives the directory from the
tag prefix (before `:`) or from the sandbox id. The resolution order lives in
`src/settings/overlays.py:38` (`overlay_dir_for`) and
`src/sandbox.py:97` (`_local_image_id_for_ref`):

```text
local_image_id  →  image tag prefix  →  sandbox id  →  demo alias
```

Each candidate is also tried with `-` replaced by `_`. The demo aliases map
`sandbox-1` and `default-sandbox-1` to `default_sandbox_1`.

**Rule:** if two sandboxes point at the same directory, they must declare
identical backup settings — the backup scheduler refuses conflicting schedules
for one `local_image_id` (`src/backup.py:248`).

## 2. The build pipeline

### 2.1 When a build happens

| Trigger | Behaviour |
|---|---|
| Manager start | `_build_startup_images()` builds every configured image not yet present in the Docker image store. |
| Sandbox creation | `_ensure_image_ready()` builds the image if missing. |
| Web UI **Build** | `docker build -t <ref> --build-arg APT_MIRROR=… <dir>`. |
| Web UI **Rebuild** | The same with `--no-cache`. |
| Web UI **Update & rebuild** | `--no-cache --pull` — refreshes the base image, rebuilds from the current source. |
| Web UI **Full update & rebuild** | `git fetch --prune` + `git merge --ff-only <upstream>`, then `--no-cache --pull`. |

The build context is always the image directory; the build-arg `APT_MIRROR`
is passed so the Dockerfile can rewrite the guest's apt sources at build time
(the default value in the Dockerfiles matches the manager's runtime mirror).

### 2.2 The `full_update` guard chain

`DockerSandboxManager.update_configured_image` (`src/sandbox.py:189`) refuses
to proceed unless **all** of the following hold:

1. The image resolves to a local directory.
2. That directory is the Git root of its worktree.
3. `git status --porcelain --untracked-files=all` is empty — **no local
   changes**, including untracked files.
4. An upstream branch is configured.
5. `git fetch --prune` succeeds.
6. No incoming path is under `data/` — the persistent state directory is
   protected from being overwritten by upstream.
7. `git merge-base --is-ancestor HEAD <upstream>` holds — the update must be a
   fast-forward.
8. `git merge --ff-only <upstream>` succeeds.

Only then is the image rebuilt with `--no-cache --pull`.

**Why so strict.** The image directory contains the VM's persistent data.
A non-fast-forward merge or a dirty worktree means a human has made decisions
that an automatic merge would destroy. The `data/` guard exists because
upstream repositories sometimes contain placeholder data directories; merging
them would clobber live state.

**Running containers are never affected by a rebuild.** The new image only
applies after a **Recreate**, which is itself guarded by `docker diff`.

### 2.3 `data/` is protected

The `data/` protection is asymmetric and deliberate:

- `data/` is excluded from Git tracking in every image repository.
- The image-update path refuses any incoming change under `data/`.
- The backup scheduler archives `images/<id>/` **including** `data/`.

## 3. The image hook (`app.py`)

### 3.1 Mechanism

If `images/<id>/app.py` exists, it is copied into the image at
`/opt/sndbx-image/app.py` and executed inside the guest after a successful
container create or start:

```bash
docker exec --user 0 \
  -e SNDBX_HOOK=on_system_start \
  -e SNDBX_CONTEXT_JSON='{...}' \
  -e <runtime contract variables> \
  sndbx-<sandbox_id> python3 /opt/sndbx-image/app.py
```

The hook runs **as root** (`--user 0`) regardless of the image's `USER`
directive, because it must fix up ownership and start services.

**Retry.** If the output indicates the Kata rootfs is not ready
(`open /var/lib/docker/rootfs/overlayfs/… no such file or directory`), the
manager retries up to 6 times with a 2-second delay.

**Failure policy.**

- Without a `runtime_contract`: the hook failure is logged as a **warning**
  and the sandbox still starts. This keeps non-critical images working even
  if their hook breaks.
- With a `runtime_contract`: the hook failure is **fatal** — the container is
  removed (create) or stopped (start) and the operation fails.

### 3.2 Context payload

`SNDBX_CONTEXT_JSON` is a JSON object:

```json
{
  "sandbox_id": "denis-obsidian-assistant",
  "image_ref": "denis-obsidian-assistant:latest",
  "resolved_mounts": [
    {
      "guest_path": "/home/clawvm",
      "source_type": "directory",
      "permission": "rw",
      "bind_propagation": "",
      "required": true
    }
  ],
  "ssh_keys": ["ssh-ed25519 AAAA... user@host"]
}
```

`resolved_mounts` reflects the **preflighted** mounts, i.e. after existence
checks, optional creation and `host_mode` application. `ssh_keys` is
de-duplicated and stripped of empty entries.

### 3.3 The hook contract

A hook must:

1. Read `SNDBX_CONTEXT_JSON` and fail clearly if it is missing or invalid.
2. Perform its idempotent setup (ownership, sshd, service start).
3. Exit `0` on success.
4. Exit non-zero on failure — the manager turns that into a warning or an
   error depending on the runtime contract.

Unknown hook names must exit non-zero rather than silently doing nothing.
Only `on_system_start` is dispatched today.

### 3.4 The capability hook

When `runtime_contract.capability_hook` is true, the manager parses the hook's
**stdout as JSON** and compares it with the contract:

```json
{"provides_postgres": true}
```

The value must equal the boolean interpretation of
`runtime_contract.environment.SNDBX_PROVIDES_POSTGRES`. A mismatch aborts the
start. This is how sndbx knows that a guest actually provides a database
before it starts injecting companion-service environment variables.

Only `langvm` and `aibusiness` implement it.

### 3.5 What hooks typically do

| Task | Pattern | Used by |
|---|---|---|
| Ownership repair | `find <home> -xdev … -exec chown <user>:<user> {} +`, pruning the read-only `.env` mount and external mounts. | all packaged images |
| SSH setup | Write `authorized_keys` (mode 0600), generate an ed25519 host key into `/run/sshd`, write a derived `sshd_config`, validate with `sshd -t`, start if not running. | all packaged images |
| Service start | Start the guest's own daemons (socat bridges, Caddy, OpenClaw, …). | `mcp_toolbox_1`, `clawvm`, `open-webui`, `paperclip` |
| Key sync timer | Install a systemd oneshot + timer that re-reads a key file from a shared mount every 30 s. | `default_sandbox_1` |
| Capability report | Print the JSON capability line. | `langvm`, `aibusiness` |

The `default_sandbox_1` hook is the reference implementation of the minimal
contract; the packaged images are the reference implementation of the "full"
contract.

## 4. The runtime contract

`runtime_contract` declares what the guest needs and what sndbx must provide
in return.

```json5
runtime_contract: {
  version: 1,
  capability_hook: false,
  environment: {
    WEBUI_BIND_HOST: "0.0.0.0",
    WEBUI_PORT: "8080"
  },
  companion_services: [
    {
      name: "postgres",
      host: "host.docker.internal",
      port: 5432,
      inject: { host_env: "DATABASE_HOST", port_env: "DATABASE_PORT" },
      readiness: { type: "tcp", host: "host.docker.internal", port: 5432 }
    }
  ],
  readiness: [
    { type: "http", url: "http://127.0.0.1:8080/health", attempts: 90, interval_seconds: 1 }
  ]
}
```

### 4.1 Environment injection

- Variable names must match `^[A-Z_][A-Z0-9_]*$`.
- Every key becomes a `-e NAME=VALUE` argument on `docker run`.
- Values may be placeholders resolved by aiset before the manager sees them.

### 4.2 Companion services

A companion service describes an endpoint **outside** the guest that the
guest must reach.

- `name` — `[a-z][a-z0-9_-]*`.
- `host` — hostname or IP; `host.docker.internal` additionally adds
  `--add-host host.docker.internal:host-gateway`.
- `port` — 1–65535.
- `inject` — the `host_env` / `port_env` variable names whose values become
  the host and port inside the guest.
- `readiness` — an optional probe executed **before** the sandbox is declared
  started.

**Special case: `postgres`.** When `SNDBX_PROVIDES_POSTGRES` is true, a
companion service named `postgres` is **skipped** — the guest owns its own
database and must not be pointed at the host's.

**Conflict rule.** An `inject` target that collides with a key in
`environment` is an error.

### 4.3 Readiness probes

| Type | Fields | Semantics |
|---|---|---|
| `http` | `url` (must be `http://` or `https://`) | `urllib.request.urlopen` with a 2 s timeout. |
| `tcp` | `host`, `port` | `socket.create_connection` with a 2 s timeout. |
| `command` | `command` | `docker exec sndbx-<id> sh -c "<command>"`. |

Each probe has `attempts` (default 1) and `interval_seconds` (default 1). The
sandbox start fails if any probe never succeeds. Probes run **after** the
image hook.

### 4.4 Effect on the container command

When a `runtime_contract` is present, the container's command is **not**
`sleep infinity` — the image's own `ENTRYPOINT`/`CMD` is used, so the guest
actually runs its application. Without a contract, sndbx appends
`sleep infinity` to keep the container alive.

## 5. Persistent data layout

The convention across images: **`data/` is the guest's home directory**, bind
mounted read-write.

```text
images/<id>/data/  →  <guest home path>
```

| Image | Guest home | Notes |
|---|---|---|
| `default_sandbox_1` | `/root` | No `data/`; uses the root `shared/` tree. |
| `mcp_toolbox_1` | `/root` | No `data/`; uses the root `shared/` tree. |
| `clawvm` | `/home/clawvm` | The complete home of the `clawvm` user (UID 1000). |
| `denis_obsidian_assistant` | `/home/clawvm` | Same, but UID/GID 1002 and two extra external mounts. |
| `langvm` | `/var/lib/langvm` | Portable runtime root; also available as a Docker-managed volume in Compose mode. |
| `aibusiness` | `/var/lib/langvm` | Same layout as `langvm`. |
| `open-webui` | `/app/backend/data` | The application home (`webui.db`, `vector_db`, `uploads`, `cache`). |
| `paperclip` | `/paperclip` | `PAPERCLIP_HOME` (`instances/default`, `config`, `db`, `workspaces`, `secrets`). |

Additional mounts follow the same pattern: a `.env` file mounted read-only at
`<home>/.env`, and — for the Obsidian fork — the host's Obsidian vault and
`Agents_shared` mounted into the workspace.

**Ownership.** The guest user inside the image is remapped at container start
to match the host directory owner (via `usermod`/`groupmod` in the Dockerfile
and a runtime `chown` sweep in the hook). A marker file
(`.open-webui-ownership-verified`, `.paperclip-ownership-verified`) prevents
repeating the expensive `chown` on every start.

## 6. The image catalogue

### 6.1 Overview

| Image directory | Tag | Base | Guest user | Overlay sandbox ids |
|---|---|---|---|---|
| `default_sandbox_1` | `default_sandbox_1` | `ubuntu:22.04` | `root` | `sandbox-1`, `default-sandbox-1` |
| `mcp_toolbox_1` | `mcp_toolbox_1` | `ubuntu:22.04` + Node 20 | `root` | `mcp-toolbox-1` |
| `clawvm` | `clawvm:latest` | `node:24-bookworm` | `clawvm` (1000) | `clawvm` |
| `denis_obsidian_assistant` | `denis-obsidian-assistant:latest` | `node:24-bookworm` | `clawvm` (1002) | `denis-obsidian-assistant` |
| `langvm` | `langvm:latest` | `postgres:16` | `root` | `langvm` |
| `aibusiness` | `aibusiness:latest` | `postgres:16` | `root` | `aibusiness` |
| `open-webui` | `open-webui:latest` | `ghcr.io/open-webui/open-webui:main` | `openwebui` | `open-webui` |
| `paperclip` | `paperclip:latest` | `node:24-trixie-slim` (3-stage) | `node` | `paperclip` |

### 6.2 Repository ownership

Only `images/default_sandbox_1/` and `images/mcp_toolbox_1/` are tracked by
the **root** sndbx repository. The other six directories are **independent Git
repositories** with their own remotes and history, and the root `.gitignore`
excludes `images/*/` except those two.

This means: an image can be versioned, reviewed and released independently of
sndbx, and `full_update` can fast-forward it from its own upstream.

### 6.3 `default_sandbox_1` — the minimal reference

- `ubuntu:22.04` + `python3`, `openssh-server`, `ca-certificates`, `ncurses-term`,
  `locales`.
- No `ENTRYPOINT`; the container runs `sleep infinity` (added by sndbx).
- Hook: writes `authorized_keys`, configures sshd, installs a
  `sndbx-ssh-sync.service` / `.timer` pair that re-reads
  `/root/shared/.ssh-sync.json` every 30 s.
- Overlay: `sandbox-1` (image via `${VM_IMAGE:-default_sandbox_1}`) and
  `default-sandbox-1` (image `default_sandbox_1`, key `${MY_SSH_PUBLIC_KEY}`).
- Shared mounts: `${SNDBX_ROOT}/shared/default_sandbox_1/.ssh → /root/.ssh`,
  `…/shared → /root/shared` (`host_mode: "0777"`), and
  `${VM_SHARED_DIR:-/tmp/sndbx-shared} → /mnt/shared`.

This is the image to copy when creating a new minimal sandbox image.

### 6.4 `mcp_toolbox_1` — the MCP bridge

- `ubuntu:22.04` + Node 20 (nodesource) + `@modelcontextprotocol/server-filesystem`,
  `mcp-shell-server` (npm) and `mcp-server-git` (pip).
- Hook starts three `socat` TCP bridges, each only if the port is free:

  | Guest port | Backend | Tag |
  |---|---|---|
  | 9011 | `mcp-server-filesystem /root/shared` | `filesystem` |
  | 9012 | `npx -y mcp-shell-server` | `bash` |
  | 9013 | `mcp-server-git --repository /root/shared` | `git` |

- The only image that declares `mcp_bindings` (`transport: "tcp-jsonl"`,
  `auth_mode: "token_passthrough"`, `tools: ["*"]`), published to the host as
  39011/39012/39013.

### 6.5 `clawvm` — OpenClaw host

- `node:24-bookworm` with ~100 apt packages, the Caddy apt repository,
  Playwright 1.62.0 + Chromium under `/ms-playwright`, and an offline-bundled
  Python document-tooling venv (`/opt/document-tools/wheels` + SHA256SUMS).
- The `node` user is renamed to `clawvm` (UID/GID 1000:1000) with a random
  SHA-512 password; `USER clawvm`, `ENTRYPOINT /usr/local/bin/clawvm-entrypoint`.
- The entrypoint sources the env loader, runs `init-document-tools`, then
  execs `~/config/start.sh` if it exists, else `sleep infinity`.
- `read_only_rootfs: true`, tmpfs `/tmp` (64 m) and `/run` (16 m), and a
  `runtime_contract` carrying `CLAWVM_HTTPS_HOST`, `CLAWVM_HTTPS_PORT` and
  `OPENCLAW_GATEWAY_PORT`.
- Nine published ports: SSH (2222), HTTP (80), HTTPS (8880) and 8881–8885.
- OpenClaw itself is **not** baked into the image; it is installed inside the
  VM by its own installer and lives under `data/.openclaw/`.

### 6.6 `denis_obsidian_assistant` — the ClawVM fork

- Byte-for-byte the same Dockerfile and package set as `clawvm`, except the
  user is remapped to **UID/GID 1002:1002** to match the host operator.
- Two extra external mounts: the host Obsidian vault and `Agents_shared` are
  mounted read-write into the guest workspace. These are **not** included in
  the image backup.
- `virtiofs_profile: "posix-acl-compat"`, `stop_grace_seconds: 360`,
  `backup: {enabled: true, interval_hours: 24, full_every: 7}`.
- The Git remote still points at the `clawvm` upstream; the working tree has
  local, uncommitted customizations that must not be committed or pushed.

### 6.7 `langvm` and `aibusiness` — LangChain/LangGraph runtimes

- `postgres:16` base (the guest owns its own PostgreSQL 16 cluster).
- The same application source (`src/app/`, 37 modules), the same
  `management_httpapp`, the same `reference_templates`.
- `aibusiness` is a near-verbatim copy with a different env namespace
  (`AIBUSINESS_*`), a different MCP port (8083 vs 8082), a different managed
  volume and a different overlay. Its `docker-compose.yml` still names the
  `langvm` image and container — an inherited inconsistency.
- Both implement the capability hook and declare `SNDBX_PROVIDES_POSTGRES:
  "true"`, so no host PostgreSQL companion service is injected.
- Readiness: `GET /readyz` on the MCP port, 45 attempts × 1 s.
- `managed_volumes` bind a Docker volume named `langvm-runtime-sndbx` /
  `aibusiness-runtime-sndbx` at `/var/lib/langvm`, so the guest's runtime
  state survives container recreation.

### 6.8 `open-webui` — chat frontend

- Base image `ghcr.io/open-webui/open-webui:main`, extended with `acl`,
  `attr`, `gosu`, `openssh-server`, `rsync`, `tini`.
- The packaged `openwebui` user (UID 1000) is remapped at start to
  `WEBUI_UID:WEBUI_GID` to match the host `data/` directory owner; an
  ownership-verified marker avoids repeating the sweep.
- `ENTRYPOINT tini → open-webui-entrypoint`, `CMD open-webui-start`.
- Published ports: UI 15080, SSH 15021. The guest listens on 8080.
- `read_only_rootfs` is not used; `/tmp` and `/run` are tmpfs.

### 6.9 `paperclip` — agent control plane

- Three-stage build: `source` clones a pinned upstream revision
  (`v2026.1012.0`), `builder` installs a pinned Rust toolchain and builds the
  UI / SDK / server workspaces, `production` assembles the runtime.
- The `node` user is remapped to `PAPERCLIP_UID:PAPERCLIP_GID`.
- The entrypoint additionally **secures the embedded PostgreSQL data**: it
  removes all ACLs recursively and sets `0700`/`0600` under
  `instances/default/db`.
- Published ports: UI 13100, SSH 12022. The guest listens on 3100.
- Several global AI CLIs are installed (Claude Code, Codex, opencode-ai,
  Gemini CLI, Kimi Code) for use by Paperclip's agents.

## 7. Adding a new image

### 7.1 Checklist

1. **Directory.** Create `images/<image-id>/` with a `Dockerfile`.
   `FROM` a base with a real userspace; add `openssh-server` if SSH access is
   wanted.
2. **Hook (optional).** Add `app.py`. Copy it in the Dockerfile:
   `COPY app.py /opt/sndbx-image/app.py`. It must be idempotent — it runs on
   every start.
3. **Persistent data (optional).** Create `images/<image-id>/data/` as the
   guest home. Mount it at the guest home path in the overlay.
4. **Overlay.** Create `images/<image-id>/sndbx.overlay.json5`:

   ```json5
   {
     sandboxes: {
       items: {
         "my-sandbox": {
           title: "My sandbox",
           image: "my-image:latest",
           memory: "2G",
           cpus: 2,
           persistent: true,
           shared_directories: [
             {
               host_path: "${SNDBX_ROOT}/images/my-image/data",
               guest_path: "/home/app",
               permission: "rw",
               source_type: "directory",
             }
           ]
         }
       }
     }
   }
   ```

5. **Register.** Add the sandbox id to the root `config.json5`:

   ```json5
   sandboxes: { items: { "my-sandbox": { run_at_startup: false } } }
   ```

6. **Restart** the manager. Overlay discovery will fail loudly if the id is
   not registered, if the directory does not match, or if the overlay declares
   a section other than `version` / `sandboxes` / `envids`.

### 7.2 Common pitfalls

| Pitfall | Consequence | Fix |
|---|---|---|
| The overlay declares a sandbox id that is not in the root | Startup fails with `OverlayError` | Register the id in the root. |
| The overlay is in the "wrong" directory | Startup fails with `OverlayError` | Set `local_image_id` in the root to the directory name. |
| The overlay declares `logging` or another global section | Startup fails | Overlays may only contain `version`, `sandboxes`, `envids`. |
| Two overlays declare the same sandbox id | Startup fails | One sandbox, one overlay. |
| The image is not Ubuntu Jammy | `apt` sources are rewritten to a Jammy mirror | Set `configure_apt_mirror: false`. |
| The hook is not idempotent | Breakage on every restart | Make every hook step idempotent. |
| The hook writes to a read-only mount | Failure | Prune mounted paths in ownership sweeps. |
| The Dockerfile uses a `USER` other than root | The hook still runs as root (`--user 0`), but the entrypoint may not | Design the entrypoint to work unprivileged. |

### 7.3 The `data/` directory and backups

- `data/` is **never** tracked by Git and is **protected** from upstream
  merges.
- The backup scheduler archives the whole `images/<id>/` directory,
  `data/` included.
- External mounts (paths outside `images/<id>/`) are **not** archived. If a
  guest's real data lives on an external mount, back it up separately.
- The restore procedure (full archive + all subsequent incrementals, in
  order) is in `README.md` and `proposal_backup.md`.
