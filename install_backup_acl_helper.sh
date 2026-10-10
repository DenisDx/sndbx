#!/usr/bin/env bash
# Install the root-owned backup helper and its narrow sudoers rule.
set -Eeuo pipefail

readonly script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly helper_source="${script_dir}/prepare_backup_permissions.sh"
readonly helper_path="/usr/local/libexec/sndbx-backup-helper"
readonly config_dir="/etc/sndbx-backup-helper"
readonly sudoers_file="/etc/sudoers.d/sndbx-backup-helper"
readonly legacy_helper_path="/usr/local/libexec/sndbx-backup-acl"
readonly legacy_config_dir="/etc/sndbx-backup-acl"
readonly legacy_sudoers_file="/etc/sudoers.d/sndbx-backup-acl"

main() {
    [[ $# -eq 1 ]] || {
        printf 'Usage: sudo %s <sndbx-service-user>\n' "$0" >&2
        exit 2
    }
    (( EUID == 0 )) || {
        printf 'Run this one-time installer with sudo.\n' >&2
        exit 1
    }
    [[ -f "$helper_source" ]] || {
        printf 'ACL helper source is missing: %s\n' "$helper_source" >&2
        exit 1
    }

    local service_user="$1"
    local service_uid
    local images_root
    local backup_root
    service_uid="$(id --user "$service_user")"
    images_root="$(realpath -- "${script_dir}/images")"
    install -d --mode 0750 "${script_dir}/backup"
    backup_root="$(realpath -- "${script_dir}/backup")"
    [[ -d "$images_root" && -d "$backup_root" ]] || {
        printf 'Images or backup directory is missing.\n' >&2
        exit 1
    }

    install -d --owner root --group root --mode 0755 /usr/local/libexec "$config_dir"
    install --owner root --group root --mode 0755 "$helper_source" "$helper_path"
    printf '%s\n' "$images_root" > "${config_dir}/images-root"
    printf '%s\n' "$backup_root" > "${config_dir}/backup-root"
    printf '%s\n' "/var/lib/sndbx-backup" > "${config_dir}/state-root"
    printf '%s\n' "$service_uid" > "${config_dir}/service-uid"
    install -d --owner root --group root --mode 0700 /var/lib/sndbx-backup
    chown root:root "${config_dir}/images-root" "${config_dir}/backup-root" \
        "${config_dir}/state-root" "${config_dir}/service-uid"
    chmod 0644 "${config_dir}/images-root" "${config_dir}/backup-root" \
        "${config_dir}/state-root" "${config_dir}/service-uid"
    printf '%s ALL=(root) NOPASSWD: %s *\n' "$service_user" "$helper_path" > "$sudoers_file"
    chmod 0440 "$sudoers_file"
    visudo --check --file "$sudoers_file"
    rm -f -- "$legacy_helper_path" "$legacy_sudoers_file"
    rm -rf -- "$legacy_config_dir"
    printf 'Installed non-interactive backup ACL helper for %s (UID %s).\n' \
        "$service_user" "$service_uid"
}

main "$@"
