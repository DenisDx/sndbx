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

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
if ! command -v kata-runtime &> /dev/null; then
    log_error "Kata runtime not found. Please run ./install_prerequisites.sh first"
    exit 1
fi

log_info "Found Kata runtime"

# Verify Kata runtime is registered with Docker
if ! docker run --rm --name kata-check --runtime kata alpine echo "OK" 2>/dev/null; then
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

mkdir -p "$SHARED_DIR" "$DATA_DIR" "$LOG_DIR"
log_info "Created directories: shared, data, logs"

# Ensure proper permissions
chmod 755 "$SNDBX_ROOT"
chmod 755 "$SHARED_DIR" "$DATA_DIR" "$LOG_DIR"

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

# Create Docker network for sandboxes (if needed)
if ! docker network inspect sndbx-net &>/dev/null; then
    log_info "Creating Docker network: sndbx-net"
    docker network create sndbx-net --driver bridge
else
    log_info "Docker network sndbx-net already exists"
fi

# Setup Python virtual environment (optional but recommended)
VENV_DIR="${SNDBX_ROOT}/venv"
if [ ! -d "$VENV_DIR" ]; then
    log_info "Creating Python virtual environment..."
    python3 -m venv "$VENV_DIR"
    # Activate and install dependencies
    source "$VENV_DIR/bin/activate"
    pip install --upgrade pip
    # Install project dependencies
    if [ -f "${SNDBX_ROOT}/requirements.txt" ]; then
        pip install -r "${SNDBX_ROOT}/requirements.txt"
    fi
    deactivate
    log_info "Virtual environment created and dependencies installed"
else
    log_info "Virtual environment already exists"
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
sed -i "s|SNDBX_USER|$(whoami)|g" "$SERVICE_FILE"
sed -i "s|SNDBX_ROOT|${SNDBX_ROOT}|g" "$SERVICE_FILE"
sed -i "s|SNDBX_VENV_BIN|${VENV_DIR}/bin|g" "$SERVICE_FILE"

log_info "Service file created at $SERVICE_FILE"

# Enable service
if $USE_SYSTEM_SERVICE; then
    log_info "Enabling system-wide sndbx service..."
    sudo systemctl daemon-reload
    sudo systemctl enable sndbx.service
    SYSTEMCTL_CMD="sudo systemctl"
    log_info "Service enabled (system-wide)"
else
    log_info "Enabling user sndbx service..."
    systemctl --user daemon-reload
    systemctl --user enable sndbx.service
    SYSTEMCTL_CMD="systemctl --user"
    log_info "Service enabled (user-level)"
fi

log_info "Installation complete!"
log_info ""
log_info "Next steps:"
log_info "1. Review configuration:"
log_info "   nano .env              # Configure MCP_PORT, MCP_HOST, etc."
log_info "   nano config.json5      # Define sandboxes and users"
log_info ""
log_info "2. Start the service:"
if $USE_SYSTEM_SERVICE; then
    log_info "   sudo systemctl start sndbx"
    log_info "   sudo systemctl status sndbx"
    log_info "   sudo journalctl -u sndbx -f        # View logs"
else
    log_info "   systemctl --user start sndbx"
    log_info "   systemctl --user status sndbx"
    log_info "   journalctl --user -u sndbx -f      # View logs"
fi
log_info ""
log_info "3. Test MCP server:"
log_info "   ./test_mcp.sh"
log_info ""
log_info "4. Open Web UI:"
log_info "   http://${WEBUI_HOST}:${WEBUI_PORT}"
log_info ""
log_info "5. For more information:"
log_info "   cat README.md          # Full documentation"
log_info "   cat GETTING_STARTED.md # Quick start guide"
log_info ""
