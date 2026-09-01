#!/usr/bin/env bash
set -Eeuo pipefail

readonly PROGRAM_NAME="kanami"
readonly MANAGER_NAME="Kanami Manager"
readonly MUTATING_SYSTEMCTL="/usr/bin/systemctl"
readonly BOT_SERVICE="kanami.service"
# D2.2 read-only test seams; future mutating commands must not trust these paths.
readonly INSTALL_DIR="${KANAMI_MANAGER_INSTALL_DIR:-/opt/kanami}"
readonly UV_BOOTSTRAP_DIR="${KANAMI_MANAGER_UV_BOOTSTRAP_DIR:-/opt/kanami-uv}"
readonly UV_CACHE_DIR="${KANAMI_MANAGER_UV_CACHE_DIR:-/var/cache/kanami/uv}"

DOCTOR_FAILURES=0

show_help() {
    cat <<'EOF'
Kanami Manager

Usage: kanami [command]

Commands:
  help       Show this help message
  version    Show manager version information
  status     Show a short read-only installation summary
  doctor     Run detailed read-only installation diagnostics
  restart    Restart the main Kanami bot service
  menu       Open the interactive menu
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

show_menu() {
    local choice

    while true; do
        show_menu_options
        printf 'Select an option [0-5]: '
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
            0)
                printf 'Goodbye.\n'
                return 0
                ;;
            *)
                printf 'Invalid choice: %s. Select a number from 0 to 5.\n' \
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
    doctor_check_executable "${checkout}/.venv/bin/kanami-web-admin" \
        "Web Admin executable" false
    doctor_check_executable "${UV_BOOTSTRAP_DIR}/bin/uv" \
        "uv bootstrap" true
    doctor_check_directory "${UV_CACHE_DIR}" "uv cache"
    doctor_check_units

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
            show_menu
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
        restart)
            restart_bot
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
