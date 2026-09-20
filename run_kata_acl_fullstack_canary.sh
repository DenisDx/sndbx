#!/usr/bin/env bash
# Test full-stack Kata ACL propagation with the parallel kata-acl runtime.
set -Eeuo pipefail

runtime_name=kata-acl
image=denis-obsidian-assistant:latest
test_share=$(mktemp -d /tmp/sndbx-kata-acl-fullstack-share-XXXXXX)
container=sndbx-kata-acl-fullstack-canary-$(openssl rand -hex 6)
failed_command=

# Report the command that caused a failed canary.
report_error() {
    local status=$?
    failed_command=$BASH_COMMAND
    echo "canary_error=exit_${status} command=${failed_command}" >&2
}

# Remove only disposable canary resources.
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
    exit "$status"
}
trap report_error ERR
trap cleanup EXIT HUP INT TERM

for command in docker setfacl openssl; do
    command -v "$command" >/dev/null || {
        echo "missing_command=$command" >&2
        exit 1
    }
done

docker image inspect "$image" >/dev/null
docker info --format '{{json .Runtimes}}' | grep -Fq '"kata-acl"'

mkdir -p -- "$test_share/nested"
printf 'acl full-stack probe\n' >"$test_share/nested/readable.txt"
setfacl --modify u:1002:rx "$test_share" "$test_share/nested"
setfacl --modify u:1002:r "$test_share/nested/readable.txt"
setfacl --default --modify u:1002:rx "$test_share" "$test_share/nested"

printf '%s\n' 'canary=START'
docker run --detach --name "$container" --runtime "$runtime_name" --read-only \
    --mount "type=bind,src=$test_share,dst=/mnt/acl-test,readonly" \
    --entrypoint /bin/sleep "$image" 60 >/dev/null

docker inspect --format '{{.HostConfig.Runtime}}' "$container" | grep -Fx "$runtime_name"
printf '%s\n' 'kata_acl_container=RUNNING'
ps -eo args= | grep -F -- '/opt/kata-acl/libexec/virtiofsd' | grep -F -- '--posix-acl' >/dev/null
printf '%s\n' 'kata_acl_daemon=ACL_ENABLED'
docker exec "$container" getfattr -n system.posix_acl_access -e hex /mnt/acl-test
docker exec "$container" getfacl -cpn /mnt/acl-test
docker exec "$container" getfacl -cpn /mnt/acl-test | grep -Fq 'default:user:1002:r-x'
docker exec --user 1002:1002 "$container" sh -c \
    'test -x /mnt/acl-test && test -x /mnt/acl-test/nested && test -r /mnt/acl-test/nested/readable.txt'
printf '%s\n' 'acl_fullstack_canary=PASS'
