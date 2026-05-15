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
- Installs Kata only when missing (to `--kata_path` if specified)
- Verifies SHA256 checksum of the downloaded Kata archive
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
Ubuntu repositories may not provide a `kata-containers` package, so install the official static release.

```bash
ARCH=$(uname -m)
if [[ "$ARCH" == "x86_64" ]]; then KATA_ARCH="amd64";
elif [[ "$ARCH" == "aarch64" ]]; then KATA_ARCH="arm64";
else echo "Unsupported arch: $ARCH"; exit 1; fi

KATA_VER=$(curl -fsSL https://api.github.com/repos/kata-containers/kata-containers/releases/latest | jq -r '.tag_name' | sed 's/^v//')
curl -fL -o /tmp/kata-static.tar.zst "https://github.com/kata-containers/kata-containers/releases/download/${KATA_VER}/kata-static-${KATA_VER}-${KATA_ARCH}.tar.zst"
sudo tar --zstd -xvf /tmp/kata-static.tar.zst -C /
sudo ln -sf /opt/kata/bin/kata-runtime /usr/local/bin/kata-runtime
sudo ln -sf /opt/kata/bin/containerd-shim-kata-v2 /usr/local/bin/containerd-shim-kata-v2
kata-runtime --version
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
jq '.runtimes.kata = {"path":"/usr/local/bin/kata-runtime"}' /etc/docker/daemon.json > "$tmpfile"
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
kata-runtime --version
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
- Dashboard: health checks (docker, kata, webui), managed container list, SSH port display, actions
- Settings: placeholder page
- Console: xterm.js terminal with sandbox selector and connect/disconnect controls

Dashboard actions:
- Start / Stop / Restart sandbox container
- Open SSH / Close SSH (port reservation from configured ssh.port_range)

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

Lifecycle hook (`app.py`):
- If `images/<image_id>/app.py` exists, sndbx executes it inside container as:
  - `python3 /opt/sndbx-image/app.py`
  - with env `SNDBX_HOOK=on_system_start`
- Hook is called after successful create/start of a sandbox container.

Web UI dashboard includes a "Local images" panel with Build/Rebuild/Update buttons.

## Troubleshooting

### MCP Server won't start

- Check if Python 3.11+ is installed: `python3 --version`
- Verify .env and config.json5 are valid: `python3 -c "from src.config import load_config; print(load_config())"`
- Check if port is in use: `lsof -i :30081` or `netstat -tuln | grep 30081`

### Sandbox creation fails

- Verify Kata runtime is available: `kata-runtime --version`
- Check Docker daemon: `docker ps`
- Inspect Docker logs: `sudo journalctl -u docker -f`
- Test manual Docker run: `docker run --rm --runtime kata alpine echo "test"`
- If `docker run --runtime kata ...` fails with `Cannot find usable config file` or exit code `125`, restore `/etc/kata-containers/configuration.toml` from Kata defaults (see "Verify Installation" section above)

### apt commands inside sandbox are very slow

**Symptom**: `apt-get update` or `apt install` takes several minutes inside the sandbox.

**Root cause**: The default `archive.ubuntu.com` repository may be throttled or blocked on your network (commonly observed in Asia and some corporate environments). Network traffic is routed through the Kata VM's virtual NIC with NAT, so the bottleneck is the repository connection speed, not virtiofs or disk I/O.

**Fix applied automatically**: sndbx configures `mirrors.aliyun.com` as the apt mirror in every newly created sandbox (`/etc/apt/sources.list` is rewritten at container creation time). This brings `apt-get update` from ~2.5 minutes down to ~11 seconds and `apt install python3` from several minutes to ~20 seconds on affected hosts.

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
