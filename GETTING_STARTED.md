# Getting Started with sndbx MVP

This guide helps you get sndbx up and running.

## Prerequisites

- Ubuntu 22.04 or later
- ~5GB free disk space (or custom paths via --kata_path)
- Internet connection for downloading Docker images

## Installation Steps

### Step 1: Install System Prerequisites

```bash
cd sndbx
chmod +x install_prerequisites.sh
./install_prerequisites.sh
```

If disk space is limited, use custom paths:

```bash
./install_prerequisites.sh --kata_path /mnt/raid1/kata --docker_path /mnt/raid1/docker
```

### Step 2: Bootstrap sndbx

```bash
chmod +x install.sh
./install.sh
```

This will:
- Create Python virtual environment
- Install dependencies (json5, fastapi, uvicorn)
- Create project directories
- Optionally setup systemd service


### Step 3: Start sndbx Service

The sndbx service is now managed by systemd. Start it with:

**System-wide service (if installed with sudo):**
```bash
sudo systemctl start sndbx
sudo systemctl status sndbx
```

**User service:**
```bash
systemctl --user start sndbx
systemctl --user status sndbx
```

View logs:
```bash
# System service
sudo journalctl -u sndbx -f

# User service
journalctl --user -u sndbx -f
```

**Or run directly (for development):**
```bash
source venv/bin/activate
python3 src/app.py
```

### Step 4: Open Web UI

- URL: http://127.0.0.1:30080 (or values from WEBUI_HOST/WEBUI_PORT)
- Login/password: from webui.auth.users in config.json5
- Default credentials: admin / changeme

## Testing the MCP Server

In a separate terminal:

### Method 1: Using test script

```bash
cd sndbx
./test_mcp.sh
```

### Method 2: Check service status

```bash
# Check if service is running
sudo systemctl status sndbx    # system service
# or
systemctl --user status sndbx  # user service

# View logs
sudo journalctl -u sndbx -f
# or
journalctl --user -u sndbx -f
```

### Method 3: Manual curl tests

**Get sandbox status:**
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

**Start sandbox:**
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

**Execute command:**
```bash
curl -X POST http://localhost:30081 \
  -H "Content-Type: application/json" \
  -d '{
    "id": "3",
    "method": "execute_command",
    "token": "test-token-123456789",
    "envid": "default-env-token",
    "params": {
      "command": "whoami && pwd && uname -a"
    }
  }'
```

## Configuration

### .env File

Edit `.env` to customize:
- `MCP_PORT`: MCP server port (default: 30081)
- `MCP_HOST`: MCP server bind address (default: 0.0.0.0)
- `MCP_TOKENS`: Authentication tokens
- VM settings: `VM_MEMORY`, `VM_CPUS`, `VM_IMAGE`

### config.json5 File

Define sandboxes and environment configurations:
```json5
sandboxes: {
  items: {
    "my-vm": {
      image: "ubuntu:22.04",
      memory: "4G",
      cpus: 4,
      disk_max: "40G",
      network_traffic_max: "200G", // planned, not enforced yet
      shared_directories: [
        {
          host_path: "/tmp/shared",
          guest_path: "/mnt/shared",
          permission: "rw"
        }
      ]
    }
  }
}
```

## Troubleshooting

### MCP server won't start

1. Check Python version: `python3 --version` (needs 3.11+)
2. Verify dependencies: `pip list | grep json5`
3. Check if port is in use: `lsof -i :30081`

### Sandbox creation fails

1. Verify Kata runtime: `kata-runtime --version`
2. Test Docker: `docker ps`
3. Check Kata is registered: `docker run --rm --runtime kata alpine echo "test"`
4. Verify Kata config exists: `test -f /etc/kata-containers/configuration.toml && echo KATA_CFG_OK`
5. If command exits with `125` and mentions missing `configuration.toml`, copy it from Kata defaults and restart Docker

### Authentication fails

1. Verify token in request matches `.env` `MCP_TOKENS`
2. Check token is not empty or whitespace
3. For multiple tokens, separate with commas (no spaces)

### Config loading fails

1. Verify JSON5 syntax: `python3 -c "import json5; json5.load(open('config.json5'))"`
2. Check all ${VAR} placeholders are defined in `.env`
3. Look for parse errors in the output

## Architecture

```
┌──────────────────────────────────┐
│  systemd / direct execution      │
└──────────────┬───────────────────┘
     │
     ↓
┌──────────────────────────────────┐
│  sndbx Application (app.py)       │ ← orchestrates services
│  - Config loader                 │
│  - MCP Server (async)            │
│  - Sandbox Manager               │
│  - (Future: Web UI backend)      │
└──────────────┬───────────────────┘
     │
  ┌───────┴────────┬────────────┐
  │                │            │
  ↓                ↓            ↓
┌─────────────┐ ┌──────────────┐ ┌───────────┐
│ curl/client │ │ Web Browser  │ │ Direct    │
│             │ │ (future)     │ │ API calls │
└──────┬──────┘ └──────┬───────┘ └─────┬─────┘
  │ JSON-RPC      │ HTTP            │
  └───────────────┼────────────────┘
        │
        ↓
┌─────────────────────────────────┐
│  MCP Server / Web Backend       │ ← token auth
│  (Python asyncio)               │
└──────┬──────────────────────────┘
       │ Docker API
       ↓

┌─────────────────────────────────┐
│  Docker Daemon (docker-ce)      │
│  with Kata runtime              │
└──────┬───────────────────────────┘
  │ OCI spec
  ↓
┌─────────────────────────────────┐
│  Kata Containers (OCI runtime)  │
└──────┬───────────────────────────┘
  │
  ↓
┌─────────────────────────────────┐
│  Firecracker (microVM)          │
│  with Container Filesystem      │
│  (Ubuntu 22.04)                 │
└─────────────────────────────────┘
```
## MCP Tools Reference

| Tool | Params | Returns |
|------|--------|---------|
| `sandbox_status` | (none) | {id, running, container_id, ip, error} |
| `sandbox_start` | (none) | {success, message, sandbox_id} |
| `sandbox_stop` | (none) | {success, message, sandbox_id} |
| `execute_command` | command | {success, output, sandbox_id} |
| `read_file` | path | {success, content, path, sandbox_id} |
| `write_file` | path, content | {success, path, message, sandbox_id} |

## Next Steps

1. See [README.md](README.md) for detailed MCP examples
2. See [MVP_SUMMARY.md](MVP_SUMMARY.md) for architecture overview
3. Configure custom sandboxes in config.json5
4. Add more tokens in `.env` for multiple users
5. Setup systemd service for automatic startup

## Support

For issues or questions:
1. Check README.md troubleshooting section
2. Review log files in `logs/`
3. Check system logs: `journalctl -u docker`
4. Verify Kata installation: `kata-runtime --version`
