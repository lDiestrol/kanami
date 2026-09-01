#!/usr/bin/env bash
set -Eeuo pipefail

readonly INSTALL_DIR="/opt/kanami"
readonly UV_BOOTSTRAP_DIR="/opt/kanami-uv"
readonly UV_CACHE_DIR="/var/cache/kanami/uv"
readonly SERVICE_HOME="/var/lib/kanami"
readonly CONFIG_FILE="/etc/kanami/kanami.env"
readonly SERVICE_FILE="/etc/systemd/system/kanami.service"
readonly MANAGER_FILE="/usr/local/bin/kanami"
readonly SERVICE_USER="kanami"
readonly WEB_SERVICE_USER="kanami-web"
readonly WEB_SERVICE_HOME="/var/lib/kanami-web"
readonly WEB_VENV_DIR="${WEB_SERVICE_HOME}/.venv"
readonly WEB_UV_BOOTSTRAP_DIR="${WEB_SERVICE_HOME}/uv"
readonly WEB_UV_CACHE_DIR="${WEB_SERVICE_HOME}/.cache/uv"
readonly WEB_CONFIG_FILE="/etc/kanami/kanami-web-admin.env"
readonly WEB_SERVICE_FILE="/etc/systemd/system/kanami-web-admin.service"
readonly WEB_GRANTS_SOURCE_RELATIVE="deploy/postgresql/kanami-web-admin-grants.sql"
readonly DB_NAME="discord_stats_prod"
web_admin_installed="false"

log() {
    printf '[kanami] %s\n' "$*"
}

fail() {
    printf '[kanami] ERROR: %s\n' "$*" >&2
    exit 1
}

refresh_manager() {
    local manager_source="${INSTALL_DIR}/scripts/manager.sh"

    [[ -f ${manager_source} && -r ${manager_source} && \
        ! -L ${manager_source} ]] || \
        fail "installed checkout manager source is not a regular readable file"
    install -m 0755 -o root -g root "${manager_source}" "${MANAGER_FILE}"
}

install_service_unit() {
    local service_source="${INSTALL_DIR}/deploy/kanami.service"

    [[ -f ${service_source} && -r ${service_source} && \
        ! -L ${service_source} ]] || \
        fail "installed checkout service unit source is not a regular readable file"
    install -m 0644 -o root -g root "${service_source}" "${SERVICE_FILE}"
}

install_web_service_unit() {
    local service_source="${INSTALL_DIR}/deploy/systemd/kanami-web-admin.service"

    [[ -f ${service_source} && -r ${service_source} && \
        ! -L ${service_source} ]] || \
        fail "installed checkout Web Admin unit source is not a regular readable file"
    install -m 0644 -o root -g root "${service_source}" "${WEB_SERVICE_FILE}"
}

validate_checkout_ownership() {
    local unexpected_source
    local writable_source
    local unexpected_venv

    [[ -d ${INSTALL_DIR} && ! -L ${INSTALL_DIR} ]] || \
        fail "production checkout must be a regular directory"
    [[ -d "${INSTALL_DIR}/.venv" && ! -L "${INSTALL_DIR}/.venv" ]] || \
        fail "project environment is missing or is not a regular directory"

    unexpected_source="$(find -P "${INSTALL_DIR}" -xdev \
        -path "${INSTALL_DIR}/.venv" -prune -o \
        \( ! -uid 0 -o ! -gid 0 \) -print -quit)"
    [[ -z ${unexpected_source} ]] || \
        fail "production checkout source must be root-owned: ${unexpected_source}"

    writable_source="$(find -P "${INSTALL_DIR}" -xdev \
        -path "${INSTALL_DIR}/.venv" -prune -o \
        ! -type l -perm /022 -print -quit)"
    [[ -z ${writable_source} ]] || \
        fail "production checkout source is group/other writable: ${writable_source}"

    unexpected_venv="$(find -P "${INSTALL_DIR}/.venv" -xdev \
        \( ! -user "${SERVICE_USER}" -o ! -group "${SERVICE_USER}" \) \
        -print -quit)"
    [[ -z ${unexpected_venv} ]] || \
        fail "project environment must be owned by ${SERVICE_USER}: ${unexpected_venv}"
    runuser -u "${SERVICE_USER}" -- test -w "${INSTALL_DIR}/.venv" || \
        fail "project environment is not writable by ${SERVICE_USER}"
}

validate_path_metadata() {
    local path="$1"
    local expected="$2"
    local description="$3"
    local actual

    actual="$(stat -c '%u:%g:%a' -- "${path}")" || \
        fail "cannot inspect ${description} metadata"
    [[ ${actual} == "${expected}" ]] || \
        fail "${description} must have owner/group/mode ${expected}; found ${actual}"
}

validate_web_systemd_state() {
    local load_state="$1"
    local active_state="$2"

    [[ ${load_state} == "loaded" ]] || \
        fail "Web Admin systemd unit is not reliably loaded; update aborted"
    case ${active_state} in
        inactive)
            ;;
        active)
            fail "Web Admin is active; stop kanami-web-admin.service before updating. D2.12 does not manage Web Admin lifecycle automatically."
            ;;
        *)
            fail "Web Admin state is ${active_state:-unknown}, not reliably inactive; update aborted"
            ;;
    esac
}

preflight_optional_web_installation() {
    local marker_count=0
    local marker
    local web_uid
    local web_gid
    local web_primary_group
    local load_state
    local active_state
    local -a path_markers=(
        "${WEB_SERVICE_HOME}"
        "${WEB_VENV_DIR}"
        "${WEB_UV_BOOTSTRAP_DIR}"
        "${WEB_UV_CACHE_DIR}"
        "${WEB_CONFIG_FILE}"
        "${WEB_SERVICE_FILE}"
    )

    if id "${WEB_SERVICE_USER}" >/dev/null 2>&1; then
        marker_count=$((marker_count + 1))
    fi
    for marker in "${path_markers[@]}"; do
        if [[ -e ${marker} || -L ${marker} ]]; then
            marker_count=$((marker_count + 1))
        fi
    done
    if ((marker_count == 0)); then
        log "Optional Web Admin installation not present; skipping Web Admin pre-flight"
        web_admin_installed="false"
        return 0
    fi
    ((marker_count == ${#path_markers[@]} + 1)) || \
        fail "partial Web Admin installation detected; inspect canonical user, home, env, unit and runtime manually"

    [[ -d ${WEB_SERVICE_HOME} && ! -L ${WEB_SERVICE_HOME} ]] || \
        fail "Web Admin home is not a regular directory"
    [[ -d ${WEB_VENV_DIR} && ! -L ${WEB_VENV_DIR} ]] || \
        fail "Web Admin environment is not a regular directory"
    [[ -d ${WEB_UV_BOOTSTRAP_DIR} && ! -L ${WEB_UV_BOOTSTRAP_DIR} ]] || \
        fail "Web Admin uv bootstrap is not a regular directory"
    [[ -d ${WEB_UV_CACHE_DIR} && ! -L ${WEB_UV_CACHE_DIR} ]] || \
        fail "Web Admin uv cache is not a regular directory"
    [[ -f ${WEB_CONFIG_FILE} && ! -L ${WEB_CONFIG_FILE} ]] || \
        fail "Web Admin environment file is not a regular file"
    [[ -f ${WEB_SERVICE_FILE} && ! -L ${WEB_SERVICE_FILE} ]] || \
        fail "Web Admin systemd unit is not a regular file"

    web_uid="$(id -u "${WEB_SERVICE_USER}")" || \
        fail "cannot resolve Web Admin user UID"
    web_gid="$(id -g "${WEB_SERVICE_USER}")" || \
        fail "cannot resolve Web Admin user GID"
    web_primary_group="$(id -gn "${WEB_SERVICE_USER}")" || \
        fail "cannot resolve Web Admin primary group"
    [[ ${web_primary_group} == "${WEB_SERVICE_USER}" ]] || \
        fail "Web Admin primary group must be ${WEB_SERVICE_USER}; found ${web_primary_group}"
    for marker in "${WEB_SERVICE_HOME}" "${WEB_VENV_DIR}" \
        "${WEB_UV_BOOTSTRAP_DIR}" "${WEB_UV_CACHE_DIR}"; do
        validate_path_metadata "${marker}" "${web_uid}:${web_gid}:750" \
            "Web Admin directory ${marker}"
        runuser -u "${WEB_SERVICE_USER}" -- test -w "${marker}" || \
            fail "Web Admin directory is not writable by ${WEB_SERVICE_USER}: ${marker}"
    done
    validate_path_metadata "${WEB_CONFIG_FILE}" "0:${web_gid}:640" \
        "Web Admin environment file"
    validate_path_metadata "${WEB_SERVICE_FILE}" "0:0:644" \
        "Web Admin systemd unit"
    runuser -u "${WEB_SERVICE_USER}" -- test -x \
        "${WEB_UV_BOOTSTRAP_DIR}/bin/uv" || \
        fail "Web Admin uv bootstrap is not executable by ${WEB_SERVICE_USER}"

    load_state="$(systemctl show --property=LoadState --value \
        kanami-web-admin.service)" || \
        fail "cannot determine Web Admin systemd unit load state; update aborted"
    active_state="$(systemctl show --property=ActiveState --value \
        kanami-web-admin.service)" || \
        fail "cannot determine Web Admin active state; update aborted"
    validate_web_systemd_state "${load_state}" "${active_state}"
    web_admin_installed="true"
}

refresh_optional_web_runtime() {
    [[ ${web_admin_installed} == "true" ]] || return 0

    log "Synchronizing optional Web Admin locked dependencies"
    (
        cd "${INSTALL_DIR}"
        runuser -u "${WEB_SERVICE_USER}" -- env HOME="${WEB_SERVICE_HOME}" \
            VIRTUAL_ENV="${WEB_VENV_DIR}" UV_CACHE_DIR="${WEB_UV_CACHE_DIR}" \
            "${WEB_UV_BOOTSTRAP_DIR}/bin/uv" sync \
            --active --frozen --no-dev
    )
    [[ -x "${WEB_VENV_DIR}/bin/kanami-web-admin" ]] || \
        fail "Web Admin sync did not create the kanami-web-admin console script"
}

apply_web_database_grants() {
    local grants_source="${INSTALL_DIR}/${WEB_GRANTS_SOURCE_RELATIVE}"

    [[ ${web_admin_installed} == "true" ]] || return 0
    [[ -f ${grants_source} && -r ${grants_source} && ! -L ${grants_source} ]] || \
        fail "installed Web Admin PostgreSQL grant policy is not a regular readable file"
    runuser -u postgres -- psql --set=ON_ERROR_STOP=1 \
        --file="${grants_source}" "${DB_NAME}" >/dev/null
}

[[ ${EUID} -eq 0 ]] || fail "run this updater with sudo"
[[ -d "${INSTALL_DIR}/.git" ]] || fail "${INSTALL_DIR} is not a Git checkout"
[[ -x "${UV_BOOTSTRAP_DIR}/bin/uv" ]] || \
    fail "uv bootstrap is missing; repair the installation manually"
[[ -d "${UV_CACHE_DIR}" ]] || fail "uv cache directory is missing"
[[ -d "${SERVICE_HOME}" ]] || fail "service home is missing"
[[ -r "${CONFIG_FILE}" ]] || fail "${CONFIG_FILE} is missing or unreadable"
id "${SERVICE_USER}" >/dev/null 2>&1 || fail "system user ${SERVICE_USER} is missing"
runuser -u "${SERVICE_USER}" -- test -x "${UV_BOOTSTRAP_DIR}/bin/uv" || \
    fail "uv is not executable by ${SERVICE_USER}"
runuser -u "${SERVICE_USER}" -- test -w "${UV_CACHE_DIR}" || \
    fail "uv cache is not writable by ${SERVICE_USER}"
runuser -u "${SERVICE_USER}" -- test -w "${SERVICE_HOME}" || \
    fail "service home is not writable by ${SERVICE_USER}"
validate_checkout_ownership
preflight_optional_web_installation

dirty="$(git -c safe.directory="${INSTALL_DIR}" -C "${INSTALL_DIR}" \
    status --porcelain)"
[[ -z ${dirty} ]] || fail "Git working tree has local changes; update aborted"

database_url="$(awk -F= '$1 == "DATABASE_URL" {sub(/^[^=]*=/, ""); print; exit}' \
    "${CONFIG_FILE}")"
[[ -n ${database_url} ]] || fail "DATABASE_URL is empty or missing"

old_commit="$(git -c safe.directory="${INSTALL_DIR}" -C "${INSTALL_DIR}" \
    rev-parse --short HEAD)"
log "Updating clean checkout from commit ${old_commit}"
(
    umask 022
    git -c safe.directory="${INSTALL_DIR}" -C "${INSTALL_DIR}" pull --ff-only
)

log "Refreshing Kanami Manager command"
refresh_manager

log "Synchronizing locked dependencies"
(
    cd "${INSTALL_DIR}"
    runuser -u "${SERVICE_USER}" -- env HOME="${SERVICE_HOME}" \
        UV_CACHE_DIR="${UV_CACHE_DIR}" \
        "${UV_BOOTSTRAP_DIR}/bin/uv" sync --frozen --no-dev
)
[[ -x "${INSTALL_DIR}/.venv/bin/discord-stats-bot" ]] || \
    fail "uv sync did not create the discord-stats-bot console script"

refresh_optional_web_runtime

log "Applying Alembic migrations"
(
    cd "${INSTALL_DIR}"
    runuser -u "${SERVICE_USER}" -- env HOME="${SERVICE_HOME}" \
        DATABASE_URL="${database_url}" \
        "${INSTALL_DIR}/.venv/bin/alembic" -c alembic.ini upgrade head
)
unset database_url

if [[ ${web_admin_installed} == "true" ]]; then
    log "Reapplying least-privilege Web Admin PostgreSQL grants"
    apply_web_database_grants
fi

log "Updating systemd unit and restarting Kanami"
install_service_unit
if [[ ${web_admin_installed} == "true" ]]; then
    log "Refreshing inactive Web Admin systemd unit without starting it"
    install_web_service_unit
fi
systemctl daemon-reload
systemctl restart kanami
systemctl is-active --quiet kanami || fail "kanami.service is not active"

new_commit="$(git -c safe.directory="${INSTALL_DIR}" -C "${INSTALL_DIR}" \
    rev-parse --short HEAD)"
log "Update complete: ${old_commit} -> ${new_commit}; kanami.service is active"
