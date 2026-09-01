#!/usr/bin/env bash
set -Eeuo pipefail

readonly INSTALL_DIR="/opt/kanami"
readonly UV_BOOTSTRAP_DIR="/opt/kanami-uv"
readonly UV_CACHE_DIR="/var/cache/kanami/uv"
readonly SERVICE_HOME="/var/lib/kanami"
readonly CONFIG_DIR="/etc/kanami"
readonly CONFIG_FILE="${CONFIG_DIR}/kanami.env"
readonly SERVICE_FILE="/etc/systemd/system/kanami.service"
readonly MANAGER_FILE="/usr/local/bin/kanami"
readonly SERVICE_USER="kanami"
readonly DB_NAME="discord_stats_prod"
readonly DB_ROLE="kanami_app"
readonly UV_VERSION="0.12.3"
readonly MAX_DISCORD_SNOWFLAKE="18446744073709551615"

discord_token=""
discord_guild_id=""
report_timezone=""

log() {
    printf '[kanami] %s\n' "$*"
}

fail() {
    printf '[kanami] ERROR: %s\n' "$*" >&2
    exit 1
}

install_manager() {
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

require_configuration_tty() {
    exec 3<>/dev/tty || \
        fail "interactive core configuration requires a usable terminal (/dev/tty)"
    [[ -t 3 ]] || \
        fail "interactive core configuration requires a usable terminal (/dev/tty)"
}

is_valid_discord_token() {
    local value="${1-}"

    [[ -n ${value} && ${value} =~ ^[A-Za-z0-9._-]+$ ]]
}

is_valid_discord_guild_id() {
    local value="${1-}"
    local normalized
    local LC_ALL=C

    [[ ${value} =~ ^[0-9]+$ ]] || return 1
    normalized="${value}"
    while [[ ${normalized} == 0* ]]; do
        normalized="${normalized#0}"
    done
    [[ -n ${normalized} ]] || return 1
    ((${#normalized} < ${#MAX_DISCORD_SNOWFLAKE})) && return 0
    ((${#normalized} == ${#MAX_DISCORD_SNOWFLAKE})) || return 1
    [[ ${normalized} < ${MAX_DISCORD_SNOWFLAKE} || \
        ${normalized} == "${MAX_DISCORD_SNOWFLAKE}" ]]
}

is_valid_report_timezone() {
    local value="${1-}"

    [[ -n ${value} && ${value} != *[[:space:]]* ]] || return 1
    python3 - "${value}" <<'PY'
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    ZoneInfo(sys.argv[1])
except (ValueError, ZoneInfoNotFoundError):
    raise SystemExit(1)
PY
}

read_hidden_token() {
    while true; do
        printf 'Discord Bot Token: ' >&3
        if ! IFS= read -r -s -u 3 discord_token; then
            printf '\n' >&3
            fail "could not read Discord Bot Token from the terminal"
        fi
        printf '\n' >&3
        if is_valid_discord_token "${discord_token}"; then
            return 0
        fi
        printf '%s\n' \
            "Invalid Discord Bot Token: enter a non-empty token containing only letters, digits, '.', '_' and '-'." \
            >&3
    done
}

read_discord_guild_id() {
    while true; do
        printf 'Discord Guild ID: ' >&3
        if ! IFS= read -r -u 3 discord_guild_id; then
            fail "could not read Discord Guild ID from the terminal"
        fi
        if is_valid_discord_guild_id "${discord_guild_id}"; then
            return 0
        fi
        printf '%s\n' \
            "Invalid Discord Guild ID: enter decimal digits from 1 to ${MAX_DISCORD_SNOWFLAKE}." \
            >&3
    done
}

read_report_timezone() {
    local entered_timezone

    printf 'Report timezone [UTC]: ' >&3
    if ! IFS= read -r -u 3 entered_timezone; then
        fail "could not read report timezone from the terminal"
    fi
    report_timezone="${entered_timezone:-UTC}"
}

validate_report_timezone() {
    while ! is_valid_report_timezone "${report_timezone}"; do
        printf '%s\n' \
            'Invalid report timezone: enter an existing IANA timezone such as UTC or Europe/Stockholm.' \
            >&3
        read_report_timezone
    done
}

show_configuration_summary() {
    printf '%s\n' \
        '' \
        'Kanami core configuration' \
        'Discord Bot Token: configured (hidden)' \
        "Discord Guild ID: ${discord_guild_id}" \
        "Report timezone: ${report_timezone}" \
        '' >&3
}

confirm_installation() {
    local answer

    while true; do
        printf 'Continue installation? [Y/n]: ' >&3
        if ! IFS= read -r -u 3 answer; then
            fail "could not read installation confirmation from the terminal"
        fi
        case ${answer} in
            "" | y | Y | yes | YES)
                return 0
                ;;
            n | N | no | NO)
                return 1
                ;;
            *)
                printf '%s\n' 'Please answer yes or no.' >&3
                ;;
        esac
    done
}

main() {
    set +x
[[ ${EUID} -eq 0 ]] || fail "run this installer with sudo"

readonly SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "${SOURCE_DIR}/pyproject.toml" ]] || fail "run from a Kanami checkout"
[[ -d "${SOURCE_DIR}/.git" ]] || fail "source directory is not a Git checkout"
source_dirty="$(git -c safe.directory="${SOURCE_DIR}" -C "${SOURCE_DIR}" \
    status --porcelain)"
[[ -z ${source_dirty} ]] || \
    fail "source Git working tree has local changes; install from a clean checkout"

[[ -r /etc/os-release ]] || fail "cannot identify the operating system"
# shellcheck disable=SC1091
source /etc/os-release
[[ ${ID:-} == "debian" && ${VERSION_ID:-} == "13" ]] || \
    fail "this installer supports Debian 13 only"

[[ ! -e "${INSTALL_DIR}" ]] || \
    fail "${INSTALL_DIR} already exists; use scripts/update.sh or inspect it manually"
[[ ! -e "${UV_BOOTSTRAP_DIR}" ]] || \
    fail "${UV_BOOTSTRAP_DIR} already exists; inspect the partial installation"
[[ ! -e "${CONFIG_FILE}" ]] || \
    fail "${CONFIG_FILE} already exists; it will not be overwritten"

require_configuration_tty
printf '%s\n' '' 'Kanami core configuration' '' >&3
read_hidden_token
read_discord_guild_id
read_report_timezone

log "Installing Debian packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates git openssl postgresql postgresql-client \
    python3 python3-pip python3-venv tzdata

validate_report_timezone
show_configuration_summary
if ! confirm_installation; then
    unset discord_token
    exec 3>&-
    log "Installation cancelled before creating Kanami database, checkout, or configuration"
    return 0
fi
exec 3>&-

log "Starting local PostgreSQL"
systemctl enable --now postgresql

role_exists="$(runuser -u postgres -- psql -tAc \
    "SELECT 1 FROM pg_roles WHERE rolname = '${DB_ROLE}'")"
database_exists="$(runuser -u postgres -- psql -tAc \
    "SELECT 1 FROM pg_database WHERE datname = '${DB_NAME}'")"
if [[ -n ${role_exists} || -n ${database_exists} ]]; then
    fail "PostgreSQL role/database already exists; configure it manually per docs/INSTALL.md"
fi

if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    log "Creating system user ${SERVICE_USER}"
    useradd --system --user-group --home-dir "${SERVICE_HOME}" \
        --shell /usr/sbin/nologin "${SERVICE_USER}"
elif ! getent group "${SERVICE_USER}" >/dev/null; then
    fail "user ${SERVICE_USER} exists without group ${SERVICE_USER}; inspect it manually"
fi

install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_USER}" \
    "${SERVICE_HOME}"
install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_USER}" \
    "${UV_BOOTSTRAP_DIR}" "${UV_CACHE_DIR}"

remote_url="$(git -c safe.directory="${SOURCE_DIR}" -C "${SOURCE_DIR}" \
    remote get-url origin)" || fail "source checkout has no origin remote"

log "Installing committed checkout into ${INSTALL_DIR}"
(
    umask 022
    git clone --local --no-hardlinks "${SOURCE_DIR}" "${INSTALL_DIR}"
)
[[ -d "${INSTALL_DIR}/.git" ]] || fail "installed checkout is missing .git"
git -c safe.directory="${INSTALL_DIR}" -C "${INSTALL_DIR}" \
    remote set-url origin "${remote_url}"
# The checkout remains root-owned; only the ignored project environment is writable.
install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_USER}" \
    "${INSTALL_DIR}/.venv"

log "Installing uv ${UV_VERSION} and locked application dependencies"
runuser -u "${SERVICE_USER}" -- python3 -m venv "${UV_BOOTSTRAP_DIR}"
runuser -u "${SERVICE_USER}" -- \
    "${UV_BOOTSTRAP_DIR}/bin/pip" install \
    --disable-pip-version-check --no-cache-dir "uv==${UV_VERSION}"
(
    cd "${INSTALL_DIR}"
    runuser -u "${SERVICE_USER}" -- env HOME="${SERVICE_HOME}" \
        UV_CACHE_DIR="${UV_CACHE_DIR}" \
        "${UV_BOOTSTRAP_DIR}/bin/uv" sync --frozen --no-dev
)
[[ -x "${INSTALL_DIR}/.venv/bin/discord-stats-bot" ]] || \
    fail "uv sync did not create the discord-stats-bot console script"

log "Creating PostgreSQL role and database"
db_password="$(openssl rand -hex 32)"
sql_file="$(mktemp)"
cleanup() {
    rm -f -- "${sql_file}"
}
trap cleanup EXIT
chmod 0600 "${sql_file}"
printf "CREATE ROLE %s LOGIN PASSWORD '%s';\n" \
    "${DB_ROLE}" "${db_password}" >"${sql_file}"
chown postgres:postgres "${sql_file}"
runuser -u postgres -- psql --set=ON_ERROR_STOP=1 --file="${sql_file}" postgres \
    >/dev/null
runuser -u postgres -- createdb --owner="${DB_ROLE}" "${DB_NAME}"

database_url="postgresql+asyncpg://${DB_ROLE}:${db_password}@127.0.0.1:5432/${DB_NAME}"

log "Writing protected core configuration"
install -d -m 0750 -o root -g "${SERVICE_USER}" "${CONFIG_DIR}"
install -m 0640 -o root -g "${SERVICE_USER}" /dev/null "${CONFIG_FILE}"
{
    printf 'DISCORD_TOKEN=%s\n' "${discord_token}"
    printf 'DISCORD_GUILD_ID=%s\n' "${discord_guild_id}"
    printf '%s\n' \
        '' \
        '# Optional Discord features; uncomment and set an ID to enable.' \
        '# DISCORD_AUDIT_LOG_CHANNEL_ID=123456789012345678' \
        '# DISCORD_AUTOROLE_ID=123456789012345678' \
        '# DISCORD_ANNIVERSARY_CHANNEL_ID=123456789012345678' \
        '# DISCORD_RETURN_CHANNEL_ID=123456789012345678' \
        ''
    printf 'DATABASE_URL=%s\n' "${database_url}"
    printf 'REPORT_TIMEZONE=%s\n' "${report_timezone}"
    printf '%s\n' \
        'VOICE_MIN_SESSION_SECONDS=10' \
        'VOICE_CHECKPOINT_INTERVAL_SECONDS=60' \
        'MEMBER_RETURN_MIN_ABSENCE_SECONDS=86400' \
        'AUDIT_TRANSIENT_RETENTION_DAYS=90' \
        'RAW_MESSAGE_RETENTION_DAYS=90' \
        'SERVER_EVENT_RETENTION_DAYS=365' \
        'LOG_LEVEL=INFO'
} >"${CONFIG_FILE}"
unset discord_token
unset db_password

log "Applying Alembic migrations"
(
    cd "${INSTALL_DIR}"
    runuser -u "${SERVICE_USER}" -- env HOME="${SERVICE_HOME}" \
        DATABASE_URL="${database_url}" \
        "${INSTALL_DIR}/.venv/bin/alembic" -c alembic.ini upgrade head
)
unset database_url

log "Installing Kanami Manager command"
install_manager

log "Installing systemd unit without starting the bot"
install_service_unit
systemctl daemon-reload

log "Installation complete. Next steps:"
printf '%s\n' \
    "  1. kanami doctor" \
    "  2. Review ${CONFIG_FILE} with sudoedit if optional settings are needed" \
    "  3. sudo systemctl enable kanami" \
    "  4. sudo kanami start" \
    "  5. kanami logs"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
