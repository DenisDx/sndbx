#!/bin/bash
# sndbx uninstall script
# Removes sndbx installation and systemd service

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log_info "sndbx uninstaller"
log_info "This will remove sndbx installation and systemd service."
log_info ""

# Confirm uninstall
read -p "Are you sure you want to uninstall sndbx? (yes/no) " -r
if [[ ! $REPLY =~ ^[Yy][Ee][Ss]$ ]]; then
    log_info "Uninstall cancelled"
    exit 0
fi

# Load .env if it exists
if [ -f "${SCRIPT_DIR}/.env" ]; then
    set -a
    source "${SCRIPT_DIR}/.env"
    set +a
fi

# Stop systemd service
log_info "Stopping sndbx service..."

# Try system service first
if systemctl is-enabled sndbx.service &>/dev/null 2>&1; then
    log_info "Stopping system service: sndbx.service"
    sudo systemctl stop sndbx.service 2>/dev/null || true
    sudo systemctl disable sndbx.service 2>/dev/null || true
    sudo systemctl daemon-reload 2>/dev/null || true
fi

# Try user service
if systemctl --user is-enabled sndbx.service &>/dev/null 2>&1; then
    log_info "Stopping user service: sndbx.service"
    systemctl --user stop sndbx.service 2>/dev/null || true
    systemctl --user disable sndbx.service 2>/dev/null || true
    systemctl --user daemon-reload 2>/dev/null || true
fi

# Remove systemd service files
log_info "Removing systemd service files..."

SYSTEM_SERVICE="/etc/systemd/system/sndbx.service"
USER_SERVICE="$HOME/.config/systemd/user/sndbx.service"

if [ -f "$SYSTEM_SERVICE" ]; then
    log_info "Removing: $SYSTEM_SERVICE"
    sudo rm -f "$SYSTEM_SERVICE"
fi

if [ -f "$USER_SERVICE" ]; then
    log_info "Removing: $USER_SERVICE"
    rm -f "$USER_SERVICE"
fi

# Ask what to keep
log_info ""
log_warn "What would you like to remove?"
echo ""
echo "1. Configuration only (.env, config.json5) - Keep code and data"
echo "2. Virtual environment only (venv/) - Keep configuration"
echo "3. Data files only (logs/, data/, shared/) - Keep code and config"
echo "4. Everything except git history"
echo "5. Nothing else (only remove service)"
echo ""

read -p "Choose (1-5): " -r CHOICE

case $CHOICE in
    1)
        log_info "Removing configuration files..."
        rm -f "${SCRIPT_DIR}/.env"
        rm -f "${SCRIPT_DIR}/config.json5"
        ;;
    2)
        log_info "Removing virtual environment..."
        rm -rf "${SCRIPT_DIR}/venv"
        ;;
    3)
        log_info "Removing data directories..."
        rm -rf "${SCRIPT_DIR}/logs"
        rm -rf "${SCRIPT_DIR}/data"
        rm -rf "${SCRIPT_DIR}/shared"
        ;;
    4)
        log_info "Removing all sndbx files except git history..."
        rm -f "${SCRIPT_DIR}/.env"
        rm -f "${SCRIPT_DIR}/config.json5"
        rm -rf "${SCRIPT_DIR}/venv"
        rm -rf "${SCRIPT_DIR}/logs"
        rm -rf "${SCRIPT_DIR}/data"
        rm -rf "${SCRIPT_DIR}/shared"
        rm -f "${SCRIPT_DIR}/__pycache__"
        rm -rf "${SCRIPT_DIR}/src/__pycache__"
        ;;
    5)
        log_info "Skipping file removal"
        ;;
    *)
        log_warn "Invalid choice, skipping file removal"
        ;;
esac

# Remove Docker network if needed
if docker network inspect sndbx-net &>/dev/null; then
    read -p "Remove Docker network 'sndbx-net'? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        log_info "Removing Docker network..."
        docker network rm sndbx-net || log_warn "Failed to remove network"
    fi
fi

log_info ""
log_info "Uninstall complete!"
log_info ""
log_info "Remaining files in ${SCRIPT_DIR}:"
du -sh "${SCRIPT_DIR}" 2>/dev/null || echo "  (unable to calculate)"
log_info ""
log_info "To completely remove sndbx, run:"
log_info "  rm -rf ${SCRIPT_DIR}"
