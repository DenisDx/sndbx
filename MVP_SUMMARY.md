- **`src/app.py`**: Main application that orchestrates all services (MCP server, Web UI, VM management)

### 4.5. Service Management
- **`install.sh`**: Creates systemd service file (sndbx.service) and enables auto-start
- **`uninstall.sh`**: Removes systemd service and optionally cleans up installation
- Service runs as normal user with venv isolation
# sndbx MVP Summary

## What's Implemented

### 1. Configuration System
- **`.env`** and **`.env.example`**: Environment variables for runtime configuration
  - MCP server port/host
  - Token authentication
  - VM settings (memory, CPU, image)
  
- **`config.json5`**: Main project configuration with sections for:
  - Web UI (auth, users)
  - MCP server (port, bind, auth)
  - Sandboxes (VM definitions with resources)
  - Envids (mapping tokens to sandboxes)
  - Users (with token authentication)

### 2. Installation & Bootstrap
- **`install_prerequisites.sh`**: Prerequisites installer (Docker, Kata, Firecracker)
  - Idempotent checks for installed packages
  - Custom path support (--kata_path, --docker_path, --tmp_path)
  - Automatic Docker package conflict detection

- **`install.sh`**: Main installation script
  - Checks Python 3.11+, Docker, Kata, Firecracker
  - Creates project directories (shared, data, logs)
  - Sets up Python venv with dependencies
  - Optionally creates systemd service

- **`requirements.txt`**: Python dependencies (json5 for JSON5 parsing)

### 3. Python MCP Server
  - Reads .env files (environment variables)
  - Parses config.json5 with JSON5 support
  - Expands placeholders: `${VAR:-default}`

  - Create/start/stop/remove sandbox containers
  - Execute commands in running containers
  - Get container status and IP address
  - Uses Kata runtime for isolation

  - Async TCP server (default: 0.0.0.0:30081)
  - JSON-RPC request/response handling
  - Token-based authentication
  - Envid-to-sandbox resolution

  - `execute_command`: Run bash in sandbox
  - `read_file`: Read file from sandbox
  - `write_file`: Write file to sandbox
  - `sandbox_status`: Get VM status
  - `sandbox_start`: Start/create sandbox
  - `sandbox_stop`: Stop sandbox
  - **`src/tools.py`**: MCP tool handlers
    - `execute_command`: Run bash in sandbox
    - `read_file`: Read file from sandbox
    - `write_file`: Write file to sandbox
    - `sandbox_status`: Get VM status
    - `sandbox_start`: Start/create sandbox
    - `sandbox_stop`: Stop sandbox

  - **`src/app.py`**: Main application (core orchestrator)
    - Loads configuration
    - Initializes sandbox manager
    - Starts MCP server
    - Graceful shutdown on SIGINT/SIGTERM
    - Extensible for future services (Web UI, VM management)

  - Loads configuration
  - Initializes sandbox manager
  - Registers all tool handlers
  - Starts MCP server
### 4. Testing & Documentation
- **`test_mcp.sh`**: Test script for MCP endpoints
  - Test sandbox status
  - Test authentication
  - Test command execution

- **`README.md`**: Full documentation including:
  - Short description
  - Installation steps (7 sections)
  - MCP server testing guide (7 curl examples)
  - Configuration reference
  - Troubleshooting guide
  - Next steps

## Architecture

```
Host System
├── install_prerequisites.sh  (Kata + Docker setup)
├── install.sh               (Project bootstrap)
├── MCP Server (Python)
│   ├── config.py (loads .env + config.json5)
│   ├── sandbox.py (Docker API calls)
│   ├── mcp_server.py (TCP async server)
│   ├── tools.py (tool implementations)
│   └── mcp_server_main.py (entry point)
├── Docker Daemon
│   └── [Kata Runtime Registered]
│       └── Sandbox Containers (Ubuntu 22.04)
│           ├── /mnt/shared (shared_directories)
│           ├── SSH (future: port forwarding)
│           └── File I/O, CLI execution (via MCP tools)
```

## How to Use

### 1. Install Prerequisites
```bash
./install_prerequisites.sh [--kata_path /path] [--docker_path /path]
```

### 2. Install sndbx
```bash
chmod +x install.sh
./install.sh
```

### 3. Start MCP Server
```bash
source venv/bin/activate
python3 src/mcp_server_main.py
```

### 4. Test via curl
```bash
# Get sandbox status
curl -X POST http://localhost:30081 \
  -H "Content-Type: application/json" \
  -d '{
    "id": "1",
    "method": "sandbox_status",
    "token": "test-token-123456789",
    "envid": "default-env-token"
  }'
```
## How to Use

### 1. Install Prerequisites
```bash
./install_prerequisites.sh [--kata_path /path] [--docker_path /path]
```

### 2. Install sndbx
```bash
chmod +x install.sh
./install.sh
```
This creates a systemd service and enables auto-start.

### 3. Start sndbx

**Using systemd (recommended):**
```bash
sudo systemctl start sndbx    # system-wide
# or
systemctl --user start sndbx  # user service
```

**Running directly (development):**
```bash
source venv/bin/activate
python3 src/app.py
```

### 4. Test via curl
```bash
# Get sandbox status
curl -X POST http://localhost:30081 \
  -H "Content-Type: application/json" \
  -d '{
    "id": "1",
    "method": "sandbox_status",
    "token": "test-token-123456789",
    "envid": "default-env-token"
  }'
```

### 5. Uninstall
```bash
chmod +x uninstall.sh
./uninstall.sh
```

## Files Created

```
sndbx/
├── .env                     # Runtime configuration
├── .env.example            # Configuration template
├── config.json5            # Main project config
├── install_prerequisites.sh # Prerequisites installer
├── install.sh              # Main installer
├── requirements.txt        # Python dependencies
├── test_mcp.sh            # MCP test script
├── src/
│   ├── __init__.py
│   ├── config.py          # Config loader
│   ├── sandbox.py         # Sandbox manager
│   ├── mcp_server.py      # MCP server core
│   ├── tools.py           # Tool handlers
│   └── mcp_server_main.py # Server entry point
└── README.md              # Full documentation
```
## Files Created

```
sndbx/
├── .env                     # Runtime configuration
├── .env.example            # Configuration template
├── config.json5            # Main project config
├── install_prerequisites.sh # Prerequisites installer (Docker, Kata, Firecracker)
├── install.sh              # Main installer (creates venv, systemd service)
├── uninstall.sh            # Uninstaller (removes service, optional cleanup)
├── requirements.txt        # Python dependencies (json5)
├── test_mcp.sh            # MCP test script
├── src/
│   ├── __init__.py
│   ├── app.py             # Main application (orchestrator)
│   ├── config.py          # Config loader with JSON5 support
│   ├── sandbox.py         # Docker sandbox manager
│   ├── mcp_server.py      # MCP server (async TCP)
│   └── tools.py           # MCP tool handlers (6 tools)
├── README.md              # Full documentation
├── GETTING_STARTED.md     # Quick start guide
└── MVP_SUMMARY.md         # Architecture overview
```

## Status

✅ **MVP Complete**
- Docker containers with Kata runtime for VM-like isolation
- MCP server with token authentication
- File I/O and command execution tools
- Configuration via JSON5 + environment variables
- Full installation and testing documentation

## Future Work (Not in MVP)

- SSH access pool with port forwarding
- Web UI dashboard
- User and ACL system (beyond token auth)
- Persistent storage for VMs
- Container image registry integration
- GPU passthrough support
- Multi-VM orchestration
- State persistence across restarts
