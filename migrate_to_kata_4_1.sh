#!/usr/bin/env bash
# Promote the staged Kata 4.1 runtime to the sole sndbx Kata runtime.
set -Eeuo pipefail

kata_version=4.1.0
legacy_bundle=/opt/kata
candidate_bundle=/opt/kata-acl
docker_config=/etc/docker/daemon.json
legacy_config=/etc/kata-containers/configuration.toml
candidate_config=/etc/kata-containers/runtime-rs/configuration-acl.toml
runtime_config=/etc/kata-containers/runtime-rs/configuration.toml
stage=
promoted=false
docker_changed=false

fail() {
    echo "kata_migration=FAIL message=$*" >&2
    exit 1
}

restore() {
    local status=$?
    set +e
    if [[ $status -ne 0 && -n $stage ]]; then
        [[ -f $stage/docker-daemon.json ]] && cp -- "$stage/docker-daemon.json" "$docker_config"
        [[ -e $stage/legacy-kata ]] && { rm -rf -- "$legacy_bundle"; mv -- "$stage/legacy-kata" "$legacy_bundle"; }
        [[ -e $stage/candidate-kata ]] && { rm -rf -- "$candidate_bundle"; mv -- "$stage/candidate-kata" "$candidate_bundle"; }
        [[ -f $stage/legacy-config.toml ]] && cp -- "$stage/legacy-config.toml" "$legacy_config"
        [[ -f $stage/candidate-config.toml ]] && cp -- "$stage/candidate-config.toml" "$candidate_config"
        rm -f -- "$runtime_config"
        systemctl reload docker >/dev/null 2>&1 || true
        echo "kata_migration_rollback=ATTEMPTED" >&2
    fi
    [[ -n $stage ]] && rm -rf -- "$stage"
    exit "$status"
}
trap restore EXIT HUP INT TERM

[[ ${EUID} -eq 0 ]] || fail "run_with=sudo bash $0"
[[ ${1:-} == --yes ]] || fail "confirmation_required=--yes"
for command in docker jq systemctl sha256sum sed; do
    command -v "$command" >/dev/null || fail "missing_command=$command"
done
[[ -x $candidate_bundle/runtime-rs/bin/containerd-shim-kata-v2 ]] || fail "missing_candidate_shim"
"$candidate_bundle/runtime-rs/bin/containerd-shim-kata-v2" --version | grep -Fq "version: $kata_version" || fail "unexpected_candidate_version"
[[ -f $candidate_config ]] || fail "missing_candidate_config=$candidate_config"
[[ -d $legacy_bundle ]] || fail "missing_legacy_bundle=$legacy_bundle"
[[ -f $docker_config ]] || fail "missing_docker_config=$docker_config"

stage=$(mktemp -d /var/tmp/sndbx-kata-4.1-migration-XXXXXX)
cp --preserve=mode,ownership,timestamps "$docker_config" "$stage/docker-daemon.json"
[[ -f $legacy_config ]] && cp --preserve=mode,ownership,timestamps "$legacy_config" "$stage/legacy-config.toml"
cp --preserve=mode,ownership,timestamps "$candidate_config" "$stage/candidate-config.toml"

mapfile -t kata_containers < <(
    docker ps --all --quiet | while IFS= read -r container_id; do
        [[ $(docker inspect --format '{{.HostConfig.Runtime}}' "$container_id") == kata ]] || continue
        running=$(docker inspect --format '{{.State.Running}}' "$container_id")
        name=$(docker inspect --format '{{.Name}}' "$container_id")
        printf '%s:%s\n' "${name#/}" "$running"
    done
)

printf 'kata_migration=START version=%s\n' "$kata_version"
for entry in "${kata_containers[@]}"; do
    printf 'kata_container_previous=%s\n' "$entry"
    name=${entry%%:*}
    if [[ ${entry##*:} == true ]]; then
        docker stop "$name" >/dev/null
    fi
    docker rm "$name" >/dev/null
done

mv -- "$legacy_bundle" "$stage/legacy-kata"
mv -- "$candidate_bundle" "$legacy_bundle"
promoted=true
sed -E 's#/opt/kata-acl/#/opt/kata/#g; s#^virtio_fs_extra_args[[:space:]]*=.*#virtio_fs_extra_args = ["--thread-pool-size=1", "-o", "announce_submounts"]#; s#^memory_slots[[:space:]]*=.*#memory_slots = 10#; s#^static_sandbox_resource_mgmt[[:space:]]*=.*#static_sandbox_resource_mgmt = true#' \
    "$stage/candidate-config.toml" >"$runtime_config"
if ! grep -E '^enable_annotations[[:space:]]*=.*"virtio_fs_extra_args"' "$runtime_config"; then
    sed -i -E '/^enable_annotations[[:space:]]*=/ s/\]$/, "virtio_fs_extra_args"]/' "$runtime_config"
fi

jq --arg shim "$legacy_bundle/runtime-rs/bin/containerd-shim-kata-v2" --arg config "$runtime_config" '
    . as $root
    | ($root.runtimes // {}) as $runtimes
    | $root + {runtimes: ($runtimes + {kata: {runtimeType: $shim, options: {ConfigPath: $config}}} | del(."kata-acl"))}
' "$stage/docker-daemon.json" >"$docker_config"
docker_changed=true
systemctl reload docker
docker info --format '{{json .Runtimes}}' | jq -e '.kata != null and ."kata-acl" == null' >/dev/null
docker run --rm --pull never --runtime kata --entrypoint /bin/true denis-obsidian-assistant:latest

rm -f -- "$legacy_config" "$candidate_config" /usr/local/bin/kata-runtime
ln -sfn "$legacy_bundle/runtime-rs/bin/containerd-shim-kata-v2" /usr/local/bin/containerd-shim-kata-v2
rm -rf -- "$stage/legacy-kata"
printf '%s\n' 'kata_migration_smoke=PASS'
printf '%s\n' 'kata_migration=PASS'