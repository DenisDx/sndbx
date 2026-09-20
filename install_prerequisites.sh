#!/usr/bin/env bash

set -euo pipefail

KATA_PATH="/opt/kata"
KATA_VERSION="4.1.0"
KATA_CONFIG_PATH="/etc/kata-containers/runtime-rs/configuration.toml"
DOCKER_PATH=""
TMP_PATH="/tmp"
KATA_ARCHIVE=""  # pre-downloaded archive path (--kata_archive)
APT_MIRROR=""    # host apt mirror override (--apt_mirror)

# Color support when stdout is a terminal.
if [[ -t 1 ]]; then
  _C_RED='\033[0;31m'; _C_GREEN='\033[0;32m'; _C_YELLOW='\033[1;33m'
  _C_BLUE='\033[0;36m'; _C_BOLD='\033[1m'; _C_NC='\033[0m'
else
  _C_RED=''; _C_GREEN=''; _C_YELLOW=''; _C_BLUE=''; _C_BOLD=''; _C_NC=''
fi

_STEP=0

# print_step: print a numbered section header.
# input: step description
# output: formatted header to stdout
print_step() {
  _STEP=$(( _STEP + 1 ))
  echo ""
  echo -e "${_C_BLUE}${_C_BOLD}━━━ Step ${_STEP}: $* ━━━${_C_NC}"
}

# print_info: print an informational message.
# input: message string
# output: text to stdout
print_info() {
  echo -e "  [INFO] $*"
}

# print_ok: print a success confirmation.
# input: message string
# output: text to stdout
print_ok() {
  echo -e "  ${_C_GREEN}[ OK ]${_C_NC} $*"
}

# print_warn: print a warning message.
# input: message string
# output: text to stdout
print_warn() {
  echo -e "  ${_C_YELLOW}[WARN]${_C_NC} $*"
}

# print_error: print an error message.
# input: message string
# output: text to stderr
print_error() {
  echo -e "  ${_C_RED}[ERR ]${_C_NC} $*" >&2
}

# print_hint: print an actionable suggestion after an error.
# input: hint text
# output: text to stderr
print_hint() {
  echo -e "  ${_C_YELLOW}      ➜ $*${_C_NC}" >&2
}

# show_help: print script usage and options.
# input: none
# output: help text to stdout
show_help() {
  cat <<'EOF'
Usage:
  ./install_prerequisites.sh [OPTIONS]

Options:
  --kata_path <path>      Install Kata into custom path (default: /opt/kata).
  --docker_path <path>    Set Docker data-root to custom path (no Docker reinstall).
  --tmp_path <path>       Use custom directory for temporary files (default: /tmp).
  --kata_archive <path>   Use pre-downloaded Kata .tar.zst archive (skip download).
  --apt_mirror <url>      Override apt mirror for host package installs.
                          Example: http://mirrors.aliyun.com/ubuntu
  --help                  Show this help and exit.
EOF
}

# parse_args: parse command-line options.
# input: script arguments
# output: updates global option variables
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --kata_path)
        if [[ $# -lt 2 ]]; then
          print_error "--kata_path requires a value."
          exit 1
        fi
        KATA_PATH="$2"
        shift 2
        ;;
      --docker_path)
        if [[ $# -lt 2 ]]; then
          print_error "--docker_path requires a value."
          exit 1
        fi
        DOCKER_PATH="$2"
        shift 2
        ;;
      --tmp_path)
        if [[ $# -lt 2 ]]; then
          print_error "--tmp_path requires a value."
          exit 1
        fi
        TMP_PATH="$2"
        shift 2
        ;;
      --kata_archive)
        if [[ $# -lt 2 ]]; then
          print_error "--kata_archive requires a value."
          exit 1
        fi
        KATA_ARCHIVE="$2"
        shift 2
        ;;
      --apt_mirror)
        if [[ $# -lt 2 ]]; then
          print_error "--apt_mirror requires a value."
          exit 1
        fi
        APT_MIRROR="$2"
        shift 2
        ;;
      --help|-h)
        show_help
        exit 0
        ;;
      *)
        print_error "Unknown argument: $1"
        show_help
        exit 1
        ;;
    esac
  done
}

# require_absolute_path: require absolute path for option values.
# input: option name and value
# output: exits on invalid path
require_absolute_path() {
  local opt_name="$1"
  local path_value="$2"
  if [[ -n "$path_value" && "$path_value" != /* ]]; then
    print_error "$opt_name must be an absolute path. Got: $path_value"
    exit 1
  fi
}

# show_startup_summary: print script plan before execution.
# input: none
# output: summary text to stdout
show_startup_summary() {
  echo ""
  echo "This script will:"
  echo "  - Verify sudo access and KVM hardware virtualization support"
  echo "  - Install only missing prerequisite packages"
  echo "  - Enable/start Docker only when needed"
  echo "  - Add current user to docker group only when missing"
  echo "  - Install Kata Containers only when missing"
  echo "  - Register Kata runtime with Docker"
  echo ""
  echo "Options in effect:"
  printf "  %-22s %s\n" "--kata_path" "$KATA_PATH"
  if [[ -n "$DOCKER_PATH" ]]; then
    printf "  %-22s %s\n" "--docker_path" "$DOCKER_PATH"
  fi
  printf "  %-22s %s\n" "--tmp_path" "$TMP_PATH"
  if [[ -n "$KATA_ARCHIVE" ]]; then
    printf "  %-22s %s\n" "--kata_archive" "$KATA_ARCHIVE  (offline install)"
  fi
  if [[ -n "$APT_MIRROR" ]]; then
    printf "  %-22s %s\n" "--apt_mirror" "$APT_MIRROR"
  fi
  echo ""
  echo "Press Y to continue, Enter to cancel."
}

# confirm_execution: ask for interactive confirmation.
# input: none
# output: exits unless user entered Y/y
confirm_execution() {
  local answer
  read -r -p "Continue? [Y/Enter]: " answer
  if [[ "$answer" != "Y" && "$answer" != "y" ]]; then
    print_warn "Cancelled by user."
    exit 0
  fi
}

# run_sudo: run command as root when needed.
# input: command and args
# output: command output
run_sudo() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

# check_sudo_access: verify sudo is available before starting work.
# input: none
# output: exits with actionable message when sudo is unavailable
check_sudo_access() {
  if [[ "${EUID}" -eq 0 ]]; then
    print_ok "Running as root."
    return 0
  fi
  if ! command -v sudo >/dev/null 2>&1; then
    print_error "sudo is not installed. Install it or run this script as root."
    exit 1
  fi
  if ! sudo -v 2>/dev/null; then
    print_error "sudo authentication failed. Ensure your user can run sudo."
    print_hint "Add to sudoers: sudo usermod -aG sudo $USER  (then re-login)"
    exit 1
  fi
  print_ok "sudo access confirmed."
}

# check_snap_docker: detect snap-installed Docker and warn about potential conflicts.
# input: none
# output: warning when snap Docker is detected
check_snap_docker() {
  if ! command -v snap >/dev/null 2>&1; then
    return 0
  fi
  if snap list docker >/dev/null 2>&1; then
    print_warn "Docker is installed via snap."
    print_warn "snap Docker may conflict with Kata runtime registration in /etc/docker/daemon.json."
    print_hint "Switch to apt Docker: sudo snap remove docker && sudo apt install -y docker.io"
    print_hint "Continuing — if Kata runtime fails, remove snap Docker first."
  fi
}

# is_pkg_installed: check whether apt package is installed.
# input: package name
# output: success code if installed
is_pkg_installed() {
  local pkg="$1"
  dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed"
}

# has_existing_docker_stack: detect if Docker is already installed by any common package/source.
# input: none
# output: success code when Docker is already present
has_existing_docker_stack() {
  if command -v docker >/dev/null 2>&1; then
    return 0
  fi

  if is_pkg_installed docker-ce || is_pkg_installed docker.io || is_pkg_installed moby-engine; then
    return 0
  fi

  if command -v snap >/dev/null 2>&1 && snap list docker >/dev/null 2>&1; then
    return 0
  fi

  return 1
}

# version_gte: compare versions using dpkg logic.
# input: left version, right version
# output: success code if left >= right
version_gte() {
  local left="$1"
  local right="$2"
  dpkg --compare-versions "$left" ge "$right"
}

# require_supported_os: validate Ubuntu 22.04+.
# input: none
# output: exits on unsupported OS
require_supported_os() {
  if [[ ! -f /etc/os-release ]]; then
    print_error "Cannot detect OS: /etc/os-release is missing."
    exit 1
  fi

  # shellcheck source=/etc/os-release
  . /etc/os-release

  if [[ "${ID:-}" != "ubuntu" ]]; then
    print_error "This script supports Ubuntu only. Detected: ${ID:-unknown}."
    exit 1
  fi

  if ! dpkg --compare-versions "${VERSION_ID:-0}" ge "22.04"; then
    print_error "Ubuntu 22.04+ is required. Detected: ${VERSION_ID:-unknown}."
    exit 1
  fi
}

# check_version_conflicts: fail early when installed versions are too old.
# input: none
# output: exits on conflict
check_version_conflicts() {
  local min_python="3.10"
  local min_docker="20.10"
  local conflicts=()

  if command -v python3 >/dev/null 2>&1; then
    local py_ver
    py_ver="$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
    if ! version_gte "$py_ver" "$min_python"; then
      conflicts+=("python3 version $py_ver is too old (required >= $min_python)")
    fi
  fi

  if command -v docker >/dev/null 2>&1; then
    local docker_ver
    docker_ver="$(docker --version | sed -E 's/^Docker version ([0-9.]+).*/\1/')"
    if [[ -n "$docker_ver" ]] && ! version_gte "$docker_ver" "$min_docker"; then
      conflicts+=("docker version $docker_ver is too old (required >= $min_docker)")
    fi
  fi

  if (( ${#conflicts[@]} > 0 )); then
    print_error "Version conflicts detected. Resolve them manually and run again:"
    for item in "${conflicts[@]}"; do
      print_error "- $item"
    done
    exit 1
  fi
}

# check_path_conflicts: fail on conflicting preinstalled Kata paths.
# input: none
# output: exits on conflict
check_path_conflicts() {
  local default_kata="/opt/kata"

  if [[ "$KATA_PATH" != "$default_kata" && -d "$default_kata" && ! -d "$KATA_PATH" ]]; then
    print_error "Kata already exists at $default_kata, but --kata_path=$KATA_PATH was requested."
    print_error "Resolve manually (remove old Kata or choose existing path) and run again."
    exit 1
  fi

  if [[ -n "$DOCKER_PATH" ]]; then
    if [[ ! -d "$DOCKER_PATH" ]]; then
      print_info "Creating Docker data directory: $DOCKER_PATH"
      run_sudo mkdir -p "$DOCKER_PATH"
    fi
  fi
}

# install_missing_packages: install only missing apt packages.
# input: package list
# output: installs packages if needed
install_missing_packages() {
  local -a required=("$@")
  local -a missing=()

  local pkg
  for pkg in "${required[@]}"; do
    if is_pkg_installed "$pkg"; then
      print_info "Already installed: $pkg"
    else
      missing+=("$pkg")
    fi
  done

  if (( ${#missing[@]} == 0 )); then
    print_ok "All prerequisite packages are already installed."
    return 0
  fi

  print_info "Installing: ${missing[*]}"

  if [[ -n "$APT_MIRROR" ]]; then
    print_info "Applying host apt mirror: $APT_MIRROR"
    local ubuntu_codename
    ubuntu_codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"
    local backup_file="/etc/apt/sources.list.sndbx-backup"
    if [[ ! -f "$backup_file" ]]; then
      run_sudo cp /etc/apt/sources.list "$backup_file"
    fi
    run_sudo tee /etc/apt/sources.list >/dev/null <<EOF
deb $APT_MIRROR $ubuntu_codename main restricted universe multiverse
deb $APT_MIRROR ${ubuntu_codename}-updates main restricted universe multiverse
deb $APT_MIRROR ${ubuntu_codename}-backports main restricted universe multiverse
deb $APT_MIRROR ${ubuntu_codename}-security main restricted universe multiverse
EOF
  fi

  run_sudo apt-get update -q
  run_sudo apt-get install -y "${missing[@]}"

  if [[ -n "$APT_MIRROR" && -f "/etc/apt/sources.list.sndbx-backup" ]]; then
    run_sudo mv /etc/apt/sources.list.sndbx-backup /etc/apt/sources.list
    print_info "Restored /etc/apt/sources.list"
  fi
}

# ensure_docker_service: enable/start Docker only when needed.
# input: none
# output: service state changes when required
ensure_docker_service() {
  if ! command -v docker >/dev/null 2>&1; then
    print_error "docker command is unavailable after package checks."
    exit 1
  fi

  if systemctl is-enabled docker >/dev/null 2>&1; then
    print_info "Docker service is already enabled."
  else
    print_info "Enabling Docker service."
    run_sudo systemctl enable docker
  fi

  if systemctl is-active docker >/dev/null 2>&1; then
    print_info "Docker service is already running."
  else
    print_info "Starting Docker service."
    run_sudo systemctl start docker
  fi
}

# ensure_user_in_docker_group: add current user to docker group if needed.
# input: none
# output: updates user groups when needed
ensure_user_in_docker_group() {
  local target_user
  target_user="${SUDO_USER:-${USER}}"

  if [[ "$target_user" == "root" ]]; then
    print_warn "Current user is root; docker group update is skipped."
    return 0
  fi

  if id -nG "$target_user" | tr ' ' '\n' | grep -qx docker; then
    print_info "User '$target_user' is already in docker group."
    return 0
  fi

  print_info "Adding user '$target_user' to docker group."
  run_sudo usermod -aG docker "$target_user"
  print_warn "Log out and back in for docker group changes to take effect."
}

# ensure_docker_data_root: configure docker data-root when requested.
# input: none
# output: updates /etc/docker/daemon.json and restarts docker
ensure_docker_data_root() {
  if [[ -z "$DOCKER_PATH" ]]; then
    return 0
  fi

  local daemon_file="/etc/docker/daemon.json"
  local current_root
  local tmpfile

  run_sudo mkdir -p /etc/docker
  if [[ ! -f "$daemon_file" ]]; then
    echo '{}' | run_sudo tee "$daemon_file" >/dev/null
  fi

  current_root="$(jq -r '."data-root" // empty' "$daemon_file")"
  if [[ -n "$current_root" && "$current_root" != "$DOCKER_PATH" ]]; then
    print_error "Docker data-root conflict: current=$current_root requested=$DOCKER_PATH"
    print_error "Move Docker data manually, then re-run with the desired path."
    exit 1
  fi

  if [[ "$current_root" == "$DOCKER_PATH" ]]; then
    print_info "Docker data-root already set to $DOCKER_PATH"
    return 0
  fi

  tmpfile="$(mktemp)"
  jq --arg root "$DOCKER_PATH" '. + {"data-root": $root}' "$daemon_file" > "$tmpfile"
  run_sudo mv "$tmpfile" "$daemon_file"

  print_info "Docker data-root configured: $DOCKER_PATH"
  if run_sudo timeout 30 systemctl restart docker; then
    print_info "Docker restarted successfully."
  else
    print_warn "Docker restart timed out or failed. Check: sudo systemctl status docker --no-pager -l"
  fi
}

# host_phys_bits: detect host physical address bits from /proc/cpuinfo.
# input: none
# output: physical address bits, default 0 when unknown
host_phys_bits() {
  local first_line
  local bits

  first_line="$(grep -m1 'address sizes' /proc/cpuinfo 2>/dev/null || true)"
  bits="$(sed -n -E 's/.*: *([0-9]+) bits physical.*/\1/p' <<< "$first_line")"

  if [[ -n "$bits" ]]; then
    echo "$bits"
  else
    echo 0
  fi
}

# ensure_kata_phys_bits_compat: apply Kata/QEMU compatibility fixes on low phys-bits hosts.
# input: path to /etc/kata-containers/configuration.toml
# output: returns success and prints 1 when config changed, otherwise 0
ensure_kata_phys_bits_compat() {
  local cfg_file="$1"
  local changed=0
  local phys_bits
  local wrapper_path="/usr/local/bin/kata-qemu-wrapper"
  local qemu_path="${KATA_PATH}/bin/qemu-system-x86_64"

  phys_bits="$(host_phys_bits)"

  if [[ ! -f "$cfg_file" ]]; then
    echo 0
    return 0
  fi

  # Always avoid nvdimm image mapping on constrained hosts.
  if grep -qE '^[[:space:]]*disable_image_nvdimm[[:space:]]*=[[:space:]]*true[[:space:]]*$' "$cfg_file"; then
    :
  elif grep -qE '^[[:space:]]*disable_image_nvdimm[[:space:]]*=' "$cfg_file"; then
    run_sudo sed -i -E 's/^[[:space:]]*disable_image_nvdimm[[:space:]]*=.*/disable_image_nvdimm = true/' "$cfg_file"
    changed=1
  else
    run_sudo awk '
      BEGIN { inserted = 0 }
      {
        print
        if (!inserted && $0 ~ /^\[hypervisor\.qemu\]$/) {
          print "disable_image_nvdimm = true"
          inserted = 1
        }
      }
    ' "$cfg_file" > /tmp/kata_cfg.$$ && run_sudo mv /tmp/kata_cfg.$$ "$cfg_file"
    changed=1
  fi

  if [[ "$phys_bits" -gt 0 && "$phys_bits" -le 36 ]]; then
    print_warn "Detected low physical address width (${phys_bits} bits). Applying Kata QEMU compatibility fixes."

    if [[ ! -x "$wrapper_path" ]]; then
      run_sudo tee "$wrapper_path" >/dev/null <<EOF
#!/usr/bin/env bash
exec ${qemu_path} \
  -global q35-pcihost.pci-hole64-size=1073741824 \
  "\$@"
EOF
      run_sudo chmod +x "$wrapper_path"
      changed=1
    fi

    if grep -qE '^[[:space:]]*machine_type[[:space:]]*=[[:space:]]*"q35"[[:space:]]*$' "$cfg_file"; then
      :
    elif grep -qE '^[[:space:]]*machine_type[[:space:]]*=' "$cfg_file"; then
      run_sudo sed -i -E 's/^[[:space:]]*machine_type[[:space:]]*=.*/machine_type = "q35"/' "$cfg_file"
      changed=1
    fi

    if grep -qE '^[[:space:]]*path[[:space:]]*=[[:space:]]*"/usr/local/bin/kata-qemu-wrapper"[[:space:]]*$' "$cfg_file"; then
      :
    elif grep -qE '^[[:space:]]*path[[:space:]]*=[[:space:]]*".*qemu-system-x86_64"[[:space:]]*$' "$cfg_file"; then
      run_sudo sed -i -E 's#^[[:space:]]*path[[:space:]]*=[[:space:]]*".*qemu-system-x86_64"#path = "/usr/local/bin/kata-qemu-wrapper"#' "$cfg_file"
      changed=1
    fi

    if grep -q '/usr/local/bin/kata-qemu-wrapper' "$cfg_file"; then
      :
    elif grep -qE '^[[:space:]]*valid_hypervisor_paths[[:space:]]*=[[:space:]]*\[' "$cfg_file"; then
      run_sudo sed -i -E 's#^[[:space:]]*valid_hypervisor_paths[[:space:]]*=[[:space:]]*\[(.*)\]#valid_hypervisor_paths = [\1, "/usr/local/bin/kata-qemu-wrapper"]#' "$cfg_file"
      changed=1
    fi
  fi

  echo "$changed"
}

# ensure_docker_kata_runtime: register Kata runtime-rs in Docker daemon config.
# input: none; output: updates Docker and Kata runtime-rs configuration.
ensure_docker_kata_runtime() {
  local daemon_file="/etc/docker/daemon.json"
  local tmpfile
  local changed=0
  local defaults_dir
  local chosen_cfg=""
  local cand
  local cfg_changed

  defaults_dir="${KATA_PATH}/share/defaults/kata-containers/runtime-rs"

  run_sudo mkdir -p /etc/docker
  if [[ ! -f "$daemon_file" ]]; then
    echo '{}' | run_sudo tee "$daemon_file" >/dev/null
  fi

  tmpfile="$(mktemp)"
  jq --arg shim "${KATA_PATH}/runtime-rs/bin/containerd-shim-kata-v2" --arg config "$KATA_CONFIG_PATH" '
    . as $cfg
    | ($cfg.runtimes // {}) as $r
    | $cfg + {runtimes: ($r + {kata: {runtimeType: $shim, options: {ConfigPath: $config}}})}
  ' "$daemon_file" > "$tmpfile"

  if ! cmp -s "$tmpfile" "$daemon_file"; then
    run_sudo mv "$tmpfile" "$daemon_file"
    changed=1
    print_info "Registered Docker runtime 'kata' -> Kata runtime-rs shim"
  else
    rm -f "$tmpfile"
    print_info "Docker runtime 'kata' is already configured"
  fi

  if [[ ! -f "$KATA_CONFIG_PATH" ]]; then
    for cand in \
      "$defaults_dir/configuration-qemu-runtime-rs.toml" \
      "$defaults_dir/configuration.toml"; do
      if [[ -f "$cand" ]]; then
        chosen_cfg="$cand"
        break
      fi
    done

    if [[ -n "$chosen_cfg" ]]; then
      run_sudo mkdir -p "$(dirname "$KATA_CONFIG_PATH")"
      run_sudo cp "$chosen_cfg" "$KATA_CONFIG_PATH"
      print_info "Installed Kata runtime-rs config: $KATA_CONFIG_PATH (source: $chosen_cfg)"
      changed=1
    else
      print_warn "Could not find Kata default configuration in $defaults_dir"
      print_warn "Create $KATA_CONFIG_PATH manually from an available runtime-rs configuration template"
    fi
  fi

  # If Kata is installed in a custom path, update default /opt/kata paths in config.
  if [[ -f "$KATA_CONFIG_PATH" && "$KATA_PATH" != "/opt/kata" ]]; then
    if grep -q '/opt/kata/' "$KATA_CONFIG_PATH"; then
      local escaped_kata
      escaped_kata="$(printf '%s' "$KATA_PATH" | sed 's/[\/&]/\\&/g')"
      run_sudo sed -i "s#/opt/kata/#${escaped_kata}/#g" "$KATA_CONFIG_PATH"
      print_info "Rewrote /opt/kata paths in $KATA_CONFIG_PATH to $KATA_PATH"
      changed=1
    fi
  fi

  if [[ -f "$KATA_CONFIG_PATH" ]]; then
    if ! grep -q '"virtio_fs_extra_args"' "$KATA_CONFIG_PATH"; then
      run_sudo sed -i -E '/^enable_annotations[[:space:]]*=/ s/\]$/, "virtio_fs_extra_args"]/' "$KATA_CONFIG_PATH"
      changed=1
    fi
    cfg_changed="$(ensure_kata_phys_bits_compat "$KATA_CONFIG_PATH")"
    if [[ "$cfg_changed" == "1" ]]; then
      print_info "Applied Kata hypervisor compatibility settings"
      changed=1
    fi
  fi

  if [[ "$changed" -eq 1 ]]; then
    if run_sudo timeout 30 systemctl reload docker; then
      print_info "Docker reloaded successfully after runtime update."
    else
      print_warn "Docker reload timed out or failed. Check: sudo systemctl status docker --no-pager -l"
    fi
  fi

  local runtimes_json
  runtimes_json="$(docker_info_runtimes_json)"
  if [[ "$runtimes_json" != *'"kata"'* ]]; then
    print_error "Docker runtime 'kata' is not visible after runtime-rs registration."
    return 1
  fi
}

# docker_info_runtimes_json: query docker runtimes with permission-aware fallback.
# input: none
# output: JSON object string on stdout, empty string on failure
docker_info_runtimes_json() {
  local out
  local errfile
  errfile="$(mktemp)"

  if out="$(docker info --format '{{json .Runtimes}}' 2>"$errfile")"; then
    rm -f "$errfile"
    echo "$out"
    return 0
  fi

  if grep -qiE 'permission denied|got permission denied' "$errfile"; then
    if out="$(run_sudo docker info --format '{{json .Runtimes}}' 2>/dev/null)"; then
      rm -f "$errfile"
      echo "$out"
      return 0
    fi
  fi

  rm -f "$errfile"
  echo ""
}

# verify_kata_runtime_ready: ensure Docker+Kata integration is actually usable.
# input: none
# output: exits with error on incomplete setup
verify_kata_runtime_ready() {
  local runtimes_json

  if ! test -f "$KATA_CONFIG_PATH"; then
    print_error "Missing $KATA_CONFIG_PATH"
    print_hint "Copy ${KATA_PATH}/share/defaults/kata-containers/runtime-rs/configuration-qemu-runtime-rs.toml"
    exit 1
  fi

  runtimes_json="$(docker_info_runtimes_json)"
  if [[ -z "$runtimes_json" ]]; then
    print_error "Cannot query Docker runtimes."
    print_hint "Ensure Docker is running and your user has access (or run with sudo)."
    exit 1
  fi

  if [[ "$runtimes_json" != *'"kata"'* ]]; then
    print_error "Docker runtime 'kata' is not visible in 'docker info'"
    print_hint "Check /etc/docker/daemon.json runtimes.kata and restart docker."
    print_hint "If Docker was installed via snap, remove snap docker and use apt docker.io."
    exit 1
  fi

  print_ok "Kata runtime integration is ready."
}

# _hint_kvm_enable: print platform-specific KVM enablement hints to stderr.
# input: none
# output: hints to stderr
_hint_kvm_enable() {
  local virt=""
  virt="$(systemd-detect-virt 2>/dev/null || true)"
  case "$virt" in
    vmware)
      print_hint "VMware: enable 'Virtualize Intel VT-x/EPT or AMD-V/RVI'"
      print_hint "  in VM Settings → Processors → Virtualization Engine."
      ;;
    microsoft)
      print_hint "Hyper-V: enable nested virtualization on the host:"
      print_hint "  Set-VMProcessor -VMName <name> -ExposeVirtualizationExtensions \$true"
      ;;
    oracle)
      print_hint "VirtualBox: enable 'Enable Nested VT-x/AMD-V'"
      print_hint "  in Settings → System → Processor."
      ;;
    kvm)
      print_hint "Nested KVM: enable nested virt on the host:"
      print_hint "  echo 'options kvm_intel nested=1' | sudo tee /etc/modprobe.d/kvm-nested.conf"
      print_hint "  sudo modprobe -r kvm_intel && sudo modprobe kvm_intel"
      ;;
    lxc*|docker|podman)
      print_hint "Running inside a container: KVM passthrough is not supported."
      print_hint "Run sndbx on the host OS or in a VM with KVM passthrough enabled."
      ;;
    xen)
      print_hint "Xen: contact your cloud provider to enable HVM/nested-virt."
      ;;
    none|"")
      print_hint "Enable hardware virtualization (VT-x or AMD-V) in BIOS/UEFI firmware."
      print_hint "For cloud VMs: check your provider docs for nested virtualization options."
      ;;
    *)
      print_hint "Platform '$virt': check virtualization settings and enable VT-x/AMD-V."
      ;;
  esac
}

# verify_kvm_support: check KVM prerequisites and fail on unsupported setup.
# input: none
# output: exits on KVM verification failure
verify_kvm_support() {
  print_info "Loading KVM kernel modules."

  if ! lsmod | grep -q '^kvm\b'; then
    if ! run_sudo modprobe kvm 2>/dev/null; then
      print_error "Cannot load kvm kernel module."
      _hint_kvm_enable
      exit 1
    fi
  fi

  if ! lsmod | grep -qE '^kvm_(intel|amd)\b'; then
    if ! run_sudo modprobe kvm_intel 2>/dev/null && ! run_sudo modprobe kvm_amd 2>/dev/null; then
      print_warn "Neither kvm_intel nor kvm_amd loaded — may be OK on some platforms."
    fi
  fi

  if ! command -v kvm-ok >/dev/null 2>&1; then
    print_error "kvm-ok is unavailable. Ensure cpu-checker is installed."
    exit 1
  fi

  local kvm_out
  if ! kvm_out="$(kvm-ok 2>&1)"; then
    print_error "kvm-ok failed:"
    echo "$kvm_out" >&2
    _hint_kvm_enable
    exit 1
  fi

  if ! grep -qi "KVM acceleration can be used" <<< "$kvm_out"; then
    print_error "KVM acceleration is not available:"
    echo "$kvm_out" >&2
    _hint_kvm_enable
    exit 1
  fi

  print_ok "KVM acceleration is available."
}

# detect_kata_arch: map system arch to Kata release arch.
# input: none
# output: arch token for Kata release files
detect_kata_arch() {
  local host_arch
  host_arch="$(uname -m)"
  case "$host_arch" in
    x86_64)
      echo "amd64"
      ;;
    aarch64)
      echo "arm64"
      ;;
    *)
      print_error "Unsupported architecture for Kata: $host_arch"
      exit 1
      ;;
  esac
}

# kata_archive_sha256: return the pinned archive digest for one supported architecture.
# input: Kata release architecture; output: SHA-256 digest.
kata_archive_sha256() {
  case "$1" in
    amd64)
      echo "3dc6b69c4acb787b967b04b64599a20d02a8beb1a8eaab3084110df9d0b08c96"
      ;;
    arm64)
      echo "cf036de3d184dc080fe84dfbdbf06cd6811386224ff8c6aaee3b5d732722e58f"
      ;;
    *)
      return 1
      ;;
  esac
}

# is_kata_installed: check whether the pinned Kata runtime-rs shim is installed.
# input: none; output: success only for the supported Kata version.
is_kata_installed() {
  local shim_path="$KATA_PATH/runtime-rs/bin/containerd-shim-kata-v2"
  [[ -x "$shim_path" ]] && "$shim_path" --version 2>/dev/null | grep -Fqx "${KATA_VERSION}"
}

# install_kata_if_missing: install Kata static release only when absent.
# input: none
# output: installs Kata files and creates /usr/local/bin symlinks
install_kata_if_missing() {
  if is_kata_installed; then
    print_ok "Kata is already installed at $KATA_PATH"
    return 0
  fi

  if [[ -d "$KATA_PATH" ]]; then
    if [[ -n "$(ls -A "$KATA_PATH" 2>/dev/null)" ]]; then
      print_error "Existing Kata at $KATA_PATH is not the required Kata ${KATA_VERSION} runtime-rs installation."
      print_hint "The installer refuses to replace an existing runtime automatically."
      print_hint "Stop and remove every Kata sandbox, preserve a backup, then perform the documented migration."
      exit 1
    fi
  fi

  local kata_arch
  local expected_sha256
  local tar_path
  local tmp_extract
  local downloaded=0

  kata_arch="$(detect_kata_arch)"
  expected_sha256="$(kata_archive_sha256 "$kata_arch")" || {
    print_error "No pinned Kata checksum is available for $kata_arch."
    exit 1
  }

  if [[ -n "$KATA_ARCHIVE" ]]; then
    # Offline mode: use pre-downloaded archive.
    if [[ ! -f "$KATA_ARCHIVE" ]]; then
      print_error "Provided --kata_archive not found: $KATA_ARCHIVE"
      exit 1
    fi
    tar_path="$KATA_ARCHIVE"
    print_info "Using pre-downloaded archive: $tar_path"
  else
    print_info "Using pinned Kata release: ${KATA_VERSION}"

    if [[ ! -d "$TMP_PATH" ]]; then
      mkdir -p "$TMP_PATH" 2>/dev/null || {
        run_sudo mkdir -p "$TMP_PATH"
        run_sudo chown "$(id -u):$(id -g)" "$TMP_PATH"
      }
    fi

    local archive_name="kata-static-${KATA_VERSION}-${kata_arch}.tar.zst"
    local base_url="https://github.com/kata-containers/kata-containers/releases/download/${KATA_VERSION}"
    tar_path="${TMP_PATH}/${archive_name}"

    print_info "Downloading Kata ${KATA_VERSION} (${kata_arch}) — may take a while..."
    local attempt
    for attempt in 1 2 3; do
      # -C - resumes partial downloads.
      if curl --http1.1 -fL --retry 3 --retry-delay 5 -C - --progress-bar \
          -o "$tar_path" "${base_url}/${archive_name}"; then
        downloaded=1
        break
      fi
      if [[ "$attempt" -eq 3 ]]; then
        print_error "Download failed after 3 attempts."
        print_hint "Download manually and retry with: --kata_archive $tar_path"
        exit 1
      fi
      print_warn "Attempt ${attempt} failed, retrying..."
    done

  fi

  print_info "Verifying pinned SHA256 checksum..."
  if ! printf '%s  %s\n' "$expected_sha256" "$tar_path" | sha256sum --check --status; then
    print_error "SHA256 mismatch — archive is corrupted or not Kata ${KATA_VERSION}."
    [[ "$downloaded" -eq 1 ]] && rm -f "$tar_path"
    exit 1
  fi
  print_ok "SHA256 checksum verified."

  tmp_extract="$(mktemp -d -p "$TMP_PATH")"

  print_info "Extracting Kata archive..."
  if ! tar --zstd -xf "$tar_path" -C "$tmp_extract"; then
    print_error "Extraction failed — archive may be incomplete or corrupted."
    rm -rf "$tmp_extract"
    [[ "$downloaded" -eq 1 ]] && rm -f "$tar_path"
    exit 1
  fi

  if [[ ! -x "$tmp_extract/opt/kata/runtime-rs/bin/containerd-shim-kata-v2" ]]; then
    print_error "Unexpected archive layout: missing Kata runtime-rs shim"
    rm -rf "$tmp_extract"
    exit 1
  fi

  run_sudo mkdir -p "$KATA_PATH"
  run_sudo cp -a "$tmp_extract/opt/kata/." "$KATA_PATH/"

  run_sudo ln -sfn "$KATA_PATH/runtime-rs/bin/containerd-shim-kata-v2" /usr/local/bin/containerd-shim-kata-v2

  rm -rf "$tmp_extract"
  [[ "$downloaded" -eq 1 ]] && rm -f "$tar_path"

  print_ok "Kata installed into $KATA_PATH"
}

# main: install and verify prerequisites with idempotent behavior.
# input: none
# output: zero exit code on success
main() {
  parse_args "$@"
  require_absolute_path "--kata_path" "$KATA_PATH"
  require_absolute_path "--docker_path" "$DOCKER_PATH"
  require_absolute_path "--tmp_path" "$TMP_PATH"
  if [[ -n "$KATA_ARCHIVE" && "$KATA_ARCHIVE" != /* ]]; then
    print_error "--kata_archive must be an absolute path. Got: $KATA_ARCHIVE"
    exit 1
  fi
  show_startup_summary
  confirm_execution

  print_step "Preflight checks"
  check_sudo_access
  require_supported_os
  check_version_conflicts
  check_path_conflicts
  check_snap_docker

  print_step "Bootstrap package for KVM check"
  install_missing_packages cpu-checker

  print_step "KVM virtualization support"
  verify_kvm_support

  print_step "System packages"
  local -a required_packages=(
    python3
    python3-venv
    python3-pip
    curl
    jq
    tar
    zstd
    socat
    qemu-system-x86
    qemu-utils
  )

  if has_existing_docker_stack; then
    print_info "Docker is already installed; skipping Docker package install."
  else
    required_packages+=(docker.io)
  fi

  install_missing_packages "${required_packages[@]}"

  print_step "Docker service"
  ensure_docker_service
  ensure_docker_data_root
  ensure_user_in_docker_group

  print_step "Kata Containers"
  install_kata_if_missing

  print_step "Kata runtime integration with Docker"
  ensure_docker_kata_runtime
  verify_kata_runtime_ready

  echo ""
  echo -e "${_C_GREEN}${_C_BOLD}━━━ All prerequisites installed and verified successfully. ━━━${_C_NC}"
  echo ""
}

main "$@"