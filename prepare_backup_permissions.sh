#!/usr/bin/env bash
# Root-owned helper for a single safe sndbx archive operation.
set -Eeuo pipefail

readonly config_dir="/etc/sndbx-backup-helper"

read_config() {
    local name="$1"
    local value
    IFS= read -r value < "${config_dir}/${name}"
    printf '%s' "$value"
}

validate_arguments() {
    [[ $# -eq 3 ]] || {
        printf 'Usage: %s <local-image-id> <full|incremental> <archive-name>\n' "$0" >&2
        exit 2
    }
    [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || {
        printf 'Invalid local image id.\n' >&2
        exit 1
    }
    [[ "$2" == "full" || "$2" == "incremental" ]] || {
        printf 'Invalid archive type.\n' >&2
        exit 1
    }
    [[ "$3" =~ ^${1}-[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}\.[0-9]{3}Z-${2}\.tar\.gz$ ]] || {
        printf 'Invalid archive name.\n' >&2
        exit 1
    }
}

require_directory_below() {
    local path="$1"
    local root="$2"
    [[ -d "$path" && ! -L "$path" && "$path" == "${root}/"* ]] || {
        printf 'Unsafe configured path: %s\n' "$path" >&2
        exit 1
    }
}

main() {
    validate_arguments "$@"
    (( EUID == 0 )) || {
        printf 'This helper must run as root through its sudoers rule.\n' >&2
        exit 1
    }

    local image_id="$1"
    local archive_type="$2"
    local archive_name="$3"
    local images_root backup_root state_root service_uid source_dir backup_dir state_dir
    images_root="$(realpath -- "$(read_config images-root)")"
    backup_root="$(realpath -- "$(read_config backup-root)")"
    state_root="$(realpath -- "$(read_config state-root)")"
    service_uid="$(read_config service-uid)"
    [[ "$service_uid" =~ ^[0-9]+$ ]] || {
        printf 'Configured service UID is invalid.\n' >&2
        exit 1
    }
    require_directory_below "$images_root" "$(dirname -- "$images_root")"
    require_directory_below "$backup_root" "$(dirname -- "$backup_root")"
    require_directory_below "$state_root" "$(dirname -- "$state_root")"

    source_dir="$(realpath -- "${images_root}/${image_id}")"
    [[ -d "$source_dir" && ! -L "$source_dir" && "$source_dir" == "${images_root}/"* ]] || {
        printf 'Image directory is unsafe or missing.\n' >&2
        exit 1
    }
    backup_dir="${backup_root}/${image_id}"
    install -d --owner "$service_uid" --group "$service_uid" --mode 0750 "$backup_dir"
    backup_dir="$(realpath -- "$backup_dir")"
    [[ "$backup_dir" == "${backup_root}/"* ]] || {
        printf 'Backup directory resolves outside configured backup root.\n' >&2
        exit 1
    }
    state_dir="${state_root}/${image_id}"
    install -d --owner root --group root --mode 0700 "$state_dir"
    exec 9>"${state_dir}/backup.lock"
    flock --nonblock 9 || {
        printf 'A backup helper is already running for this image.\n' >&2
        exit 1
    }
    find "$backup_dir" -maxdepth 1 -type f -name ".${image_id}.tar.*" -delete

    local snapshot_path="${state_dir}/${image_id}.snar"
    local work_snapshot
    local temp_archive
    work_snapshot="$(mktemp "${state_dir}/.${image_id}.snar.XXXXXX")"
    temp_archive="$(mktemp "${backup_dir}/.${image_id}.tar.XXXXXX")"
    cleanup() {
        rm -f -- "${work_snapshot:-}" "${temp_archive:-}"
    }
    trap cleanup EXIT

    if [[ "$archive_type" == "incremental" ]]; then
        [[ -f "$snapshot_path" && ! -L "$snapshot_path" ]] || {
            printf 'Missing root-owned incremental snapshot.\n' >&2
            exit 1
        }
        cp -- "$snapshot_path" "$work_snapshot"
    else
        rm -f -- "$work_snapshot"
    fi

    tar --create --gzip --ignore-failed-read \
        "--listed-incremental=${work_snapshot}" \
        "--file=${temp_archive}" \
        --directory "$(dirname -- "$source_dir")" "$(basename -- "$source_dir")"

    mv -f -- "$temp_archive" "${backup_dir}/${archive_name}"
    mv -f -- "$work_snapshot" "$snapshot_path"
    chown "$service_uid:$service_uid" "${backup_dir}/${archive_name}"
    chmod 0640 "${backup_dir}/${archive_name}"
    trap - EXIT
    printf 'Created %s\n' "${backup_dir}/${archive_name}"
}

main "$@"
