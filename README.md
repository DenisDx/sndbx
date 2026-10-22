# sndbx

## Short Description
sndbx is a safe and isolated sandbox platform with MCP access. It uses Docker + Kata + Firecracker to provide VM-like execution environments, CLI access, file operations, and management through Web UI and MCP tools.

sndbx is a part of an ecosystem that enables the construction of hybrid multi-agent AI systems, but can also be used autonomously

![system scheme](system_scheme.svg)

Possible applications:

As part of an automated multi-agent system ( e.g., OpenClaw or Aidir :-) ) providing access via the MCP protocol to
- a console (cli) with full access - installing and running applications, developing applications, work with files etc.

- used via Aidir Tools Injection - allows replacing a model with a system with search tools, development tools with script execution, and adding file management

- serves as secure file storage with access via MCP, ftp, smb etc

Using this system, you can easily provide each agent with its own (or shared) sandbox, in which it will have full root access (but you may limit it if needed). Restrictions can be imposed at the virtual machine level, such as maximum used disk space, CPU load, and so on.
Non-persistent machines can be used, meaning that after finishing work, the virtual machine will return to its fresh state.

The use of individual per-agent virtual environments partially solves the problem of promt injection, which is important for externally open systems (such as colloc) - the system will be able to perform complex operations up to the development and execution of program code, work with files, etc. - but even if the user convinces the system to execute the `sudo rm -rf /` command, after the conversation with him ends, everything will return to its original state.

Moreover, this allows running in one environment (and even on one physical computer) multiple independent multi-agent systems - "work groups", each of whose "employees" has their own computer, their own files, can work with shared files, etc.

## Installation
Follow the steps below on Ubuntu 22.04+.

### 1. Install Prerequisites
Install system packages required by sndbx and by the installation process.

Recommended way (idempotent script with version conflict checks):

```bash
chmod +x install_prerequisites.sh
./install_prerequisites.sh
```

Custom install paths example:

```bash
./install_prerequisites.sh --kata_path /mnt/raid1/kata --docker_path /mnt/raid1/docker
```

Use pre-downloaded Kata archive (offline/slow-network install):

```bash
./install_prerequisites.sh --kata_archive /path/to/kata-static-3.x.y-amd64.tar.zst
```

Use a fast apt mirror for host package installation (useful in China or corporate networks):

```bash
./install_prerequisites.sh --apt_mirror http://mirrors.aliyun.com/ubuntu
```

The script behavior:
- Verifies sudo access before doing anything
- Detects snap-installed Docker and warns about potential conflicts
- Checks KVM availability early with platform-specific hints (VMware, Hyper-V, VirtualBox, nested KVM, containers)
- Installs only missing packages
- Skips Docker package install if Docker is already present (including snap, `docker-ce`, `docker.io`)
- Enables/starts Docker only when needed
- Adds current user to `docker` group only when missing
- Installs pinned Kata 4.1 runtime-rs only when missing (to `--kata_path` if specified)
- Verifies a pinned SHA256 checksum for the downloaded or supplied Kata archive
- Resumes interrupted downloads automatically (no need to restart from scratch)
- Sets Docker data directory (`data-root`) to `--docker_path` if specified
- Uses `--tmp_path` for temporary download/extraction (default: `/tmp`)
- Stops with an error if installed versions are older than supported minimums
- Prints a numbered step-by-step progress log
- Prints startup summary and waits for `Y` confirmation (Enter cancels)

Manual commands (if you prefer step-by-step setup):

```bash
sudo apt update
sudo apt install -y \
	python3 python3-venv python3-pip \
	docker.io \
	curl jq tar zstd \
	qemu-system-x86 qemu-utils cpu-checker
```

Enable and start Docker (if Docker was not installed):

```bash
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

Log out and back in after adding your user to the docker group.

### 2. Verify Virtualization Support

```bash
sudo modprobe kvm
sudo modprobe kvm_intel || sudo modprobe kvm_amd
kvm-ok
```

If `kvm-ok` reports nested virtualization issues, enable nested mode on the host before continuing.

### 3. Install Kata Containers
`install_prerequisites.sh` installs the supported Kata 4.1 static release and
registers Docker runtime `kata`. It uses the runtime-rs shim and configuration
at `/etc/kata-containers/runtime-rs/configuration.toml`; do not replace it with
the deprecated `kata-runtime` binary setup. Run the prerequisite installer
instead of downloading an unpinned latest release manually:

```bash
./install_prerequisites.sh
```

### 4. Install Firecracker
Install Firecracker from the official release archive.

```bash
ARCH=$(uname -m)
if [[ "$ARCH" == "x86_64" ]]; then FC_ARCH="x86_64";
elif [[ "$ARCH" == "aarch64" ]]; then FC_ARCH="aarch64";
else echo "Unsupported arch: $ARCH"; exit 1; fi

FC_VER=$(curl -fsSL https://api.github.com/repos/firecracker-microvm/firecracker/releases/latest | jq -r '.tag_name')
curl -fL -o /tmp/firecracker.tgz "https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VER}/firecracker-${FC_VER}-${FC_ARCH}.tgz"
tar -xvf /tmp/firecracker.tgz -C /tmp
sudo install -m 0755 "/tmp/release-${FC_VER}-${FC_ARCH}/firecracker-${FC_VER}-${FC_ARCH}" /usr/local/bin/firecracker
sudo install -m 0755 "/tmp/release-${FC_VER}-${FC_ARCH}/jailer-${FC_VER}-${FC_ARCH}" /usr/local/bin/jailer
firecracker --version
```

Note: the Kata bundle also ships `/opt/kata/bin/firecracker`, but installing to `/usr/local/bin` makes runtime selection explicit.

### 5. Configure Docker to Use Kata Runtime
Register Kata runtime in Docker daemon config.

```bash
sudo mkdir -p /etc/docker
if [[ ! -f /etc/docker/daemon.json ]]; then
	echo '{}' | sudo tee /etc/docker/daemon.json >/dev/null
fi

tmpfile=$(mktemp)
jq '.runtimes.kata = {"runtimeType":"/opt/kata/runtime-rs/bin/containerd-shim-kata-v2","options":{"ConfigPath":"/etc/kata-containers/runtime-rs/configuration.toml"}}' /etc/docker/daemon.json > "$tmpfile"
sudo mv "$tmpfile" /etc/docker/daemon.json
sudo systemctl restart docker
```

### 6. Install sndbx

```bash
git clone <your-sndbx-repo-url> sndbx
cd sndbx
chmod +x install.sh
./install.sh
```

If `.env` does not exist, create it from `.env.example` before running the service:

```bash
cp .env.example .env
```

### 7. Verify Installation

```bash
/opt/kata/runtime-rs/bin/containerd-shim-kata-v2 --version
firecracker --version
docker info | grep -A5 Runtimes
```

Required Kata readiness checks (must pass before starting sandboxes):

```bash
test -f /etc/kata-containers/configuration.toml && echo KATA_CFG_OK
docker info --format '{{json .Runtimes}}' | grep -q '"kata"' && echo KATA_RUNTIME_OK
docker run --rm --runtime kata alpine echo OK
```

If the last command fails with exit code `125` and mentions missing `configuration.toml`, restore it from Kata defaults:

```bash
CFG_SRC="$(for f in \
  /opt/kata/share/defaults/kata-containers/configuration.toml \
  /opt/kata/share/defaults/kata-containers/configuration-qemu.toml \
  /opt/kata/share/defaults/kata-containers/configuration-fc.toml \
  /opt/kata/share/defaults/kata-containers/configuration-clh.toml \
  /mnt/raid1/kata/share/defaults/kata-containers/configuration.toml \
  /mnt/raid1/kata/share/defaults/kata-containers/configuration-qemu.toml \
  /mnt/raid1/kata/share/defaults/kata-containers/configuration-fc.toml \
  /mnt/raid1/kata/share/defaults/kata-containers/configuration-clh.toml
do
  [ -f "$f" ] && echo "$f" && break
done)"

echo "Using config source: $CFG_SRC"
sudo mkdir -p /etc/kata-containers
sudo cp "$CFG_SRC" /etc/kata-containers/configuration.toml
sudo systemctl restart docker
```

Then open Web UI on the configured host and port from `.env` and `config.json5`.

## MCP Server Testing

The MCP (Model Context Protocol) server runs on the port configured in `.env` (default: `30081`).

### HTTP/SSE Endpoints (Current Transport)

sndbx MCP now exposes HTTP endpoints on the MCP port:

- `POST /`
- `POST /mcp`
- `POST /mcp/v1`

Legacy compatibility endpoints:

- `GET /sse`
- `POST /messages`

All examples below use the default token from `config.json5`.

Basic tools list via `POST /mcp`:

```bash
curl -X POST http://127.0.0.1:30081/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer test-token-123456789" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/list"
  }'
```

The same request via root endpoint `POST /`:

```bash
curl -X POST http://127.0.0.1:30081/ \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer test-token-123456789" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list"
  }'
```

Versioned endpoint `POST /mcp/v1`:

```bash
curl -X POST http://127.0.0.1:30081/mcp/v1 \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer test-token-123456789" \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/list"
  }'
```

Streamable HTTP mode (SSE response stream):

```bash
curl -N -X POST 'http://127.0.0.1:30081/mcp?stream=1' \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -H "Authorization: Bearer test-token-123456789" \
  -d '{
    "jsonrpc": "2.0",
    "id": 10,
    "method": "tools/list"
  }'
```

Legacy SSE handshake:

```bash
curl -N 'http://127.0.0.1:30081/sse?session_id=demo-session'
```

Legacy message publish:

```bash
curl -X POST 'http://127.0.0.1:30081/messages?session_id=demo-session' \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer test-token-123456789" \
  -d '{
    "jsonrpc": "2.0",
    "id": 11,
    "method": "tools/list",
    "session_id": "demo-session"
  }'
```


### 1. Start sndbx Application

The sndbx application manages all services including the MCP server.

**Using systemd service (recommended):**
```bash
sudo systemctl start sndbx     # System-wide
# or
systemctl --user start sndbx   # User service
```

**Running directly (for development):**
```bash
source venv/bin/activate
python3 src/app.py
```

## Web UI (v1)

Web UI is served by the same core app on WEBUI_HOST:WEBUI_PORT.

- Default URL: http://127.0.0.1:30080
- Auth mode: login/password from config.json5 webui.auth.users
- Session cookie: sndbx_session (HttpOnly; Secure when HTTPS)
- Session files: sessions/<sha256(token)>.json

Implemented tabs in v1:
- Dashboard: health checks (docker, kata, webui), managed container list, SSH port display, actions, per-VM runtime monitoring
- Settings: root configuration and per-image overlay editor (aiset)
- Console: xterm.js terminal with sandbox selector and connect/disconnect controls

Dashboard actions:
- Start / Stop / Restart sandbox container
- Open SSH / Close SSH (port reservation from configured ssh.port_range)

Dashboard runtime monitoring:
- Each container row has an independent **Monitor** switch. All switches are
  off when the page is opened; no metrics are collected until the user enables
  one. The selection is not persisted across page reloads.
- While monitoring is on and the Dashboard is visible, sndbx displays memory,
  CPU, host disk information, a health badge, and a 10-minute in-memory
  history. Data is sampled every 5 seconds through `docker stats`.
- Monitor details are equal-height blocks arranged horizontally: Memory, CPU,
  Disk, top processes by memory, and top processes by CPU. The strip scrolls
  horizontally when the window is too narrow for all blocks. Drag the bottom
  handle to change the detail height; drag a block's lower-right handle to
  change its width. Double-click a width handle to reset it.
- A read-only `docker exec` probe runs every 15 seconds for each monitored VM.
  It reads `MemAvailable`, OOM counters, swap, uptime, and the top five
  processes by memory and CPU. Process names are shown; command arguments are
  not collected.
- Guest memory pressure and host memory pressure are reported separately, so a
  VM with a too-small memory allocation can be distinguished from a host-wide
  shortage.
- Monitoring stores metrics only in memory. It has no background sampler, no
  log entries, no browser notifications, and no persistent history.
- The endpoint is authenticated `GET /api/monitor?ids=<sandbox-id>,...`.
- Disk monitoring is best effort: it shows host free space and the Docker
  writable-layer size (`SizeRw`). Under the current Kata/VirtioFS setup, a
  reliable per-VM disk percentage or `disk_max` quota is not exposed.

Console transport:
- Frontend: xterm.js
- Backend: authenticated WebSocket endpoint `/ws/console/{sandbox_id}`
- Runtime: PTY bridge to `docker exec -i sndbx-<sandbox_id> bash`
### 2. Get Sandbox Status

Check if a sandbox is running:

```bash
curl -X POST http://localhost:30081 \
  -H "Content-Type: application/json" \
  -d '{
    "id": "1",
    "method": "sandbox_status",
    "token": "test-token-123456789",
    "envid": "default-env-token"
  }'
```

Expected response:
```json
{
  "id": "1",
  "result": {
    "id": "sandbox-1",
    "running": false,
    "container_id": null,
    "ip": null,
    "error": "Container not found or not running"
  }
}
```

### 3. Start a Sandbox

Create and start a sandbox VM:

```bash
curl -X POST http://localhost:30081 \
  -H "Content-Type: application/json" \
  -d '{
    "id": "2",
    "method": "sandbox_start",
    "token": "test-token-123456789",
    "envid": "default-env-token"
  }'
```

Note: The sandbox will be created automatically if it doesn't exist, or started if it does.

### 4. Execute Command in Sandbox

Run a bash command inside the running sandbox:

```bash
curl -X POST http://localhost:30081 \
  -H "Content-Type: application/json" \
  -d '{
    "id": "3",
    "method": "execute_command",
    "token": "test-token-123456789",
    "envid": "default-env-token",
    "params": {
      "command": "whoami && pwd && ls -la /"
    }
  }'
```

Expected response (if sandbox is running):
```json
{
  "id": "3",
  "result": {
    "success": true,
    "output": "root\n/\nbin/ boot/ dev/ etc/ ...",
    "sandbox_id": "sandbox-1"
  }
}
```

### 5. Read File from Sandbox

Read a file inside the sandbox:

```bash
curl -X POST http://localhost:30081 \
  -H "Content-Type: application/json" \
  -d '{
    "id": "4",
    "method": "read_file",
    "token": "test-token-123456789",
    "envid": "default-env-token",
    "params": {
      "path": "/etc/hostname"
    }
  }'
```

### 6. Write File to Sandbox

Write a file inside the sandbox:

```bash
curl -X POST http://localhost:30081 \
  -H "Content-Type: application/json" \
  -d '{
    "id": "5",
    "method": "write_file",
    "token": "test-token-123456789",
    "envid": "default-env-token",
    "params": {
      "path": "/tmp/hello.txt",
      "content": "Hello from sndbx!"
    }
  }'
```

### 7. Stop a Sandbox

Stop and remove the sandbox:

```bash
curl -X POST http://localhost:30081 \
  -H "Content-Type: application/json" \
  -d '{
    "id": "6",
    "method": "sandbox_stop",
    "token": "test-token-123456789",
    "envid": "default-env-token"
  }'
```

### Error Responses

If authentication fails:

```bash
curl -X POST http://localhost:30081 \
  -H "Content-Type: application/json" \
  -d '{
    "id": "err1",
    "method": "sandbox_status",
    "token": "invalid-token"
  }'
```

Response:
```json
{
  "id": "err1",
  "error": "Invalid or missing token"
}
```

If the tool doesn't exist:

```json
{
  "id": "err2",
  "error": "Unknown tool: unknown_method"
}
```

## Configuration

### .env File

The `.env` file contains environment variables:
- `SNDBX_ROOT`: Installation directory
- `LOG_LEVEL`: Logging verbosity (debug, info, warning, error)
- `MCP_PORT`: MCP server listen port (default: 30081)
- `MCP_HOST`: MCP server bind address (default: 0.0.0.0)
- `MCP_TOKENS`: Comma-separated authentication tokens

Edit `.env` to customize settings:

```bash
nano .env
# Change MCP_PORT, MCP_HOST, MCP_TOKENS, etc.
# Then restart the MCP server
```

### config.json5 File

The `config.json5` file defines sandboxes, users, and MCP settings:

```json5
{
  sandboxes: {
    items: {
      "sandbox-1": {
        image: "ubuntu:22.04",
        memory: "2G",
        cpus: 2,
        disk_max: "20G",
        network_traffic_max: "100G", // planned, not enforced yet
        shared_directories: [
          {
            host_path: "/tmp/sndbx-shared",
            guest_path: "/mnt/shared",
            permission: "rw"
          }
        ]
      }
    }
  },

  envids: {
    "default-env-token": {
      sandbox: "sandbox-1",
      description: "Default test environment"
    }
  }
}
```

### Settings Library (aiset)

Configuration is owned by the pinned [`aiset`](https://github.com/DenisDx/aiset)
submodule at `11d8838efe9e5cc447a9211c655a431fab810e8e` (`0.1.0`). Do not edit
files inside `aiset/` from this repository; advance the submodule instead.

Two kinds of document are loaded:

- `config.json5` is the root registry, the global settings and the override
  layer.
- `images/<image-dir>/sndbx.overlay.json5` holds the defaults for the sandboxes
  built from that image. One file may cover several sandbox ids (for example,
  `images/default_sandbox_1/sndbx.overlay.json5` covers `sandbox-1` and
  `default-sandbox-1`).

Merge precedence is the same as the aidir worker config: the root wins every
conflict, objects deep-merge, and keys present only in an overlay are added.
A root array replaces the overlay array as a whole. To let an overlay supply a
value, remove the key from the root (an empty root array or a root `false` still
wins).

Schemas live in `config.descriptor.json5` and `config.types.json5`. Unknown keys
are errors: update the descriptor before adding a new configuration key.

The Web UI **Settings** tab edits the root and every overlay. Changes that
require a restart are reported as `restart_required`; use the Restart button.
Logging and Auto-start apply without a restart.

`data/sandbox_startup_overrides.json` is legacy. On the first aiset start it is
imported into `config.json5` and renamed to `.migrated`.

Design: `proposal_aiset_integration.md`. Delivery status, deviations and
remaining operational steps: `execution_aiset_integration.md`.

### Local Sandbox Images (images/<id>)

You can define local sandbox images in project folders:

```text
images/<image_id>/
  Dockerfile
  app.py          # optional lifecycle hook script
  ...other files
```

How it works:
- If `sandboxes.items.<sandbox_id>.image` points to `<image_id>` and `images/<image_id>/Dockerfile` exists, sndbx can build the image locally.
- Auto-build is triggered only when sandbox creation is needed and the Docker image is missing.
- Existing containers keep normal start/stop behavior (no forced rebuild on startup).
- Set `configure_apt_mirror: false` on images that must retain their native package sources, such as Debian-based images. The default remains `true` for backward compatibility.

Lifecycle hook (`app.py`):
- If `images/<image_id>/app.py` exists, sndbx executes it inside container as:
  - `python3 /opt/sndbx-image/app.py`
  - with env `SNDBX_HOOK=on_system_start`
- Hook is called after successful create/start of a sandbox container.

Web UI dashboard includes a "Local images" panel with Build/Rebuild/Update buttons.

### Per-VM Local Image Backups

sndbx can run an internal incremental backup scheduler for local image directories. The scheduler runs inside the existing `sndbx.service` process; it does not use cron, systemd timers, Docker image export, or a separate daemon. The feature is opt-in per sandbox.

When a virtual machine creates files under a different host UID or GID, install
the non-interactive root backup helper once as an administrator:

```bash
sudo ./install_backup_acl_helper.sh <sndbx-service-user>
```

The standard installer offers this step interactively. For unattended
installation, enable it explicitly:

```bash
INSTALL_BACKUP_ACL_HELPER=1 ./install.sh
```

It installs a root-owned helper and a sudoers rule that permits only that
helper for the service user. The scheduler uses `sudo -n` to invoke the helper
without a password prompt. The helper reads only the configured local image,
writes only its configured backup directory, and maintains root-owned
incremental snapshot state under `/var/lib/sndbx-backup/`. It never changes
ACLs or permissions in the virtual-machine image. If provisioning is absent
or fails, no archive is created and the error identifies the failed helper.

Live backups are file-level, not application-consistent. The helper uses GNU
tar's `--ignore-failed-read` so a file changed by a running VM does not discard
the whole archive; a database that changes while read can be omitted or
captured at an arbitrary point in time.

Enable it in `config.json5`:

```json5
sandboxes: {
  items: {
    "sandbox-1": {
      image: "demo",
      local_image_id: "demo",
      backup: {
        enabled: true,
        interval_hours: 24,
        full_every: 7,
      },
    },
  },
},
```

| Parameter | Type | Meaning |
|---|---:|---|
| `enabled` | boolean | Enables the scheduler for this sandbox. Default: `false`. |
| `interval_hours` | positive integer | Time between backup runs. |
| `full_every` | positive integer | Create a new full backup after this many backup intervals. |

The initial run always creates a full backup. Invalid backup settings are reported in the sndbx log and skipped; they must not stop the rest of sndbx.

#### What gets backed up

The scheduler archives the image source directory `images/<local_image_id>/`, which includes its persistent `data/` directory. It does **not** archive Docker storage under `/var/lib/docker`, Docker containers, or external bind mounts outside the image directory.

For example, the Obsidian vault and `Agents_shared` are external mounts and are intentionally not included in an `Assistant` image backup.

#### Archive layout

Each local image gets an isolated backup directory:

```text
backup/
  denis_obsidian_assistant/
    denis_obsidian_assistant-2026-10-10T110000Z-full.tar.gz
    denis_obsidian_assistant-2026-10-11T110000Z-incremental.tar.gz
    denis_obsidian_assistant.snar
```

The directory name is `local_image_id`, not a sandbox ID or Docker image tag. This keeps the archive location stable when a sandbox ID or tag changes. Archive names use UTC timestamps and contain either `full` or `incremental`. The snapshot file (`.snar`) is private scheduler state.

The scheduler does not stop a sandbox before creating an archive, so an archive is a file-level backup, not an application-consistent database snapshot.

#### Restore procedure

Restore must occur into an empty target directory, not over a running image directory.

1. Extract the most recent required full archive together with its `.snar` file using GNU tar.
2. Apply every incremental archive after that full archive in chronological order.
3. Validate ownership and permissions before using the restored image.
4. Build the image and create a new sandbox from the restored directory.

The full archive and every subsequent incremental archive are required. Losing an archive breaks restoration of later changes in that chain.

A typical restore:

```bash
# Stop any sandbox that uses this image and remove its container first.
restore=/var/tmp/sndbx-restore
mkdir -p "$restore"
tar --extract --gzip \
  --listed-incremental="$restore/snapshot.snar" \
  --file="$SNDBX_ROOT/backup/<local_image_id>/<local_image_id>-<UTC>-full.tar.gz" \
  --directory="$restore"
for incremental in "$SNDBX_ROOT/backup/<local_image_id>/<local_image_id>-<UTC>-incremental.tar.gz"; do
  tar --extract --gzip \
    --listed-incremental="$restore/snapshot.snar" \
    --file="$incremental" \
    --directory="$restore"
done
# Replace the broken images/<local_image_id> tree with the restored contents
# and rebuild the image.
```

## Troubleshooting

### MCP Server won't start

- Check if Python 3.11+ is installed: `python3 --version`
- Verify .env and config.json5 are valid: `python3 -c "from src.config import load_config; print(load_config())"`
- Check if port is in use: `lsof -i :30081` or `netstat -tuln | grep 30081`

### Sandbox creation fails

- Verify Kata runtime is available: `/opt/kata/runtime-rs/bin/containerd-shim-kata-v2 --version`
- Check Docker daemon: `docker ps`
- Inspect Docker logs: `sudo journalctl -u docker -f`
- Test manual Docker run: `docker run --rm --runtime kata alpine echo "test"`
- If `docker run --runtime kata ...` fails with `Cannot find usable config file` or exit code `125`, restore `/etc/kata-containers/runtime-rs/configuration.toml` from the Kata 4.1 runtime-rs QEMU default

### apt commands inside sandbox are very slow

**Symptom**: `apt-get update` or `apt install` takes several minutes inside the sandbox.

**Root cause**: The default `archive.ubuntu.com` repository may be throttled or blocked on your network (commonly observed in Asia and some corporate environments). Network traffic is routed through the Kata VM's virtual NIC with NAT, so the bottleneck is the repository connection speed, not virtiofs or disk I/O.

**Fix applied automatically**: by default, sndbx configures `mirrors.aliyun.com` as the apt mirror in newly created sandboxes (`/etc/apt/sources.list` is rewritten at container creation time). This brings `apt-get update` from ~2.5 minutes down to ~11 seconds and `apt install python3` from several minutes to ~20 seconds on affected hosts. Set `configure_apt_mirror: false` in a sandbox config when the image is not Ubuntu Jammy or must retain its native package sources.

**For existing sandboxes** (already created before this fix was applied), reconfigure manually:

```bash
docker exec sndbx-<sandbox_id> bash -c '
cat > /etc/apt/sources.list << "EOF"
deb http://mirrors.aliyun.com/ubuntu jammy main restricted universe multiverse
deb http://mirrors.aliyun.com/ubuntu jammy-updates main restricted universe multiverse
deb http://mirrors.aliyun.com/ubuntu jammy-backports main restricted universe multiverse
deb http://mirrors.aliyun.com/ubuntu jammy-security main restricted universe multiverse
EOF
'
```

Or recreate the sandbox via Web UI / MCP to have the mirror applied automatically.

**To use a different mirror**, change `APT_MIRROR` and `APT_SECURITY_MIRROR` in [src/sandbox.py](src/sandbox.py) and restart the service.

### Token authentication fails

- Verify token in `.env` and MCP request match exactly
- Check `MCP_TOKENS` in `.env` contains the token being used
- Note: Tokens are comma-separated, no spaces

## Next Steps

The MVP includes basic infrastructure:
- Docker containers with Kata runtime for isolation
- MCP server for remote management
- Configuration via .env and config.json5

Future enhancements:
- Web UI dashboard for VM management
- SSH access pool and port forwarding
- User and ACL system
- Persistent storage for VMs
- Container image registry integration
