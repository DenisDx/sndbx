#!/usr/bin/env bash
# Install a parallel Kata runtime for the ACL VirtioFS canary.
set -Eeuo pipefail

runtime_name=kata-acl
kata_version=4.1.0
kata_arch=amd64
archive_url="https://github.com/kata-containers/kata-containers/releases/download/${kata_version}/kata-static-${kata_version}-${kata_arch}.tar.zst"
archive_sha256=3dc6b69c4acb787b967b04b64599a20d02a8beb1a8eaab3084110df9d0b08c96
target_dir=/opt/kata-acl
kata_config=/etc/kata-containers/runtime-rs/configuration-acl.toml
docker_config=/etc/docker/daemon.json
cache_dir=${KATA_ACL_CACHE_DIR:-/var/cache/sndbx-kata-acl}
provided_archive=${KATA_ACL_ARCHIVE:-}
stage=
installed_target=false
installed_config=false
docker_changed=false

fail() {
    echo "kata_acl_install=FAIL message=$*" >&2
    exit 1
}

cleanup() {
    local status=$?
    set +e
    if [[ $status -ne 0 && $docker_changed == true ]]; then
        cp -- "$stage/docker-daemon.json.backup" "$docker_config"
        systemctl reload docker >/dev/null 2>&1 || true
    fi
    if [[ $status -ne 0 && $installed_config == true ]]; then
        rm -f -- "$kata_config"
        rmdir -- "$(dirname "$kata_config")" 2>/dev/null || true
    fi
    if [[ $status -ne 0 && $installed_target == true ]]; then
        rm -rf -- "$target_dir"
    fi
    [[ -n ${new_target:-} ]] && rm -rf -- "$new_target"
    [[ -n $stage ]] && rm -rf -- "$stage"
    if [[ $status -eq 0 && -z $provided_archive ]]; then
        echo "archive_cache=RETAINED path=$cached_archive"
    elif [[ $status -ne 0 && -z $provided_archive && -s ${cached_archive:-} ]]; then
        echo "download_resume_path=$cached_archive" >&2
    elif [[ $status -ne 0 && -z $provided_archive && -s ${partial_archive:-} ]]; then
        echo "download_resume_path=$partial_archive" >&2
    fi
    exit "$status"
}
trap cleanup EXIT HUP INT TERM

if [[ ${EUID} -ne 0 ]]; then
    fail "run_with=sudo bash $0"
fi

[[ $(uname -m) == x86_64 ]] || fail "unsupported_architecture=$(uname -m)"
for command in curl jq sha256sum tar python3 systemctl docker; do
    command -v "$command" >/dev/null || fail "missing_command=$command"
done
[[ -f $docker_config ]] || fail "missing_docker_config=$docker_config"
[[ ! -e $target_dir ]] || fail "target_exists=$target_dir use=./uninstall_kata_acl.sh"
[[ ! -e $kata_config ]] || fail "config_exists=$kata_config use=./uninstall_kata_acl.sh"
jq -e . "$docker_config" >/dev/null || fail "invalid_json=$docker_config"
if jq -e '.runtimes["kata-acl"] != null' "$docker_config" >/dev/null; then
    fail "docker_runtime_exists=$runtime_name"
fi

stage=$(mktemp -d /var/tmp/sndbx-kata-acl-install-XXXXXX)
archive_name="kata-static-${kata_version}-${kata_arch}.tar.zst"
cached_archive="$cache_dir/$archive_name"
partial_archive="$cached_archive.part"
extract_dir="$stage/extract"
new_target="/opt/.kata-acl-new-$$"
mkdir -p -- "$extract_dir"

printf 'kata_acl_install=START version=%s\n' "$kata_version"
if [[ -n $provided_archive ]]; then
    [[ -f $provided_archive ]] || fail "provided_archive_missing=$provided_archive"
    archive=$provided_archive
    printf 'archive_source=provided path=%s\n' "$archive"
else
    mkdir -p -- "$cache_dir"
    if [[ -f $cached_archive ]]; then
        if printf '%s  %s\n' "$archive_sha256" "$cached_archive" | sha256sum --check --status; then
            printf 'archive_cache=VALID path=%s\n' "$cached_archive"
        else
            rm -f -- "$cached_archive"
        fi
    fi
    if [[ ! -f $cached_archive ]]; then
        resume_bytes=0
        [[ -f $partial_archive ]] && resume_bytes=$(stat --format=%s "$partial_archive")
        printf 'archive_download=http1.1 resume_bytes=%s\n' "$resume_bytes"
        curl --http1.1 --fail --location --retry 3 --retry-all-errors \
            --continue-at - --output "$partial_archive" "$archive_url"
        printf '%s  %s\n' "$archive_sha256" "$partial_archive" | sha256sum --check --status
        mv -- "$partial_archive" "$cached_archive"
    fi
    archive=$cached_archive
fi
printf '%s  %s\n' "$archive_sha256" "$archive" | sha256sum --check --status
tar --zstd -xf "$archive" -C "$extract_dir"
bundle_dir="$extract_dir/opt/kata"
[[ -x $bundle_dir/runtime-rs/bin/containerd-shim-kata-v2 ]] || fail "archive_missing=runtime-rs-shim"
[[ -e $bundle_dir/libexec/virtiofsd ]] || fail "archive_missing=virtiofsd"
[[ -x $bundle_dir/bin/qemu-system-x86_64 ]] || fail "archive_missing=qemu"
[[ -f $bundle_dir/share/kata-containers/vmlinux.container ]] || fail "archive_missing=kernel"
[[ -f $bundle_dir/share/kata-containers/kata-containers.img ]] || fail "archive_missing=image"

mv -- "$bundle_dir" "$new_target"
mv -- "$new_target" "$target_dir"
installed_target=true

mkdir -p -- "$(dirname "$kata_config")"
python3 - "$kata_config" <<'PY'
import re
import sys
from pathlib import Path

source_path = Path("/etc/kata-containers/configuration.toml")
target_path = Path(sys.argv[1])
content = source_path.read_text(encoding="utf-8").replace("/opt/kata/", "/opt/kata-acl/")

field = "virtio_fs_extra_args"
pattern = rf"^{field}\s*=\s*\[[^\]]*\]"
replacement = 'virtio_fs_extra_args = ["--thread-pool-size=1", "--announce-submounts", "--posix-acl=always"]'
content, substitutions = re.subn(pattern, replacement, content, flags=re.MULTILINE)
if substitutions != 1:
    raise SystemExit(f"Expected exactly one {field} in Kata configuration")

def replace_legacy_timeout(
    text: str,
    legacy_field: str,
    replacement,
) -> str:
    pattern = rf"^{legacy_field}[ \t]*=[ \t]*(\d+)[ \t]*$"
    text, substitutions = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if substitutions > 1:
        raise SystemExit(f"Expected at most one {legacy_field} in Kata configuration")
    return text

content = replace_legacy_timeout(
    content,
    "dial_timeout",
    lambda match: "dial_timeout_ms = 10\nreconnect_timeout_ms = " + str(int(match.group(1)) * 1000),
)
content = replace_legacy_timeout(
    content,
    "cdh_api_timeout",
    lambda match: "cdh_api_timeout_ms = " + str(int(match.group(1)) * 1000),
)

runtime_header = "[runtime]"
runtime_values = '\n'.join((
    runtime_header,
    'name = "virt_container"',
    'hypervisor_name = "qemu"',
    'agent_name = "kata"',
))
content, substitutions = re.subn(
    rf"^{re.escape(runtime_header)}$",
    runtime_values,
    content,
    count=1,
    flags=re.MULTILINE,
)
if substitutions != 1:
    raise SystemExit("Missing [runtime] in Kata configuration")

target_path.write_text(content, encoding="utf-8")
PY
chmod 0644 "$kata_config"
installed_config=true
"$target_dir/runtime-rs/bin/containerd-shim-kata-v2" --version
printf '%s\n' 'kata_acl_shim=VALID'

cp --preserve=mode,ownership,timestamps "$docker_config" "$stage/docker-daemon.json.backup"
jq --arg runtime "$runtime_name" --arg shim "$target_dir/runtime-rs/bin/containerd-shim-kata-v2" --arg config "$kata_config" '
    . as $root
    | ($root.runtimes // {}) as $runtimes
    | $root + {
        runtimes: ($runtimes + {
            ($runtime): {
                runtimeType: $shim,
                options: {ConfigPath: $config}
            }
        })
    }
' "$docker_config" >"$stage/docker-daemon.json"
install --mode=0644 "$stage/docker-daemon.json" "$docker_config"
docker_changed=true
systemctl reload docker
docker info --format '{{json .Runtimes}}' | jq -e '."kata-acl" != null' >/dev/null
printf '%s\n' 'docker_runtime=REGISTERED'
docker run --rm --pull never --runtime "$runtime_name" --entrypoint /bin/true denis-obsidian-assistant:latest
printf '%s\n' 'kata_acl_smoke=PASS'

printf '%s\n' 'kata_acl_install=PASS'
