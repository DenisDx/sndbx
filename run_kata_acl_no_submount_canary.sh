#!/usr/bin/env bash
# Test Kata ACL propagation without VirtioFS announced submounts.
set -Eeuo pipefail

runtime_name=kata-acl
kata_config=/etc/kata-containers/runtime-rs/configuration-acl.toml
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
base_canary="$script_dir/run_kata_acl_fullstack_canary.sh"
backup=
original_sha256=
failed_command=

# Report the command that caused a failed canary.
report_error() {
    local status=$?
    failed_command=$BASH_COMMAND
    echo "canary_error=exit_${status} command=${failed_command}" >&2
}

# Restore only the isolated kata-acl configuration.
cleanup() {
    local status=$?
    set +e
    if [[ -n $backup && -f $backup ]]; then
        install --mode=0644 "$backup" "$kata_config"
    fi
    if [[ -f $kata_config ]]; then
        restored_sha256=$(sha256sum "$kata_config" | awk '{print $1}')
        if [[ $restored_sha256 == "$original_sha256" ]]; then
            echo "kata_acl_config_restored=PASS"
        else
            echo "kata_acl_config_restored=FAIL" >&2
            status=1
        fi
    fi
    [[ -n $backup ]] && rm -f -- "$backup"
    exit "$status"
}
trap report_error ERR
trap cleanup EXIT HUP INT TERM

if [[ ${EUID} -ne 0 ]]; then
    echo "Run with: sudo bash $0" >&2
    exit 2
fi
[[ -f $kata_config ]] || { echo "missing_kata_acl_config=$kata_config" >&2; exit 1; }
[[ -x $base_canary ]] || [[ -f $base_canary ]] || { echo "missing_base_canary=$base_canary" >&2; exit 1; }

mapfile -t kata_acl_containers < <(
    docker ps --all --quiet | while IFS= read -r container_id; do
        if [[ $(docker inspect --format '{{.HostConfig.Runtime}}' "$container_id") == "$runtime_name" ]]; then
            echo "$container_id"
        fi
    done
)
[[ ${#kata_acl_containers[@]} -eq 0 ]] || {
    echo "kata_acl_container_exists=refusing_config_change" >&2
    exit 1
}

backup=$(mktemp /var/tmp/sndbx-kata-acl-config-XXXXXX)
install --mode=0600 "$kata_config" "$backup"
original_sha256=$(sha256sum "$kata_config" | awk '{print $1}')

python3 - "$kata_config" <<'PY'
import re
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
content = config_path.read_text(encoding="utf-8")
pattern = r"^virtio_fs_extra_args\s*=\s*\[[^\]]*\]"
replacement = 'virtio_fs_extra_args = ["--thread-pool-size=1", "--no-announce-submounts", "--posix-acl=always"]'
content, substitutions = re.subn(pattern, replacement, content, count=1, flags=re.MULTILINE)
if substitutions != 1:
    raise SystemExit("Expected one virtio_fs_extra_args configuration entry")
config_path.write_text(content, encoding="utf-8")
PY

grep -Fx 'virtio_fs_extra_args = ["--thread-pool-size=1", "--no-announce-submounts", "--posix-acl=always"]' "$kata_config" >/dev/null
printf '%s\n' 'kata_acl_submounts=DISABLED'
bash "$base_canary"
