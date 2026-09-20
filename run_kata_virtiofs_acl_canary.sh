#!/usr/bin/env bash
# Test a newer VirtioFS daemon without persisting Kata or Docker changes.
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run with: sudo bash $0" >&2
    exit 2
fi

kata_config=/etc/kata-containers/configuration.toml
kata_runtime=/usr/local/bin/kata-runtime
image=denis-obsidian-assistant:latest
archive_url=https://github.com/kata-containers/kata-containers/releases/download/4.1.0/kata-static-4.1.0-amd64.tar.zst
archive_sha256=3dc6b69c4acb787b967b04b64599a20d02a8beb1a8eaab3084110df9d0b08c96
stage=$(mktemp -d /var/tmp/sndbx-kata-acl-canary-XXXXXX)
backup=$(mktemp /var/tmp/sndbx-kata-config-XXXXXX)
test_share=$(mktemp -d /tmp/sndbx-kata-acl-share-XXXXXX)
container=sndbx-kata-acl-canary-$(openssl rand -hex 6)
original_sha256=$(sha256sum "$kata_config" | awk '{print $1}')
failed_command=

report_error() {
    local status=$?
    failed_command=$BASH_COMMAND
    echo "canary_error=exit_${status} command=${failed_command}" >&2
}

cleanup() {
    local status=$?
    set +e
    docker rm --force "$container" >/dev/null 2>&1
    rm -rf -- "$test_share"
    if [[ -f $backup ]]; then
        install --mode=0644 "$backup" "$kata_config"
    fi
    if [[ -f $kata_config ]]; then
        restored_sha256=$(sha256sum "$kata_config" | awk '{print $1}')
        if [[ $restored_sha256 == "$original_sha256" ]]; then
            echo "config_restored=PASS"
        else
            echo "config_restored=FAIL" >&2
            status=1
        fi
    fi
    rm -f -- "$backup"
    rm -rf -- "$stage"
    if [[ -e $stage || -e $test_share || -e $backup ]]; then
        echo "cleanup=FAIL" >&2
        status=1
    else
        echo "cleanup=PASS"
    fi
    exit "$status"
}
trap report_error ERR
trap cleanup EXIT HUP INT TERM

echo "canary=START"

[[ -x $kata_runtime ]] || { echo "Kata runtime is unavailable" >&2; exit 1; }
[[ -r $kata_config ]] || { echo "Kata config is unavailable" >&2; exit 1; }
docker image inspect "$image" >/dev/null
install --mode=0600 "$kata_config" "$backup"

archive="$stage/kata-static-4.1.0-amd64.tar.zst"
daemon="$stage/virtiofsd-1.14.0"
curl --fail --location --retry 3 --retry-all-errors --output "$archive" "$archive_url"
printf '%s  %s\n' "$archive_sha256" "$archive" | sha256sum --check --status
entries_file="$stage/virtiofsd-entries.txt"
tar --zstd -tf "$archive" >"$entries_file"
entry=$(awk -F/ '$NF == "virtiofsd" { print }' "$entries_file")
[[ $(printf '%s\n' "$entry" | sed '/^$/d' | wc -l) -eq 1 ]] || {
    echo "Expected one VirtioFS daemon in the archive" >&2
    exit 1
}
tar --zstd -xOf "$archive" "$entry" >"$daemon"
chmod 0755 "$daemon"
file "$daemon" | grep -Eiq 'ELF 64-bit.*x86-64'
"$daemon" --help 2>&1 | grep -Eq -- '(^|[[:space:],])--posix-acl([=[:space:],]|$)'
rm -f -- "$archive"

python3 - "$kata_config" "$daemon" <<'PY'
import re
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
daemon_path = sys.argv[2]
content = config_path.read_text(encoding="utf-8")

def add_list_value(field: str, value: str) -> str:
    pattern = rf"^({field}\s*=\s*\[)([^\]]*)(\])"
    match = re.search(pattern, content, flags=re.MULTILINE)
    if match is None:
        raise SystemExit(f"Missing {field} in Kata config")
    items = match.group(2)
    if f'"{value}"' in items:
        return content
    separator = "" if not items.strip() else ", "
    replacement = f'{match.group(1)}{items.rstrip()}{separator}"{value}"{match.group(3)}'
    return content[:match.start()] + replacement + content[match.end():]

content = add_list_value("enable_annotations", "virtio_fs_daemon")
content = add_list_value("valid_virtio_fs_daemon_paths", daemon_path)
config_path.write_text(content, encoding="utf-8")
PY

"$kata_runtime" --config "$kata_config" check --no-network-checks
echo "kata_config=VALID"

setfacl --modify u:1002:rx "$test_share"
setfacl --default --modify u:1002:rx "$test_share"
annotation_daemon="io.katacontainers.config.hypervisor.virtio_fs_daemon=$daemon"
annotation_args='io.katacontainers.config.hypervisor.virtio_fs_extra_args=["--thread-pool-size=1", "--announce-submounts", "--posix-acl"]'
docker run --detach --name "$container" --runtime kata --read-only \
    --mount "type=bind,src=$test_share,dst=/mnt/acl-test,readonly" \
    --annotation "$annotation_daemon" \
    --annotation "$annotation_args" \
    --entrypoint /bin/sleep "$image" 60 >/dev/null

sleep 2
ps -eo args= | grep -F -- "$daemon" | grep -F -- '--posix-acl' >/dev/null
echo "daemon_canary=RUNNING"
docker exec "$container" getfattr -n system.posix_acl_access -e hex /mnt/acl-test
docker exec "$container" getfacl -cpn /mnt/acl-test
docker exec --user 1002:1002 "$container" sh -c 'test -x /mnt/acl-test && test -r /mnt/acl-test'
echo "acl_canary=PASS"