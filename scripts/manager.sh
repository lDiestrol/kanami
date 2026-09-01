#!/usr/bin/env bash
set -Eeuo pipefail

readonly PROGRAM_NAME="kanami"
readonly MANAGER_NAME="Kanami Manager"

show_help() {
    cat <<'EOF'
Kanami Manager

Usage: kanami [command]

Commands:
  help       Show this help message
  version    Show manager version information
EOF
}

find_checkout() {
    local script_dir
    local candidate
    local checkout

    script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
    for candidate in "${script_dir}/.." /opt/kanami; do
        [[ -f "${candidate}/pyproject.toml" ]] || continue
        checkout="$(cd -- "${candidate}" && pwd)"
        git -c safe.directory="${checkout}" -C "${checkout}" \
            rev-parse --is-inside-work-tree >/dev/null 2>&1 || continue
        printf '%s\n' "${checkout}"
        return 0
    done

    return 1
}

show_version() {
    local checkout
    local commit

    printf '%s\n' "${MANAGER_NAME}"
    if checkout="$(find_checkout)" && \
        commit="$(git -c safe.directory="${checkout}" -C "${checkout}" \
            rev-parse --short HEAD 2>/dev/null)" && [[ -n ${commit} ]]; then
        printf 'Git commit: %s\n' "${commit}"
    fi
}

unknown_command() {
    printf '%s: unknown command: %s\n' "${PROGRAM_NAME}" "$1" >&2
    printf "Run '%s help' for usage.\n" "${PROGRAM_NAME}" >&2
    return 2
}

main() {
    local command="${1:-help}"

    case "${command}" in
        help | -h | --help)
            show_help
            ;;
        version)
            show_version
            ;;
        *)
            unknown_command "${command}"
            ;;
    esac
}

main "$@"
