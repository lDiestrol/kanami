#!/usr/bin/env bash
set -Eeuo pipefail

readonly INSTALL_DIR="/opt/kanami"
readonly UV_BOOTSTRAP_DIR="/opt/kanami-uv"
readonly UV_CACHE_DIR="/var/cache/kanami/uv"
readonly SERVICE_HOME="/var/lib/kanami"
readonly CONFIG_FILE="/etc/kanami/kanami.env"
readonly SERVICE_FILE="/etc/systemd/system/kanami.service"
readonly SERVICE_USER="kanami"

log() {
    printf '[kanami] %s\n' "$*"
}

fail() {
    printf '[kanami] ERROR: %s\n' "$*" >&2
    exit 1
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
foreign_owned="$(find "${INSTALL_DIR}" -xdev ! -user "${SERVICE_USER}" \
    -print -quit)"
[[ -z ${foreign_owned} ]] || \
    fail "install tree contains files not owned by ${SERVICE_USER}: ${foreign_owned}"

dirty="$(runuser -u "${SERVICE_USER}" -- env HOME="${SERVICE_HOME}" \
    git -C "${INSTALL_DIR}" status --porcelain)"
[[ -z ${dirty} ]] || fail "Git working tree has local changes; update aborted"

database_url="$(awk -F= '$1 == "DATABASE_URL" {sub(/^[^=]*=/, ""); print; exit}' \
    "${CONFIG_FILE}")"
[[ -n ${database_url} ]] || fail "DATABASE_URL is empty or missing"

old_commit="$(runuser -u "${SERVICE_USER}" -- env HOME="${SERVICE_HOME}" \
    git -C "${INSTALL_DIR}" rev-parse --short HEAD)"
log "Updating clean checkout from commit ${old_commit}"
runuser -u "${SERVICE_USER}" -- env HOME="${SERVICE_HOME}" \
    git -C "${INSTALL_DIR}" pull --ff-only

log "Synchronizing locked dependencies"
(
    cd "${INSTALL_DIR}"
    runuser -u "${SERVICE_USER}" -- env HOME="${SERVICE_HOME}" \
        UV_CACHE_DIR="${UV_CACHE_DIR}" \
        "${UV_BOOTSTRAP_DIR}/bin/uv" sync --frozen --no-dev
)
[[ -x "${INSTALL_DIR}/.venv/bin/discord-stats-bot" ]] || \
    fail "uv sync did not create the discord-stats-bot console script"

log "Applying Alembic migrations"
(
    cd "${INSTALL_DIR}"
    runuser -u "${SERVICE_USER}" -- env HOME="${SERVICE_HOME}" \
        DATABASE_URL="${database_url}" \
        "${INSTALL_DIR}/.venv/bin/alembic" -c alembic.ini upgrade head
)
unset database_url

log "Updating systemd unit and restarting Kanami"
install -m 0644 "${INSTALL_DIR}/deploy/kanami.service" "${SERVICE_FILE}"
systemctl daemon-reload
systemctl restart kanami
systemctl is-active --quiet kanami || fail "kanami.service is not active"

new_commit="$(runuser -u "${SERVICE_USER}" -- env HOME="${SERVICE_HOME}" \
    git -C "${INSTALL_DIR}" rev-parse --short HEAD)"
log "Update complete: ${old_commit} -> ${new_commit}; kanami.service is active"
