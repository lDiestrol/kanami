#!/usr/bin/env bash
set -Eeuo pipefail

readonly PROGRAM_NAME="kanami"
readonly MANAGER_NAME="Kanami Manager"
readonly MUTATING_SYSTEMCTL="/usr/bin/systemctl"
readonly BOT_SERVICE="kanami.service"
readonly JOURNALCTL="/usr/bin/journalctl"
readonly DEFAULT_LOG_LINES=100
readonly MAX_LOG_LINES=1000
readonly UPDATE_CHECKOUT="/opt/kanami"
readonly UPDATE_SCRIPTS_DIR="/opt/kanami/scripts"
readonly UPDATE_SCRIPT="/opt/kanami/scripts/update.sh"
readonly WEB_SETUP_SCRIPT="/opt/kanami/scripts/web-admin-setup.sh"
readonly UPDATE_BASH="/usr/bin/bash"
readonly UPDATE_STAT="/usr/bin/stat"
readonly PRODUCTION_GIT_DIR="${UPDATE_CHECKOUT}/.git"
readonly PRODUCTION_MANAGER_SOURCE="${UPDATE_SCRIPTS_DIR}/manager.sh"
readonly PRODUCTION_SERVICE_UNIT="${UPDATE_CHECKOUT}/deploy/kanami.service"
readonly PRODUCTION_VENV="${UPDATE_CHECKOUT}/.venv"
readonly PRODUCTION_BOT_EXECUTABLE="${PRODUCTION_VENV}/bin/discord-stats-bot"
# D2.2 read-only test seams; future mutating commands must not trust these paths.
readonly INSTALL_DIR="${KANAMI_MANAGER_INSTALL_DIR:-/opt/kanami}"
readonly UV_BOOTSTRAP_DIR="${KANAMI_MANAGER_UV_BOOTSTRAP_DIR:-/opt/kanami-uv}"
readonly UV_CACHE_DIR="${KANAMI_MANAGER_UV_CACHE_DIR:-/var/cache/kanami/uv}"
readonly WEB_ADMIN_EXECUTABLE="${KANAMI_MANAGER_WEB_ADMIN_EXECUTABLE:-/var/lib/kanami-web/.venv/bin/kanami-web-admin}"

DOCTOR_FAILURES=0
DOCTOR_UPDATE_FAILURES=0
UPDATE_INVOKED=0

show_help() {
    cat <<'EOF'
Kanami Manager

Usage: kanami [command]

Commands:
  help       Show this help message
  version    Show manager version information
  status     Show a short read-only installation summary
  doctor     Run detailed read-only installation diagnostics
  logs       Show recent Kanami bot logs
  restart    Restart the main Kanami bot service
  start      Start the main Kanami bot service
  stop       Stop the main Kanami bot service
  update     Run the trusted production updater
  web-setup  Activate a complete Web Admin installation for production
  menu       Open the interactive menu

Logs usage: kanami logs [--lines N]
Update usage: sudo kanami update
Web setup usage: sudo kanami web-setup
EOF
}

show_menu_options() {
    cat <<'EOF'
Kanami Manager

1. Status
2. Doctor
3. Version
4. Help
5. Restart bot
6. Logs
7. Start bot
8. Stop bot
9. Update
10. Activate Web Admin for production
0. Exit
EOF
}

confirm_restart() {
    local answer

    printf 'Restart kanami.service? [y/N]: '
    if ! IFS= read -r answer; then
        printf '\nRestart cancelled.\n'
        return 1
    fi
    answer="${answer%$'\r'}"
    case "${answer}" in
        y | Y | yes | YES)
            return 0
            ;;
        *)
            printf 'Restart cancelled.\n'
            return 1
            ;;
    esac
}

confirm_stop() {
    local answer

    printf 'Stop kanami.service? [y/N]: '
    if ! IFS= read -r answer; then
        printf '\nStop cancelled.\n'
        return 1
    fi
    answer="${answer%$'\r'}"
    case "${answer}" in
        y | Y | yes | YES)
            return 0
            ;;
        *)
            printf 'Stop cancelled.\n'
            return 1
            ;;
    esac
}

confirm_update() {
    local answer

    printf 'Run Kanami update? [y/N]: '
    if ! IFS= read -r answer; then
        printf '\nUpdate cancelled.\n'
        return 1
    fi
    answer="${answer%$'\r'}"
    case "${answer}" in
        y | Y | yes | YES)
            return 0
            ;;
        *)
            printf 'Update cancelled.\n'
            return 1
            ;;
    esac
}

show_menu() {
    local choice
    local update_status

    while true; do
        show_menu_options
        printf 'Select an option [0-10]: '
        if ! IFS= read -r choice; then
            printf '\nEnd of input; exiting.\n'
            return 0
        fi
        choice="${choice%$'\r'}"
        printf '\n'
        case "${choice}" in
            1)
                show_status
                ;;
            2)
                show_doctor || true
                ;;
            3)
                show_version
                ;;
            4)
                show_help
                ;;
            5)
                if confirm_restart; then
                    if ! restart_bot; then
                        printf 'Restart failed.\n'
                    fi
                fi
                ;;
            6)
                if ! show_logs; then
                    printf 'Unable to show logs.\n' >&2
                fi
                ;;
            7)
                if ! start_bot; then
                    printf 'Start failed.\n'
                fi
                ;;
            8)
                if confirm_stop; then
                    if ! stop_bot; then
                        printf 'Stop failed.\n'
                    fi
                fi
                ;;
            9)
                if confirm_update; then
                    update_status=0
                    if run_update; then
                        update_status=0
                    else
                        update_status=$?
                    fi
                    if ((UPDATE_INVOKED == 1)); then
                        if ((update_status == 0)); then
                            printf 'Update completed.\n'
                        else
                            printf 'Update failed with exit code %d; installation may be partially updated.\n' \
                                "${update_status}" >&2
                            printf 'Review the updater output, then run kanami status and kanami doctor.\n' \
                                >&2
                        fi
                        printf 'The installed Manager may have been refreshed; ending this menu session.\n'
                        printf "Run 'kanami' again to start a fresh menu session.\n"
                        return "${update_status}"
                    fi
                    printf 'Update was not started.\n'
                fi
                ;;
            10)
                if ! run_web_setup; then
                    printf 'Web Admin production activation failed or was declined.\n' >&2
                fi
                ;;
            0)
                printf 'Goodbye.\n'
                return 0
                ;;
            *)
                printf 'Invalid choice: %s. Select a number from 0 to 10.\n' \
                    "${choice}"
                ;;
        esac
        printf '\n'
    done
}

is_kanami_checkout_candidate() {
    local candidate="$1"

    [[ -f "${candidate}/pyproject.toml" ]] && \
        [[ -f "${candidate}/scripts/manager.sh" ]] && \
        [[ -f "${candidate}/deploy/kanami.service" ]]
}

checkout_path() {
    local script_dir
    local source_checkout

    if [[ -n ${KANAMI_MANAGER_INSTALL_DIR+x} ]]; then
        printf '%s\n' "${INSTALL_DIR}"
        return 0
    fi

    script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
    source_checkout="$(cd -- "${script_dir}/.." && pwd)"
    if is_kanami_checkout_candidate "${source_checkout}"; then
        printf '%s\n' "${source_checkout}"
    else
        printf '%s\n' "${INSTALL_DIR}"
    fi
}

git_readonly() {
    local checkout="$1"
    shift

    git --no-optional-locks -c safe.directory="${checkout}" \
        -C "${checkout}" "$@"
}

git_repository_available() {
    local checkout="$1"
    local result

    result="$(git_readonly "${checkout}" rev-parse --is-inside-work-tree \
        2>/dev/null)" || return 1
    [[ ${result} == "true" ]]
}

git_commit() {
    local checkout="$1"
    local commit

    commit="$(git_readonly "${checkout}" rev-parse --short HEAD \
        2>/dev/null)" || return 1
    [[ ${commit} =~ ^[0-9a-fA-F]+$ ]] || return 1
    printf '%s\n' "${commit}"
}

git_branch() {
    local checkout="$1"
    local branch

    branch="$(git_readonly "${checkout}" symbolic-ref --quiet --short HEAD \
        2>/dev/null)" || return 1
    [[ -n ${branch} ]] || return 1
    printf '%s\n' "${branch}"
}

systemctl_available() {
    command -v systemctl >/dev/null 2>&1
}

unit_load_state() {
    local unit="$1"
    local state

    state="$(systemctl show "${unit}" \
        --property=LoadState --value 2>/dev/null)" || return 1
    state="${state%%$'\n'*}"
    case "${state}" in
        loaded | masked | not-found | error | bad-setting)
            printf '%s\n' "${state}"
            ;;
        *)
            return 1
            ;;
    esac
}

unit_active_state() {
    local unit="$1"
    local state

    state="$(systemctl is-active "${unit}" 2>/dev/null)" || true
    state="${state%%$'\n'*}"
    case "${state}" in
        active | reloading | activating | deactivating | inactive | failed | maintenance)
            printf '%s\n' "${state}"
            ;;
        *)
            return 1
            ;;
    esac
}

logs_usage_error() {
    printf '%s: %s\n' "${PROGRAM_NAME}" "$1" >&2
    printf 'Usage: %s logs [--lines N]\n' "${PROGRAM_NAME}" >&2
    return 2
}

show_logs() {
    local lines="${DEFAULT_LOG_LINES}"

    if (($# > 0)); then
        if [[ $1 != "--lines" ]]; then
            logs_usage_error "unexpected logs argument: $1"
            return 2
        fi
        if (($# < 2)); then
            logs_usage_error "--lines requires a value"
            return 2
        fi
        if (($# > 2)); then
            logs_usage_error "unexpected logs argument: $3"
            return 2
        fi
        lines="$2"
    fi

    if [[ ! ${lines} =~ ^[0-9]+$ ]]; then
        logs_usage_error "--lines must be an integer from 1 to ${MAX_LOG_LINES}"
        return 2
    fi
    if ((${#lines} > 4)) || \
        ((10#${lines} < 1 || 10#${lines} > MAX_LOG_LINES)); then
        logs_usage_error "--lines must be between 1 and ${MAX_LOG_LINES}"
        return 2
    fi
    lines="$((10#${lines}))"
    if [[ ! -x ${JOURNALCTL} ]]; then
        printf '%s: cannot show logs: %s is unavailable\n' \
            "${PROGRAM_NAME}" "${JOURNALCTL}" >&2
        return 1
    fi

    "${JOURNALCTL}" -u "${BOT_SERVICE}" -n "${lines}" --no-pager
}

lifecycle_usage_error() {
    local action="$1"

    printf '%s: %s does not accept arguments\n' \
        "${PROGRAM_NAME}" "${action}" >&2
    printf 'Usage: sudo %s %s\n' "${PROGRAM_NAME}" "${action}" >&2
    return 2
}

validate_update_path_metadata() {
    local path="$1"
    local label="$2"
    local metadata
    local uid
    local gid
    local mode
    local extra

    metadata="$("${UPDATE_STAT}" --format='%u %g %a' -- "${path}")" || {
        printf '%s: update trust check failed: cannot inspect %s\n' \
            "${PROGRAM_NAME}" "${label}" >&2
        return 1
    }
    IFS=' ' read -r uid gid mode extra <<< "${metadata}"
    if [[ ! ${uid} =~ ^[0-9]+$ || ! ${gid} =~ ^[0-9]+$ || \
        ! ${mode} =~ ^[0-7]{3,4}$ || -n ${extra} ]]; then
        printf '%s: update trust check failed: invalid metadata for %s\n' \
            "${PROGRAM_NAME}" "${label}" >&2
        return 1
    fi
    if [[ ${uid} != "0" || ${gid} != "0" ]]; then
        printf '%s: update trust check failed: %s must be owned by UID 0 and GID 0\n' \
            "${PROGRAM_NAME}" "${label}" >&2
        return 1
    fi
    if (((8#${mode} & 8#022) != 0)); then
        printf '%s: update trust check failed: %s is writable by group or other\n' \
            "${PROGRAM_NAME}" "${label}" >&2
        return 1
    fi
}

validate_update_directory() {
    local path="$1"
    local label="$2"

    if [[ ! -d ${path} ]]; then
        printf '%s: update trust check failed: %s is missing or is not a directory\n' \
            "${PROGRAM_NAME}" "${label}" >&2
        return 1
    fi
    if [[ -L ${path} ]]; then
        printf '%s: update trust check failed: %s must not be a symlink\n' \
            "${PROGRAM_NAME}" "${label}" >&2
        return 1
    fi
    validate_update_path_metadata "${path}" "${label}"
}

validate_update_script() {
    if [[ ! -f ${UPDATE_SCRIPT} || ! -r ${UPDATE_SCRIPT} ]]; then
        printf '%s: update trust check failed: updater is missing or is not a readable regular file\n' \
            "${PROGRAM_NAME}" >&2
        return 1
    fi
    if [[ -L ${UPDATE_SCRIPT} ]]; then
        printf '%s: update trust check failed: updater must not be a symlink\n' \
            "${PROGRAM_NAME}" >&2
        return 1
    fi
    validate_update_path_metadata "${UPDATE_SCRIPT}" "updater"
}

validate_web_setup_script() {
    local metadata

    if [[ ! -f ${WEB_SETUP_SCRIPT} || ! -r ${WEB_SETUP_SCRIPT} || \
        ! -x ${WEB_SETUP_SCRIPT} ]]; then
        printf '%s: web-setup trust check failed: canonical setup script is missing or not executable\n' \
            "${PROGRAM_NAME}" >&2
        return 1
    fi
    if [[ -L ${WEB_SETUP_SCRIPT} ]]; then
        printf '%s: web-setup trust check failed: canonical setup script must not be a symlink\n' \
            "${PROGRAM_NAME}" >&2
        return 1
    fi
    metadata="$("${UPDATE_STAT}" --format='%u %g %a' -- "${WEB_SETUP_SCRIPT}")" || {
        printf '%s: web-setup trust check failed: cannot inspect canonical setup script\n' \
            "${PROGRAM_NAME}" >&2
        return 1
    }
    [[ ${metadata} == "0 0 755" ]] || {
        printf '%s: web-setup trust check failed: canonical setup script must be root:root 0755\n' \
            "${PROGRAM_NAME}" >&2
        return 1
    }
}

validate_update_bootstrap_trust() {
    validate_update_directory "${UPDATE_CHECKOUT}" "production checkout" || \
        return 1
    validate_update_directory "${UPDATE_SCRIPTS_DIR}" "scripts directory" || \
        return 1
    validate_update_script
}

run_update() {
    UPDATE_INVOKED=0
    if (( EUID != 0 )); then
        printf '%s: update requires root; run: sudo %s update\n' \
            "${PROGRAM_NAME}" "${PROGRAM_NAME}" >&2
        return 1
    fi
    if [[ ! -x ${UPDATE_BASH} ]]; then
        printf '%s: cannot update: %s is unavailable\n' \
            "${PROGRAM_NAME}" "${UPDATE_BASH}" >&2
        return 1
    fi
    if [[ ! -x ${UPDATE_STAT} ]]; then
        printf '%s: cannot update: %s is unavailable\n' \
            "${PROGRAM_NAME}" "${UPDATE_STAT}" >&2
        return 1
    fi
    validate_update_bootstrap_trust || return 1

    UPDATE_INVOKED=1
    "${UPDATE_BASH}" "${UPDATE_SCRIPT}"
}

run_web_setup() {
    if ((EUID != 0)); then
        printf '%s: web-setup requires root; run: sudo %s web-setup\n' \
            "${PROGRAM_NAME}" "${PROGRAM_NAME}" >&2
        return 1
    fi
    if [[ ! -x ${UPDATE_BASH} ]]; then
        printf '%s: cannot run web-setup: %s is unavailable\n' \
            "${PROGRAM_NAME}" "${UPDATE_BASH}" >&2
        return 1
    fi
    if [[ ! -x ${UPDATE_STAT} ]]; then
        printf '%s: cannot run web-setup: %s is unavailable\n' \
            "${PROGRAM_NAME}" "${UPDATE_STAT}" >&2
        return 1
    fi
    validate_update_directory "${UPDATE_CHECKOUT}" "production checkout" || return 1
    validate_update_directory "${UPDATE_SCRIPTS_DIR}" "scripts directory" || return 1
    validate_web_setup_script || return 1

    "${UPDATE_BASH}" "${WEB_SETUP_SCRIPT}"
}

validate_bot_lifecycle_action() {
    local action="$1"
    local load_state

    if [[ ${EUID} -ne 0 ]]; then
        printf '%s: %s requires root; run: sudo %s %s\n' \
            "${PROGRAM_NAME}" "${action}" "${PROGRAM_NAME}" "${action}" >&2
        return 1
    fi
    if [[ ! -x ${MUTATING_SYSTEMCTL} ]]; then
        printf '%s: cannot %s %s: %s is unavailable\n' \
            "${PROGRAM_NAME}" "${action}" "${BOT_SERVICE}" \
            "${MUTATING_SYSTEMCTL}" >&2
        return 1
    fi
    load_state="$("${MUTATING_SYSTEMCTL}" show "${BOT_SERVICE}" \
        --property=LoadState --value 2>/dev/null)" || {
        printf '%s: cannot verify %s load state before %s\n' \
            "${PROGRAM_NAME}" "${BOT_SERVICE}" "${action}" >&2
        return 1
    }
    load_state="${load_state%%$'\n'*}"
    if [[ ${load_state} == "loaded" ]]; then
        return 0
    fi
    case "${load_state}" in
        not-found)
            printf '%s: cannot %s %s: unit is not installed\n' \
                "${PROGRAM_NAME}" "${action}" "${BOT_SERVICE}" >&2
            ;;
        masked | error | bad-setting)
            printf '%s: cannot %s %s: abnormal load state: %s\n' \
                "${PROGRAM_NAME}" "${action}" "${BOT_SERVICE}" \
                "${load_state}" >&2
            ;;
        *)
            printf '%s: cannot %s %s: load state unavailable\n' \
                "${PROGRAM_NAME}" "${action}" "${BOT_SERVICE}" >&2
            ;;
    esac
    return 1
}

start_bot() {
    validate_bot_lifecycle_action start || return 1

    if "${MUTATING_SYSTEMCTL}" is-active --quiet "${BOT_SERVICE}"; then
        printf '%s is already active.\n' "${BOT_SERVICE}"
        return 0
    fi
    if ! "${MUTATING_SYSTEMCTL}" start "${BOT_SERVICE}"; then
        printf '%s: failed to start %s\n' \
            "${PROGRAM_NAME}" "${BOT_SERVICE}" >&2
        return 1
    fi
    if ! "${MUTATING_SYSTEMCTL}" is-active --quiet "${BOT_SERVICE}"; then
        printf '%s: %s is not active after start\n' \
            "${PROGRAM_NAME}" "${BOT_SERVICE}" >&2
        return 1
    fi

    printf '%s started successfully.\n' "${BOT_SERVICE}"
}

stop_bot() {
    local active_state

    validate_bot_lifecycle_action stop || return 1

    if ! "${MUTATING_SYSTEMCTL}" stop "${BOT_SERVICE}"; then
        printf '%s: failed to stop %s\n' \
            "${PROGRAM_NAME}" "${BOT_SERVICE}" >&2
        return 1
    fi
    active_state="$("${MUTATING_SYSTEMCTL}" is-active \
        "${BOT_SERVICE}" 2>/dev/null)" || true
    active_state="${active_state%%$'\n'*}"
    if [[ ${active_state} != "inactive" ]]; then
        if [[ -z ${active_state} ]]; then
            active_state="unknown"
        fi
        printf '%s: %s state after stop is %s, expected inactive\n' \
            "${PROGRAM_NAME}" "${BOT_SERVICE}" "${active_state}" >&2
        return 1
    fi

    printf '%s stopped successfully.\n' "${BOT_SERVICE}"
}

restart_bot() {
    local load_state

    if ((EUID != 0)); then
        printf '%s: restart requires root; run: sudo kanami restart\n' \
            "${PROGRAM_NAME}" >&2
        return 1
    fi
    if [[ ! -x ${MUTATING_SYSTEMCTL} ]]; then
        printf '%s: cannot restart %s: %s is unavailable\n' \
            "${PROGRAM_NAME}" "${BOT_SERVICE}" "${MUTATING_SYSTEMCTL}" >&2
        return 1
    fi
    load_state="$("${MUTATING_SYSTEMCTL}" show "${BOT_SERVICE}" \
        --property=LoadState --value 2>/dev/null)" || {
        printf '%s: cannot verify %s load state\n' \
            "${PROGRAM_NAME}" "${BOT_SERVICE}" >&2
        return 1
    }
    load_state="${load_state%%$'\n'*}"
    if [[ ${load_state} != "loaded" ]]; then
        case "${load_state}" in
            not-found)
                printf '%s: cannot restart %s: unit is not installed\n' \
                    "${PROGRAM_NAME}" "${BOT_SERVICE}" >&2
                ;;
            masked | error | bad-setting)
                printf '%s: cannot restart %s: abnormal load state: %s\n' \
                    "${PROGRAM_NAME}" "${BOT_SERVICE}" "${load_state}" >&2
                ;;
            *)
                printf '%s: cannot restart %s: load state unavailable\n' \
                    "${PROGRAM_NAME}" "${BOT_SERVICE}" >&2
                ;;
        esac
        return 1
    fi

    if ! "${MUTATING_SYSTEMCTL}" restart "${BOT_SERVICE}"; then
        printf '%s: failed to restart %s\n' \
            "${PROGRAM_NAME}" "${BOT_SERVICE}" >&2
        return 1
    fi
    if ! "${MUTATING_SYSTEMCTL}" is-active --quiet "${BOT_SERVICE}"; then
        printf '%s: %s is not active after restart\n' \
            "${PROGRAM_NAME}" "${BOT_SERVICE}" >&2
        return 1
    fi

    printf '%s restarted successfully.\n' "${BOT_SERVICE}"
}

service_summary() {
    local unit="$1"
    local optional="${2:-false}"
    local load_state
    local active_state

    if ! systemctl_available; then
        printf 'unknown (systemctl unavailable)'
        return 0
    fi
    load_state="$(unit_load_state "${unit}")" || {
        printf 'unknown (state unavailable)'
        return 0
    }
    case "${load_state}" in
        loaded)
            active_state="$(unit_active_state "${unit}")" || {
                printf 'unknown (state unavailable)'
                return 0
            }
            printf '%s' "${active_state}"
            ;;
        not-found)
            if [[ ${optional} == "true" ]]; then
                printf 'not installed (optional)'
            else
                printf 'not installed'
            fi
            ;;
        masked | error | bad-setting)
            printf '%s (abnormal load state)' "${load_state}"
            ;;
    esac
}

show_version() {
    local checkout
    local commit

    printf '%s\n' "${MANAGER_NAME}"
    checkout="$(checkout_path)"
    if [[ -d ${checkout} ]] && git_repository_available "${checkout}" && \
        commit="$(git_commit "${checkout}")"; then
        printf 'Git commit: %s\n' "${commit}"
    fi
}

show_status() {
    local checkout
    local commit="unavailable"
    local branch="unavailable"

    checkout="$(checkout_path)"
    printf '%s status\n' "${MANAGER_NAME}"
    if [[ -d ${checkout} ]]; then
        printf 'Checkout: found\n'
        printf 'Checkout path: %s\n' "${checkout}"
        if git_repository_available "${checkout}"; then
            commit="$(git_commit "${checkout}")" || commit="unavailable"
            branch="$(git_branch "${checkout}")" || branch="unavailable"
        fi
    else
        printf 'Checkout: not found\n'
        printf 'Checkout path: %s (not found)\n' "${checkout}"
    fi
    printf 'Git commit: %s\n' "${commit}"
    printf 'Git branch: %s\n' "${branch}"
    printf 'kanami.service: %s\n' "$(service_summary kanami.service)"
    printf 'kanami-web-admin.service: %s\n' \
        "$(service_summary kanami-web-admin.service true)"
}

doctor_result() {
    local level="$1"
    local check="$2"
    local detail="$3"

    printf '[%s] %s: %s\n' "${level}" "${check}" "${detail}"
    if [[ ${level} == "FAIL" ]]; then
        DOCTOR_FAILURES=$((DOCTOR_FAILURES + 1))
    fi
}

doctor_check_checkout() {
    local checkout="$1"

    if [[ -d ${checkout} ]]; then
        doctor_result OK "Checkout" "found at ${checkout}"
        return 0
    fi
    doctor_result FAIL "Checkout" "not found or inaccessible at ${checkout}"
    return 1
}

doctor_check_git() {
    local checkout="$1"
    local dirty

    if [[ ! -d ${checkout} ]]; then
        doctor_result SKIP "Git repository" "checkout unavailable"
        doctor_result SKIP "Git working tree" "checkout unavailable"
        doctor_result SKIP "Git origin" "checkout unavailable"
        return 0
    fi
    if ! command -v git >/dev/null 2>&1; then
        doctor_result FAIL "Git repository" "git command unavailable"
        doctor_result SKIP "Git working tree" "git command unavailable"
        doctor_result SKIP "Git origin" "git command unavailable"
        return 0
    fi
    if ! git_repository_available "${checkout}"; then
        doctor_result FAIL "Git repository" "not a readable Git repository"
        doctor_result SKIP "Git working tree" "Git repository unavailable"
        doctor_result SKIP "Git origin" "Git repository unavailable"
        return 0
    fi

    doctor_result OK "Git repository" "valid"
    if dirty="$(git_readonly "${checkout}" status --porcelain \
        2>/dev/null)"; then
        if [[ -z ${dirty} ]]; then
            doctor_result OK "Git working tree" "clean"
        else
            doctor_result FAIL "Git working tree" "local changes detected"
        fi
    else
        doctor_result FAIL "Git working tree" "could not inspect"
    fi
    if git_readonly "${checkout}" remote get-url origin \
        >/dev/null 2>&1; then
        doctor_result OK "Git origin" "configured"
    else
        doctor_result FAIL "Git origin" "missing or inaccessible"
    fi
}

doctor_check_executable() {
    local path="$1"
    local check="$2"
    local required="$3"

    if [[ -x ${path} ]]; then
        doctor_result OK "${check}" "executable at ${path}"
    elif [[ ${required} == "true" ]]; then
        doctor_result FAIL "${check}" "missing or not executable at ${path}"
    else
        doctor_result WARN "${check}" "optional component missing at ${path}"
    fi
}

doctor_check_directory() {
    local path="$1"
    local check="$2"

    if [[ -d ${path} ]]; then
        doctor_result OK "${check}" "found at ${path}"
    else
        doctor_result FAIL "${check}" "missing or inaccessible at ${path}"
    fi
}

doctor_trust_result() {
    local level="$1"
    local check="$2"
    local detail="$3"

    doctor_result "${level}" "${check}" "${detail}"
    if [[ ${level} == "FAIL" ]]; then
        DOCTOR_UPDATE_FAILURES=$((DOCTOR_UPDATE_FAILURES + 1))
    fi
}

doctor_check_trusted_directory() {
    local path="$1"
    local check="$2"
    local metadata
    local uid
    local gid
    local mode
    local extra

    if [[ -L ${path} ]]; then
        doctor_trust_result FAIL "${check}" "must not be a symlink: ${path}"
        return 1
    fi
    if [[ ! -d ${path} ]]; then
        doctor_trust_result FAIL "${check}" \
            "missing or not a directory: ${path}"
        return 1
    fi
    if [[ ! -x ${UPDATE_STAT} ]]; then
        doctor_trust_result FAIL "${check}" \
            "ownership unavailable: ${UPDATE_STAT} is unavailable"
        return 1
    fi
    metadata="$("${UPDATE_STAT}" --format='%u %g %a' -- "${path}" \
        2>/dev/null)" || {
        doctor_trust_result FAIL "${check}" \
            "could not inspect ownership at ${path}"
        return 1
    }
    IFS=' ' read -r uid gid mode extra <<< "${metadata}"
    if [[ ! ${uid} =~ ^[0-9]+$ || ! ${gid} =~ ^[0-9]+$ || \
        ! ${mode} =~ ^[0-7]{3,4}$ || -n ${extra} ]]; then
        doctor_trust_result FAIL "${check}" "invalid ownership metadata"
        return 1
    fi
    if [[ ${uid} != "0" || ${gid} != "0" ]]; then
        doctor_trust_result FAIL "${check}" \
            "non-canonical ownership UID ${uid} GID ${gid}; privileged update must not run; manual review/migration/reinstall from trusted source required"
        return 1
    fi
    if (((8#${mode} & 8#022) != 0)); then
        doctor_trust_result FAIL "${check}" \
            "group/other writable (mode ${mode}); privileged update must not run until trust is restored"
        return 1
    fi
    doctor_trust_result OK "${check}" "trusted root-owned directory (mode ${mode})"
}

doctor_check_trusted_file() {
    local path="$1"
    local check="$2"
    local metadata
    local uid
    local gid
    local mode
    local extra

    if [[ -L ${path} ]]; then
        doctor_trust_result FAIL "${check}" "must not be a symlink: ${path}"
        return 1
    fi
    if [[ ! -f ${path} || ! -r ${path} ]]; then
        doctor_trust_result FAIL "${check}" \
            "missing or not a readable regular file: ${path}"
        return 1
    fi
    if [[ ! -x ${UPDATE_STAT} ]]; then
        doctor_trust_result FAIL "${check}" \
            "ownership unavailable: ${UPDATE_STAT} is unavailable"
        return 1
    fi
    metadata="$("${UPDATE_STAT}" --format='%u %g %a' -- "${path}" \
        2>/dev/null)" || {
        doctor_trust_result FAIL "${check}" \
            "could not inspect ownership at ${path}"
        return 1
    }
    IFS=' ' read -r uid gid mode extra <<< "${metadata}"
    if [[ ! ${uid} =~ ^[0-9]+$ || ! ${gid} =~ ^[0-9]+$ || \
        ! ${mode} =~ ^[0-7]{3,4}$ || -n ${extra} ]]; then
        doctor_trust_result FAIL "${check}" "invalid ownership metadata"
        return 1
    fi
    if [[ ${uid} != "0" || ${gid} != "0" ]]; then
        doctor_trust_result FAIL "${check}" \
            "non-canonical ownership UID ${uid} GID ${gid}; privileged update must not run; manual review/migration/reinstall from trusted source required"
        return 1
    fi
    if (((8#${mode} & 8#022) != 0)); then
        doctor_trust_result FAIL "${check}" \
            "group/other writable (mode ${mode}); privileged update must not run until trust is restored"
        return 1
    fi
    doctor_trust_result OK "${check}" "trusted root-owned file (mode ${mode})"
}

doctor_check_production_venv() {
    local metadata
    local owner
    local group
    local mode
    local extra

    if [[ -L ${PRODUCTION_VENV} ]]; then
        doctor_trust_result FAIL "Production project venv" \
            "writable exception must not be a symlink: ${PRODUCTION_VENV}"
        return 1
    fi
    if [[ ! -d ${PRODUCTION_VENV} ]]; then
        doctor_trust_result FAIL "Production project venv" \
            "writable exception is missing or not a directory: ${PRODUCTION_VENV}"
        return 1
    fi
    if [[ ! -x ${UPDATE_STAT} ]]; then
        doctor_trust_result FAIL "Production project venv" \
            "ownership unavailable: ${UPDATE_STAT} is unavailable"
        return 1
    fi
    metadata="$("${UPDATE_STAT}" --format='%U %G %a' -- \
        "${PRODUCTION_VENV}" 2>/dev/null)" || {
        doctor_trust_result FAIL "Production project venv" \
            "could not inspect writable exception ownership"
        return 1
    }
    IFS=' ' read -r owner group mode extra <<< "${metadata}"
    if [[ ${owner} != "kanami" || ${group} != "kanami" || \
        ! ${mode} =~ ^[0-7]{3,4}$ || -n ${extra} ]]; then
        doctor_trust_result FAIL "Production project venv" \
            "expected kanami:kanami writable exception, got ${owner:-unknown}:${group:-unknown} mode ${mode:-unknown}"
        return 1
    fi
    if (((8#${mode} & 8#200) == 0)); then
        doctor_trust_result FAIL "Production project venv" \
            "owner kanami lacks write permission (mode ${mode})"
        return 1
    fi
    doctor_trust_result OK "Production project venv" \
        "allowed kanami:kanami writable exception (mode ${mode})"
}

doctor_check_production_trust() {
    DOCTOR_UPDATE_FAILURES=0
    printf 'Production trust:\n'

    if [[ -x ${UPDATE_BASH} ]]; then
        doctor_trust_result OK "Production bash" "available at ${UPDATE_BASH}"
    else
        doctor_trust_result FAIL "Production bash" "unavailable at ${UPDATE_BASH}"
    fi
    if [[ -x ${UPDATE_STAT} ]]; then
        doctor_trust_result OK "Production stat" "available at ${UPDATE_STAT}"
    else
        doctor_trust_result FAIL "Production stat" "unavailable at ${UPDATE_STAT}"
    fi

    doctor_check_trusted_directory "${UPDATE_CHECKOUT}" \
        "Production checkout" || true
    doctor_check_trusted_directory "${PRODUCTION_GIT_DIR}" \
        "Production Git metadata" || true
    doctor_check_trusted_directory "${UPDATE_SCRIPTS_DIR}" \
        "Production scripts directory" || true
    doctor_check_trusted_file "${UPDATE_SCRIPT}" "Production updater" || true
    doctor_check_trusted_file "${PRODUCTION_MANAGER_SOURCE}" \
        "Production Manager source" || true
    doctor_check_trusted_file "${PRODUCTION_SERVICE_UNIT}" \
        "Production service unit" || true
    doctor_check_production_venv || true
    if [[ -x ${PRODUCTION_BOT_EXECUTABLE} ]]; then
        doctor_trust_result OK "Production bot executable" \
            "executable at ${PRODUCTION_BOT_EXECUTABLE}"
    else
        doctor_trust_result FAIL "Production bot executable" \
            "missing or not executable at ${PRODUCTION_BOT_EXECUTABLE}"
    fi

    if ((DOCTOR_UPDATE_FAILURES == 0)); then
        doctor_result OK "Update readiness" "READY"
    else
        doctor_result FAIL "Update readiness" \
            "NOT READY; restore trust through manual review/migration/reinstall from trusted source"
    fi
}

doctor_check_units() {
    local bot_load_state
    local bot_active_state
    local web_load_state
    local web_active_state

    if ! systemctl_available; then
        doctor_result WARN "systemctl" "command unavailable"
        doctor_result WARN "kanami.service unit" "required unit could not be verified"
        doctor_result SKIP "kanami.service active" "systemctl unavailable"
        doctor_result WARN "kanami-web-admin.service unit" \
            "optional unit could not be verified"
        doctor_result SKIP "kanami-web-admin.service active" \
            "systemctl unavailable"
        return 0
    fi

    bot_load_state="$(unit_load_state kanami.service)" || \
        bot_load_state="unknown"
    case "${bot_load_state}" in
        loaded)
            doctor_result OK "kanami.service unit" "installed (load state: loaded)"
            bot_active_state="$(unit_active_state kanami.service)" || \
                bot_active_state="unknown"
            if [[ ${bot_active_state} == "active" ]]; then
                doctor_result OK "kanami.service active" "active"
            elif [[ ${bot_active_state} == "unknown" ]]; then
                doctor_result WARN "kanami.service active" "state unavailable"
            else
                doctor_result FAIL "kanami.service active" "${bot_active_state}"
            fi
            ;;
        not-found)
            doctor_result FAIL "kanami.service unit" "not installed"
            doctor_result SKIP "kanami.service active" "unit not installed"
            ;;
        masked | error | bad-setting)
            doctor_result FAIL "kanami.service unit" \
                "abnormal load state: ${bot_load_state}"
            doctor_result SKIP "kanami.service active" \
                "load state is ${bot_load_state}"
            ;;
        unknown)
            doctor_result WARN "kanami.service unit" "state unavailable"
            doctor_result SKIP "kanami.service active" "unit state unavailable"
            ;;
    esac

    web_load_state="$(unit_load_state kanami-web-admin.service)" || \
        web_load_state="unknown"
    case "${web_load_state}" in
        loaded)
            doctor_result OK "kanami-web-admin.service unit" \
                "installed (load state: loaded)"
            web_active_state="$(unit_active_state kanami-web-admin.service)" || \
                web_active_state="unknown"
            if [[ ${web_active_state} == "active" ]]; then
                doctor_result OK "kanami-web-admin.service active" "active"
            else
                doctor_result WARN "kanami-web-admin.service active" \
                    "optional service ${web_active_state}"
            fi
            ;;
        not-found)
            doctor_result WARN "kanami-web-admin.service unit" \
                "optional unit not installed"
            doctor_result SKIP "kanami-web-admin.service active" \
                "optional unit not installed"
            ;;
        masked | error | bad-setting)
            doctor_result WARN "kanami-web-admin.service unit" \
                "optional unit has abnormal load state: ${web_load_state}"
            doctor_result SKIP "kanami-web-admin.service active" \
                "load state is ${web_load_state}"
            ;;
        unknown)
            doctor_result WARN "kanami-web-admin.service unit" \
                "optional unit state unavailable"
            doctor_result SKIP "kanami-web-admin.service active" \
                "unit state unavailable"
            ;;
    esac
}

show_doctor() {
    local checkout

    DOCTOR_FAILURES=0
    checkout="$(checkout_path)"
    printf '%s doctor\n' "${MANAGER_NAME}"
    doctor_check_checkout "${checkout}" || true
    doctor_check_git "${checkout}"
    doctor_check_executable "${checkout}/.venv/bin/discord-stats-bot" \
        "Bot executable" true
    doctor_check_executable "${WEB_ADMIN_EXECUTABLE}" \
        "Web Admin executable" false
    doctor_check_executable "${UV_BOOTSTRAP_DIR}/bin/uv" \
        "uv bootstrap" true
    doctor_check_directory "${UV_CACHE_DIR}" "uv cache"
    doctor_check_units
    doctor_check_production_trust

    if ((DOCTOR_FAILURES == 0)); then
        printf 'Overall: HEALTHY\n'
        return 0
    fi
    printf 'Overall: UNHEALTHY\n'
    return 1
}

unknown_command() {
    printf '%s: unknown command: %s\n' "${PROGRAM_NAME}" "$1" >&2
    printf "Run '%s help' for usage.\n" "${PROGRAM_NAME}" >&2
    return 2
}

main() {
    local command

    if (($# == 0)); then
        if [[ -t 0 && -t 1 ]]; then
            show_menu || return $?
        else
            show_help
        fi
        return 0
    fi
    command="$1"

    case "${command}" in
        help | -h | --help)
            show_help
            ;;
        version)
            show_version
            ;;
        status)
            show_status
            ;;
        doctor)
            show_doctor
            ;;
        logs)
            shift
            show_logs "$@"
            ;;
        restart)
            restart_bot
            ;;
        start)
            shift
            if (($# > 0)); then
                lifecycle_usage_error start
            else
                start_bot
            fi
            ;;
        stop)
            shift
            if (($# > 0)); then
                lifecycle_usage_error stop
            else
                stop_bot
            fi
            ;;
        update)
            shift
            if (($# > 0)); then
                lifecycle_usage_error update
            else
                run_update
            fi
            ;;
        web-setup)
            shift
            if (($# > 0)); then
                lifecycle_usage_error web-setup
            else
                run_web_setup
            fi
            ;;
        menu)
            show_menu
            ;;
        *)
            unknown_command "${command}"
            ;;
    esac
}

main "$@"
