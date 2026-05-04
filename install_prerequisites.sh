#!/usr/bin/env bash

set -euo pipefail

KATA_PATH="/opt/kata"
DOCKER_PATH=""
TMP_PATH="/tmp"

# print_info: print an informational message.
# input: message string
# output: text to stdout
print_info() {
  echo "[INFO] $*"
}

# print_warn: print a warning message.
# input: message string
# output: text to stdout
print_warn() {
  echo "[WARN] $*"
}

# print_error: print an error message.
# input: message string
# output: text to stderr
print_error() {
  echo "[ERROR] $*" >&2
}

# show_help: print script usage and options.
# input: none
# output: help text to stdout
show_help() {
  cat <<'EOF'
Usage:
  ./install_prerequisites.sh [--kata_path <path>] [--docker_path <path>] [--help]

Options:
  --kata_path <path>    Install Kata into custom path (default: /opt/kata).
  --docker_path <path>  Set Docker data-root to custom path (no Docker reinstall).
  --tmp_path <path>     Use custom directory for temporary files (default: /tmp).
  --help                Show this help and exit.
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
  echo "This script will:"
  echo "- Install only missing prerequisite packages"
  echo "- Enable/start Docker only when needed"
  echo "- Add current user to docker group only when missing"
  echo "- Verify KVM support"
  echo "- Install Kata only when missing"
  echo ""
  echo "Options:"
  echo "- --kata_path <path>   (current: $KATA_PATH)"
  if [[ -n "$DOCKER_PATH" ]]; then
    echo "- --docker_path <path> (current: $DOCKER_PATH)"
  else
    echo "- --docker_path <path> (current: not set)"
  fi
  echo "- --tmp_path <path>    (current: $TMP_PATH)"
  echo ""
  echo "Press Y to continue. Press Enter (or any other key) to cancel."
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
      print_info "Package already installed: $pkg"
    else
      missing+=("$pkg")
    fi
  done

  if (( ${#missing[@]} == 0 )); then
    print_info "All prerequisite packages are already installed."
    return 0
  fi

  print_info "Installing missing packages: ${missing[*]}"
  run_sudo apt update
  run_sudo apt install -y "${missing[@]}"
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

    if grep -qE '^[[:space:]]*memory_slots[[:space:]]*=[[:space:]]*0[[:space:]]*$' "$cfg_file"; then
      :
    elif grep -qE '^[[:space:]]*memory_slots[[:space:]]*=' "$cfg_file"; then
      run_sudo sed -i -E 's/^[[:space:]]*memory_slots[[:space:]]*=.*/memory_slots = 0/' "$cfg_file"
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

# ensure_docker_kata_runtime: register Kata runtime in Docker daemon config.
# input: none
# output: updates /etc/docker/daemon.json and restarts docker when changed
ensure_docker_kata_runtime() {
  local daemon_file="/etc/docker/daemon.json"
  local tmpfile
  local changed=0
  local defaults_dir
  local chosen_cfg=""
  local cand
  local cfg_changed

  defaults_dir="${KATA_PATH}/share/defaults/kata-containers"

  run_sudo mkdir -p /etc/docker
  if [[ ! -f "$daemon_file" ]]; then
    echo '{}' | run_sudo tee "$daemon_file" >/dev/null
  fi

  tmpfile="$(mktemp)"
  jq '
    . as $cfg
    | ($cfg.runtimes // {}) as $r
    | ($r.kata // {}) as $k
    | $cfg + {runtimes: ($r + {kata: (($k + {runtimeType: "io.containerd.kata.v2"}) | del(.path))})}
  ' "$daemon_file" > "$tmpfile"

  if ! cmp -s "$tmpfile" "$daemon_file"; then
    run_sudo mv "$tmpfile" "$daemon_file"
    changed=1
    print_info "Registered Docker runtime 'kata' -> io.containerd.kata.v2"
  else
    rm -f "$tmpfile"
    print_info "Docker runtime 'kata' is already configured"
  fi

  if [[ ! -f /etc/kata-containers/configuration.toml ]]; then
    for cand in \
      "$defaults_dir/configuration.toml" \
      "$defaults_dir/configuration-qemu.toml" \
      "$defaults_dir/configuration-fc.toml" \
      "$defaults_dir/configuration-clh.toml"; do
      if [[ -f "$cand" ]]; then
        chosen_cfg="$cand"
        break
      fi
    done

    if [[ -n "$chosen_cfg" ]]; then
      run_sudo mkdir -p /etc/kata-containers
      run_sudo cp "$chosen_cfg" /etc/kata-containers/configuration.toml
      print_info "Installed Kata config: /etc/kata-containers/configuration.toml (source: $chosen_cfg)"
      changed=1
    else
      print_warn "Could not find Kata default configuration in $defaults_dir"
      print_warn "Create /etc/kata-containers/configuration.toml manually from available configuration-*.toml"
    fi
  fi

  # If Kata is installed in a custom path, update default /opt/kata paths in config.
  if [[ -f /etc/kata-containers/configuration.toml && "$KATA_PATH" != "/opt/kata" ]]; then
    if grep -q '/opt/kata/' /etc/kata-containers/configuration.toml; then
      local escaped_kata
      escaped_kata="$(printf '%s' "$KATA_PATH" | sed 's/[\/&]/\\&/g')"
      run_sudo sed -i "s#/opt/kata/#${escaped_kata}/#g" /etc/kata-containers/configuration.toml
      print_info "Rewrote /opt/kata paths in /etc/kata-containers/configuration.toml to $KATA_PATH"
      changed=1
    fi
  fi

  if [[ -f /etc/kata-containers/configuration.toml ]]; then
    cfg_changed="$(ensure_kata_phys_bits_compat /etc/kata-containers/configuration.toml)"
    if [[ "$cfg_changed" == "1" ]]; then
      print_info "Applied Kata hypervisor compatibility settings"
      changed=1
    fi
  fi

  if [[ "$changed" -eq 1 ]]; then
    if run_sudo timeout 30 systemctl restart docker; then
      print_info "Docker restarted successfully after runtime update."
    else
      print_warn "Docker restart timed out or failed. Check: sudo systemctl status docker --no-pager -l"
    fi
  fi
}

# verify_kata_runtime_ready: ensure Docker+Kata integration is actually usable.
# input: none
# output: exits with error on incomplete setup
verify_kata_runtime_ready() {
  local runtimes_json

  if ! test -f /etc/kata-containers/configuration.toml; then
    print_error "Missing /etc/kata-containers/configuration.toml"
    print_error "Fix by copying one of ${KATA_PATH}/share/defaults/kata-containers/configuration-*.toml"
    exit 1
  fi

  runtimes_json="$(docker info --format '{{json .Runtimes}}' 2>/dev/null || true)"
  if [[ "$runtimes_json" != *'"kata"'* ]]; then
    print_error "Docker runtime 'kata' is not visible in docker info"
    print_error "Check /etc/docker/daemon.json runtimes.kata.runtimeType and restart docker"
    exit 1
  fi

  print_info "Kata runtime integration is ready (docker runtime + configuration.toml)."
}

# verify_kvm_support: check KVM prerequisites and fail on unsupported setup.
# input: none
# output: exits on KVM verification failure
verify_kvm_support() {
  print_info "Verifying KVM support."

  if ! lsmod | grep -q '^kvm\b'; then
    run_sudo modprobe kvm
  fi

  if ! lsmod | grep -q '^kvm_(intel|amd)\b'; then
    run_sudo modprobe kvm_intel || run_sudo modprobe kvm_amd
  fi

  if ! command -v kvm-ok >/dev/null 2>&1; then
    print_error "kvm-ok command is unavailable. Ensure cpu-checker is installed."
    exit 1
  fi

  local kvm_out
  if ! kvm_out="$(kvm-ok 2>&1)"; then
    print_error "kvm-ok failed:"
    echo "$kvm_out" >&2
    exit 1
  fi

  if ! grep -qi "KVM acceleration can be used" <<< "$kvm_out"; then
    print_error "KVM prerequisites check failed:"
    echo "$kvm_out" >&2
    print_error "If this is a nested VM setup, enable nested virtualization and retry."
    exit 1
  fi

  print_info "KVM check passed."
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

# is_kata_installed: check whether Kata runtime is present in target path.
# input: none
# output: success code if installed in target path
is_kata_installed() {
  [[ -x "$KATA_PATH/bin/kata-runtime" && -x "$KATA_PATH/bin/containerd-shim-kata-v2" ]]
}

# install_kata_if_missing: install Kata static release only when absent.
# input: none
# output: installs Kata files and creates /usr/local/bin symlinks
install_kata_if_missing() {
  if is_kata_installed; then
    print_info "Kata is already installed at $KATA_PATH"
    return 0
  fi

  if [[ -d "$KATA_PATH" ]]; then
    if [[ -n "$(ls -A "$KATA_PATH" 2>/dev/null)" ]]; then
      print_error "Target Kata path exists and is not empty: $KATA_PATH"
      print_error "Refusing to overwrite existing data."
      exit 1
    fi
  fi

  local kata_arch
  local kata_ver
  local tar_path
  local tmp_extract

  kata_arch="$(detect_kata_arch)"
  kata_ver="$(curl -fsSL https://api.github.com/repos/kata-containers/kata-containers/releases/latest | jq -r '.tag_name' | sed 's/^v//')"
  if [[ ! -d "$TMP_PATH" ]]; then
    mkdir -p "$TMP_PATH" 2>/dev/null || {
      run_sudo mkdir -p "$TMP_PATH"
      run_sudo chown "$(id -u):$(id -g)" "$TMP_PATH"
    }
  fi
  tar_path="${TMP_PATH}/kata-static-${kata_ver}-${kata_arch}.tar.zst"
  tmp_extract="$(mktemp -d -p "$TMP_PATH")"

  print_info "Downloading Kata ${kata_ver} (${kata_arch})"
  curl -fL -o "$tar_path" "https://github.com/kata-containers/kata-containers/releases/download/${kata_ver}/kata-static-${kata_ver}-${kata_arch}.tar.zst"

  print_info "Extracting Kata archive"
  tar --zstd -xvf "$tar_path" -C "$tmp_extract" >/dev/null

  if [[ ! -d "$tmp_extract/opt/kata" ]]; then
    print_error "Unexpected Kata archive layout: missing opt/kata"
    rm -rf "$tmp_extract"
    exit 1
  fi

  run_sudo mkdir -p "$KATA_PATH"
  run_sudo cp -a "$tmp_extract/opt/kata/." "$KATA_PATH/"

  run_sudo ln -sfn "$KATA_PATH/bin/kata-runtime" /usr/local/bin/kata-runtime
  run_sudo ln -sfn "$KATA_PATH/bin/containerd-shim-kata-v2" /usr/local/bin/containerd-shim-kata-v2

  rm -rf "$tmp_extract"
  rm -f "$tar_path"

  print_info "Kata installed into $KATA_PATH"
}

# main: install and verify prerequisites with idempotent behavior.
# input: none
# output: zero exit code on success
main() {
  parse_args "$@"
  require_absolute_path "--kata_path" "$KATA_PATH"
  require_absolute_path "--docker_path" "$DOCKER_PATH"
  require_absolute_path "--tmp_path" "$TMP_PATH"
  show_startup_summary
  confirm_execution

  require_supported_os
  check_version_conflicts
  check_path_conflicts

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
    cpu-checker
  )

  if has_existing_docker_stack; then
    print_info "Docker is already installed; package installation for Docker will be skipped."
  else
    required_packages+=(docker.io)
  fi

  install_missing_packages "${required_packages[@]}"
  ensure_docker_service
  ensure_docker_data_root
  ensure_user_in_docker_group
  verify_kvm_support
  install_kata_if_missing
  ensure_docker_kata_runtime
  verify_kata_runtime_ready

  print_info "Prerequisites are installed and verified."
}

main "$@"