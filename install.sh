#!/bin/bash
# sndbx installation script
# Sets up configuration and creates necessary directories

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Script directory (needed early for config template logic below)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DOCKER_SUDO_FALLBACK_USED=false

# ---------------------------------------------------------------------------
# Config template handling
# Usage: ./install.sh [config-name]
#   config-name  name or path of .json5.example template to apply (default: "config").
#                Extension ".json5.example" is appended if not present.
# When the parameter is given explicitly OR config.json5 is absent, the script
# rotates the current config.json5 into up to three .bak backups and copies the
# chosen template as the new config.json5.
# ---------------------------------------------------------------------------
CONFIG_PARAM="${1:-}"
CONFIG_PARAM_EXPLICIT=false
if [[ -n "$CONFIG_PARAM" ]]; then
    CONFIG_PARAM_EXPLICIT=true
fi

# Resolve template file path
resolve_config_template() {
    local name="${1:-config}"
    # Strip any trailing ".json5.example" / ".json5" so we can normalise.
    name="${name%.json5.example}"
    name="${name%.json5}"
    local resolved="${SCRIPT_DIR}/${name}.json5.example"
    echo "$resolved"
}

# Rotate existing config.json5 backups (keeps up to 3 .bak suffixes, deletes older).
rotate_config_backup() {
    local cfg="${SCRIPT_DIR}/config.json5"
    [[ ! -f "$cfg" ]] && return 0
    local bak3="${cfg}.bak.bak.bak"
    local bak2="${cfg}.bak.bak"
    local bak1="${cfg}.bak"
    [[ -f "$bak3" ]] && rm -f "$bak3"
    [[ -f "$bak2" ]] && mv "$bak2" "$bak3"
    [[ -f "$bak1" ]] && mv "$bak1" "$bak2"
    mv "$cfg" "$bak1"
    log_info "Rotated config.json5 -> config.json5.bak"
}

# Determine whether we need to apply a template.
APPLY_TEMPLATE=false
if $CONFIG_PARAM_EXPLICIT; then
    APPLY_TEMPLATE=true
elif [[ ! -f "${SCRIPT_DIR}/config.json5" ]]; then
    APPLY_TEMPLATE=true
    log_info "config.json5 not found — will create from template"
fi

if $APPLY_TEMPLATE; then
    TEMPLATE_FILE="$(resolve_config_template "${CONFIG_PARAM:-config}")"
    if [[ ! -f "$TEMPLATE_FILE" ]]; then
        log_error "Config template not found: $TEMPLATE_FILE"
        exit 1
    fi
    rotate_config_backup
    cp "$TEMPLATE_FILE" "${SCRIPT_DIR}/config.json5"
    log_info "Config applied: $TEMPLATE_FILE -> config.json5"
fi

# docker_cmd: run docker command with sudo fallback on docker.sock permission errors.
# input: docker subcommand and args; output: command stdout/stderr and exit code.
docker_cmd() {
    local errfile
    local rc
    errfile="$(mktemp)"

    docker "$@" 2>"$errfile"
    rc=$?

    if [[ "$rc" -eq 0 ]]; then
        rm -f "$errfile"
        return 0
    fi

    if grep -qiE "permission denied|got permission denied|cannot connect to the docker daemon" "$errfile"; then
        rm -f "$errfile"

        if command -v sudo >/dev/null 2>&1; then
            if [[ "$DOCKER_SUDO_FALLBACK_USED" != true ]]; then
                log_warn "Docker access for current shell is limited; retrying Docker commands via sudo"
                log_warn "If install_prerequisites.sh just added your user to docker group, re-login or run: newgrp docker"
                DOCKER_SUDO_FALLBACK_USED=true
            fi
            sudo docker "$@"
            return $?
        fi

        log_error "Docker daemon access denied and sudo is unavailable"
        log_error "Re-login after docker group update or run this script with sudo"
        return "$rc"
    fi

    cat "$errfile" >&2
    rm -f "$errfile"
    return "$rc"
}

log_info "sndbx installer"
log_info "Script directory: $SCRIPT_DIR"

# Check prerequisites
log_info "Checking prerequisites..."

# Python 3.11+
if ! command -v python3 &> /dev/null; then
    log_error "Python3 not found. Please install Python 3.11 or later"
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
log_info "Found Python $PYTHON_VERSION"

# Docker
if ! command -v docker &> /dev/null; then
    log_error "Docker not found. Please run ./install_prerequisites.sh first"
    exit 1
fi

log_info "Found Docker: $(docker --version)"

# Kata
# The shim location depends on the --kata_path used by install_prerequisites.sh,
# so read it from the Docker daemon configuration instead of assuming /opt/kata.
KATA_SHIM="$(python3 - <<'PY'
import json
from pathlib import Path

fallback = "/opt/kata/runtime-rs/bin/containerd-shim-kata-v2"
try:
    config = json.loads(Path("/etc/docker/daemon.json").read_text(encoding="utf-8"))
except Exception:
    config = {}
runtime = (config.get("runtimes") or {}).get("kata") or {}
runtime_type = runtime.get("runtimeType")
print(runtime_type.strip() if isinstance(runtime_type, str) and runtime_type.strip() else fallback)
PY
)"
if [[ ! -x "$KATA_SHIM" ]]; then
    log_error "Kata runtime not found at $KATA_SHIM"
    log_error "Please run ./install_prerequisites.sh first"
    exit 1
fi

log_info "Found Kata 4.1 runtime-rs shim at $KATA_SHIM"

# Verify Kata runtime is registered with Docker
if ! docker_cmd run --rm --name kata-check --runtime kata alpine echo "OK" >/dev/null; then
    log_warn "Kata runtime check failed - may need to verify Docker daemon.json configuration"
fi

# Check nested virtualization (if applicable)
if [[ -e /proc/cpuinfo ]]; then
    if ! grep -q "vmx\|svm" /proc/cpuinfo; then
        log_warn "Nested virtualization may not be available"
    fi
fi

# Create directories
log_info "Creating required directories..."

SNDBX_ROOT="${SCRIPT_DIR}"
SHARED_DIR="${SNDBX_ROOT}/shared"
DATA_DIR="${SNDBX_ROOT}/data"
LOG_DIR="${SNDBX_ROOT}/logs"
SESSIONS_DIR="${SNDBX_ROOT}/sessions"
BACKUP_DIR="${SNDBX_ROOT}/backup"

mkdir -p "$SHARED_DIR" "$DATA_DIR" "$LOG_DIR" "$SESSIONS_DIR" "$BACKUP_DIR"
log_info "Created directories: shared, data, logs, sessions, backup"

# Ensure proper permissions
chmod 755 "$SNDBX_ROOT"
chmod 755 "$SHARED_DIR" "$DATA_DIR" "$LOG_DIR" "$SESSIONS_DIR" "$BACKUP_DIR"

# Check if .env needs to be created
if [ ! -f "${SNDBX_ROOT}/.env" ]; then
    log_warn ".env file not found"
    log_info "Creating .env from .env.example..."
    
    if [ -f "${SNDBX_ROOT}/.env.example" ]; then
        cp "${SNDBX_ROOT}/.env.example" "${SNDBX_ROOT}/.env"
        
        # Update SNDBX_ROOT in .env
        sed -i "s|^SNDBX_ROOT=.*|SNDBX_ROOT=${SNDBX_ROOT}|" "${SNDBX_ROOT}/.env"
        
        log_info "Created .env with SNDBX_ROOT set to $SNDBX_ROOT"
    else
        log_error ".env.example not found"
        exit 1
    fi
fi

# Load .env
set -a
source "${SNDBX_ROOT}/.env"
set +a

log_info "Loaded configuration from .env"
log_info "SNDBX_ROOT=$SNDBX_ROOT"
log_info "LOG_LEVEL=$LOG_LEVEL"
log_info "MCP_PORT=$MCP_PORT"
log_info "MCP_HOST=$MCP_HOST"

# Initialise the aiset submodule; the configuration subsystem imports it from
# <root>/aiset/src and it is not part of requirements.txt.
if [ -f "${SNDBX_ROOT}/.gitmodules" ]; then
    if command -v git >/dev/null 2>&1; then
        log_info "Initialising git submodules (aiset)..."
        if git -C "$SNDBX_ROOT" submodule update --init --recursive; then
            log_info "Git submodules are ready"
        else
            log_warn "Could not initialise git submodules automatically"
        fi
    else
        log_warn "git is not available; the aiset submodule was not initialised"
    fi
fi

if [ ! -d "${SNDBX_ROOT}/aiset/src/aiset" ]; then
    log_error "The aiset settings library is missing from ${SNDBX_ROOT}/aiset/src"
    log_error "Run: git submodule update --init --recursive"
    log_error "sndbx cannot start without it"
    exit 1
fi

# Setup Python virtual environment and sync dependencies in venv
VENV_DIR="${SNDBX_ROOT}/venv"
if [ ! -d "$VENV_DIR" ]; then
    log_info "Creating Python virtual environment..."
    python3 -m venv "$VENV_DIR"
    log_info "Virtual environment created"
else
    log_info "Virtual environment already exists"
fi

log_info "Syncing Python dependencies in venv..."
"${VENV_DIR}/bin/python3" -m pip install --upgrade pip
if [ -f "${SNDBX_ROOT}/requirements.txt" ]; then
    "${VENV_DIR}/bin/python3" -m pip install -r "${SNDBX_ROOT}/requirements.txt"
    log_info "Dependencies synced from requirements.txt"
else
    log_warn "requirements.txt not found; skipping dependency install"
fi

# Check ports availability
log_info "Checking port availability..."

check_port() {
    if lsof -Pi :$1 -sTCP:LISTEN -t >/dev/null 2>&1; then
        return 1  # In use
    fi
    return 0  # Available
}

if ! check_port $MCP_PORT; then
    log_warn "Port $MCP_PORT is already in use. Update MCP_PORT in .env"
fi

if ! check_port $WEBUI_PORT; then
    log_warn "Port $WEBUI_PORT is already in use. Update WEBUI_PORT in .env"
fi

# Firewall check for externally exposed Web UI
if [[ "$WEBUI_HOST" == "0.0.0.0" || "$WEBUI_HOST" == "::" ]]; then
    if command -v ufw >/dev/null 2>&1; then
        UFW_STATUS=$(ufw status 2>/dev/null | head -n1 || true)
        if [[ "$UFW_STATUS" == "Status: active" ]]; then
            if ! ufw status 2>/dev/null | grep -Eq "(^|[[:space:]])${WEBUI_PORT}/tcp([[:space:]]|$).*ALLOW"; then
                log_warn "UFW is active and WEBUI_HOST is $WEBUI_HOST"
                log_warn "Port $WEBUI_PORT/tcp is not explicitly allowed in UFW"
                read -p "Open Web UI port in UFW now? (y/N) " -n 1 -r
                echo
                if [[ $REPLY =~ ^[Yy]$ ]]; then
                    if sudo ufw allow "${WEBUI_PORT}/tcp"; then
                        log_info "Allowed ${WEBUI_PORT}/tcp in UFW"
                    else
                        log_warn "Could not update UFW automatically"
                        log_warn "Run manually: sudo ufw allow ${WEBUI_PORT}/tcp"
                    fi
                else
                    log_info "Skipping UFW update"
                    log_info "If external access is needed, run: sudo ufw allow ${WEBUI_PORT}/tcp"
                fi
            fi
        fi
    fi
fi

# Setup systemd service
log_info "Setting up systemd service..."

SERVICE_FILE="/etc/systemd/system/sndbx.service"
USE_SYSTEM_SERVICE=true

# Check if we can write to /etc/systemd
if [ ! -w /etc/systemd/system ]; then
    log_warn "Cannot write to /etc/systemd/system (need root). Using user service instead."
    SERVICE_FILE="$HOME/.config/systemd/user/sndbx.service"
    USE_SYSTEM_SERVICE=false
    mkdir -p "$(dirname "$SERVICE_FILE")"
fi

# Create or update service file
cat > "$SERVICE_FILE" << 'EOF'
[Unit]
Description=sndbx Sandbox Management Service
Documentation=https://github.com/yourusername/sndbx
After=docker.service
Wants=docker.service

[Service]
Type=simple
Restart=always
RestartSec=5s
StandardOutput=journal
StandardError=journal

# Service identity and paths (placeholders replaced by install script)
WorkingDirectory=SNDBX_ROOT
Environment="PATH=SNDBX_VENV_BIN:/usr/local/bin:/usr/bin:/bin"

# Main command
ExecStart=SNDBX_VENV_BIN/python3 SNDBX_ROOT/src/app.py

[Install]
WantedBy=multi-user.target
EOF

# For user services, use user target and no explicit User= directive.
if ! $USE_SYSTEM_SERVICE; then
    sed -i 's/^WantedBy=multi-user.target$/WantedBy=default.target/' "$SERVICE_FILE"
else
    sed -i '/^WorkingDirectory=/i User=SNDBX_USER' "$SERVICE_FILE"
fi

# Replace placeholders with actual values
SERVICE_USER="$(whoami)"
sed -i "s|SNDBX_USER|${SERVICE_USER}|g" "$SERVICE_FILE"
sed -i "s|SNDBX_ROOT|${SNDBX_ROOT}|g" "$SERVICE_FILE"
sed -i "s|SNDBX_VENV_BIN|${VENV_DIR}/bin|g" "$SERVICE_FILE"

log_info "Service file created at $SERVICE_FILE"

# Install the optional root-owned backup ACL helper. It is required only when
# backups must read guest-owned files created under foreign host UIDs or GIDs.
install_backup_acl_helper() {
    local requested="${INSTALL_BACKUP_ACL_HELPER:-}"

    if [[ -z "$requested" && -t 0 ]]; then
        read -r -p "Install non-interactive backup ACL helper? (y/N) " requested
    fi

    if [[ "$requested" =~ ^([Yy]|[Yy][Ee][Ss]|1|true|TRUE)$ ]]; then
        if sudo "${SNDBX_ROOT}/install_backup_acl_helper.sh" "$SERVICE_USER"; then
            log_info "Installed non-interactive backup ACL helper"
        else
            log_warn "Could not install the backup ACL helper"
            log_warn "Backups of guest-owned files will fail until it is installed"
        fi
    elif [[ -n "$requested" && ! "$requested" =~ ^([Nn]|[Nn][Oo]|0|false|FALSE)$ ]]; then
        log_error "INSTALL_BACKUP_ACL_HELPER must be one of: 1, true, yes, 0, false, no"
        exit 1
    else
        log_info "Skipping optional backup ACL helper"
    fi
}

install_backup_acl_helper

# Enable service
if $USE_SYSTEM_SERVICE; then
    log_info "Enabling system-wide sndbx service..."
    sudo systemctl daemon-reload
    sudo systemctl enable sndbx.service
    SYSTEMCTL_CMD="sudo systemctl"
    log_info "Service enabled (system-wide)"

    log_info "Starting system-wide sndbx service..."
    if sudo systemctl start sndbx.service; then
        log_info "Service started (system-wide)"
    else
        log_warn "Failed to start sndbx service automatically"
        log_warn "Check status: sudo systemctl status sndbx"
    fi
else
    log_info "Enabling user sndbx service..."
    systemctl --user daemon-reload
    systemctl --user enable sndbx.service
    SYSTEMCTL_CMD="systemctl --user"
    log_info "Service enabled (user-level)"

    log_info "Starting user sndbx service..."
    if systemctl --user start sndbx.service; then
        log_info "Service started (user-level)"
    else
        log_warn "Failed to start sndbx service automatically"
        log_warn "Check status: systemctl --user status sndbx"
    fi
fi

log_info "Installation complete!"
log_info ""
log_info "Next steps:"
log_info "1. Review configuration:"
log_info "   nano .env              # Configure MCP_PORT, MCP_HOST, etc."
log_info "   nano config.json5      # Define sandboxes and users"
log_info ""
log_info "2. Check service status:"
if $USE_SYSTEM_SERVICE; then
    log_info "   sudo systemctl status sndbx"
    log_info "   sudo journalctl -u sndbx -f        # View logs"
else
    log_info "   systemctl --user status sndbx"
    log_info "   journalctl --user -u sndbx -f      # View logs"
fi
log_info ""
log_info "3. Test MCP server:"
log_info "   ./test_mcp.sh"
log_info ""
log_info "4. Open Web UI:"
log_info "   http://${WEBUI_HOST}:${WEBUI_PORT}"
log_info "   Default login: admin"
log_info "   Default password: admin"
log_info "   YOU MUST CHANGE THE DEFAULT PASSWORD IMMEDIATELY"
log_info ""
log_info "5. For more information:"
log_info "   cat README.md          # Full documentation"
log_info "   cat GETTING_STARTED.md # Quick start guide"
log_info ""
