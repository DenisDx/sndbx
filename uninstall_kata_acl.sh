#!/usr/bin/env bash
# Remove the parallel Kata ACL runtime without touching the default Kata runtime.
set -Eeuo pipefail

runtime_name=kata-acl
target_dir=/opt/kata-acl
kata_config=/etc/kata-containers/runtime-rs/configuration-acl.toml
docker_config=/etc/docker/daemon.json
stage=
docker_changed=false

fail() {
    echo "kata_acl_uninstall=FAIL message=$*" >&2
    exit 1
}

cleanup() {
    local status=$?
    set +e
    if [[ $status -ne 0 && $docker_changed == true ]]; then
        cp -- "$stage/docker-daemon.json.backup" "$docker_config"
        systemctl reload docker >/dev/null 2>&1 || true
    fi
    [[ -n $stage ]] && rm -rf -- "$stage"
    exit "$status"
}
trap cleanup EXIT HUP INT TERM

if [[ ${EUID} -ne 0 ]]; then
    fail "run_with=sudo bash $0"
fi
for command in docker jq systemctl; do
    command -v "$command" >/dev/null || fail "missing_command=$command"
done

mapfile -t container_ids < <(docker ps --all --quiet)
for container_id in "${container_ids[@]}"; do
    runtime=$(docker inspect --format '{{.HostConfig.Runtime}}' "$container_id")
    if [[ $runtime == "$runtime_name" ]]; then
        container_name=$(docker inspect --format '{{.Name}}' "$container_id")
        fail "container_uses_runtime=${container_name#/} remove_container_first"
    fi
done

read -r -p "Remove only the parallel Kata ACL runtime? Type yes: " confirmation
[[ $confirmation == yes ]] || fail "confirmation_required=yes"

if [[ -f $docker_config ]] && jq -e '.runtimes["kata-acl"] != null' "$docker_config" >/dev/null; then
    stage=$(mktemp -d /var/tmp/sndbx-kata-acl-uninstall-XXXXXX)
    cp --preserve=mode,ownership,timestamps "$docker_config" "$stage/docker-daemon.json.backup"
    jq '
        del(.runtimes["kata-acl"])
        | if .runtimes == {} then del(.runtimes) else . end
    ' "$docker_config" >"$stage/docker-daemon.json"
    install --mode=0644 "$stage/docker-daemon.json" "$docker_config"
    docker_changed=true
    systemctl reload docker
    if docker info --format '{{json .Runtimes}}' | jq -e '."kata-acl" == null' >/dev/null; then
        printf '%s\n' 'docker_runtime=REMOVED'
    else
        fail "docker_runtime_still_present=$runtime_name"
    fi
fi

rm -f -- "$kata_config"
rmdir -- "$(dirname "$kata_config")" 2>/dev/null || true
rm -rf -- "$target_dir"
if [[ -e $kata_config || -e $target_dir ]]; then
    fail "cleanup_failed"
fi
printf '%s\n' 'kata_acl_uninstall=PASS'
