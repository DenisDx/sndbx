#!/usr/bin/env bash
# Verify the per-container Kata ACL compatibility annotation.
set -Eeuo pipefail

runtime_name=kata-acl
image=denis-obsidian-assistant:latest
kata_config=/etc/kata-containers/runtime-rs/configuration-acl.toml
annotation_key=io.katacontainers.config.hypervisor.virtio_fs_extra_args
annotation_value='--thread-pool-size=1,--no-announce-submounts,--posix-acl=always'
test_share=$(mktemp -d /tmp/sndbx-kata-acl-annotation-share-XXXXXX)
container=sndbx-kata-acl-annotation-canary-$(openssl rand -hex 6)
original_sha256=
failed_command=

# Report the command that caused a failed canary.
report_error() {
    local status=$?
    failed_command=$BASH_COMMAND
    echo "canary_error=exit_${status} command=${failed_command}" >&2
}

# Remove only disposable canary resources and verify the base configuration.
cleanup() {
    local status=$?
    set +e
    docker rm --force "$container" >/dev/null 2>&1
    rm -rf -- "$test_share"
    if docker container inspect "$container" >/dev/null 2>&1 || [[ -e $test_share ]]; then
        echo "cleanup=FAIL" >&2
        status=1
    else
        echo "cleanup=PASS"
    fi
    if [[ -n $original_sha256 && $(sha256sum "$kata_config" | awk '{print $1}') == "$original_sha256" ]]; then
        echo "kata_acl_config_unchanged=PASS"
    else
        echo "kata_acl_config_unchanged=FAIL" >&2
        status=1
    fi
    exit "$status"
}
trap report_error ERR
trap cleanup EXIT HUP INT TERM

if [[ ${EUID} -ne 0 ]]; then
    echo "Run with: sudo bash $0" >&2
    exit 2
fi
[[ -f $kata_config ]] || { echo "missing_kata_acl_config=$kata_config" >&2; exit 1; }
original_sha256=$(sha256sum "$kata_config" | awk '{print $1}')
for command in docker openssl setfacl sha256sum; do
    command -v "$command" >/dev/null || {
        echo "missing_command=$command" >&2
        exit 1
    }
done

mapfile -t kata_acl_containers < <(
    docker ps --all --quiet | while IFS= read -r container_id; do
        if [[ $(docker inspect --format '{{.HostConfig.Runtime}}' "$container_id") == "$runtime_name" ]]; then
            echo "$container_id"
        fi
    done
)
[[ ${#kata_acl_containers[@]} -eq 0 ]] || {
    echo "kata_acl_container_exists=refusing_canary" >&2
    exit 1
}

docker image inspect "$image" >/dev/null
docker info --format '{{json .Runtimes}}' | grep -Fq '"kata-acl"'

mkdir -p -- "$test_share/nested"
printf 'acl annotation probe\n' >"$test_share/nested/readable.txt"
setfacl --modify u:1002:rx "$test_share" "$test_share/nested"
setfacl --modify u:1002:r "$test_share/nested/readable.txt"
setfacl --default --modify u:1002:rx "$test_share" "$test_share/nested"

printf '%s\n' 'canary=START'
docker run --detach --name "$container" --runtime "$runtime_name" --read-only \
    --annotation "$annotation_key=$annotation_value" \
    --mount "type=bind,src=$test_share,dst=/mnt/acl-test,readonly" \
    --entrypoint /bin/sleep "$image" 60 >/dev/null

docker inspect --format '{{.HostConfig.Runtime}}' "$container" | grep -Fx "$runtime_name"
docker inspect --format '{{index .HostConfig.Annotations "io.katacontainers.config.hypervisor.virtio_fs_extra_args"}}' \
    "$container" | grep -Fqx -- "$annotation_value"
printf '%s\n' 'kata_acl_annotation=APPLIED'
ps -eo args= | grep -F -- '/opt/kata-acl/libexec/virtiofsd' | grep -F -- '--no-announce-submounts' | grep -F -- '--posix-acl=always' >/dev/null
printf '%s\n' 'kata_acl_daemon=ACL_COMPAT_ENABLED'
docker exec "$container" getfattr -n system.posix_acl_access -e hex /mnt/acl-test
docker exec "$container" getfacl -cpn /mnt/acl-test | grep -Fq 'default:user:1002:r-x'
docker exec --user 1002:1002 "$container" sh -c \
    'test -x /mnt/acl-test && test -x /mnt/acl-test/nested && test -r /mnt/acl-test/nested/readable.txt'
printf '%s\n' 'acl_annotation_canary=PASS'