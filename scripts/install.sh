#!/usr/bin/env bash
set -Eeuo pipefail

readonly INSTALL_DIR="/opt/kanami"
readonly UV_BOOTSTRAP_DIR="/opt/kanami-uv"
readonly UV_CACHE_DIR="/var/cache/kanami/uv"
readonly SERVICE_HOME="/var/lib/kanami"
readonly CONFIG_DIR="/etc/kanami"
readonly CONFIG_FILE="${CONFIG_DIR}/kanami.env"
readonly SERVICE_FILE="/etc/systemd/system/kanami.service"
readonly WEB_CONFIG_FILE="${CONFIG_DIR}/kanami-web-admin.env"
readonly WEB_SERVICE_FILE="/etc/systemd/system/kanami-web-admin.service"
readonly MANAGER_FILE="/usr/local/bin/kanami"
readonly SERVICE_USER="kanami"
readonly WEB_SERVICE_USER="kanami-web"
readonly WEB_SERVICE_HOME="/var/lib/kanami-web"
readonly WEB_VENV_DIR="${WEB_SERVICE_HOME}/.venv"
readonly WEB_UV_BOOTSTRAP_DIR="${WEB_SERVICE_HOME}/uv"
readonly WEB_UV_CACHE_DIR="${WEB_SERVICE_HOME}/.cache/uv"
readonly WEB_GRANTS_SOURCE_RELATIVE="deploy/postgresql/kanami-web-admin-grants.sql"
readonly DB_NAME="discord_stats_prod"
readonly DB_ROLE="kanami_app"
readonly WEB_DB_ROLE="kanami_web_readonly"
readonly UV_VERSION="0.12.3"
readonly MAX_DISCORD_SNOWFLAKE="18446744073709551615"

discord_token=""
discord_guild_id=""
report_timezone=""
configure_web_admin="false"
installation_confirmed="false"
partial_warning_reported="false"
installer_main_bashpid=""
web_oauth_client_id=""
web_oauth_client_secret=""
web_oauth_redirect_uri=""
web_owner_ids=""

log() {
    printf '[kanami] %s\n' "$*"
}

fail() {
    printf '[kanami] ERROR: %s\n' "$*" >&2
    report_partial_installation
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

install_web_service_unit() {
    local service_source="${INSTALL_DIR}/deploy/systemd/kanami-web-admin.service"

    [[ -f ${service_source} && -r ${service_source} && \
        ! -L ${service_source} ]] || \
        fail "installed checkout Web Admin unit source is not a regular readable file"
    install -m 0644 -o root -g root "${service_source}" "${WEB_SERVICE_FILE}"
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

is_valid_web_oauth_secret() {
    local value="${1-}"

    [[ -n ${value} && ${value} =~ ^[A-Za-z0-9._-]+$ ]]
}

is_valid_web_redirect_uri() {
    local value="${1-}"

    python3 - "${value}" <<'PY'
import sys
from urllib.parse import urlsplit

try:
    parsed = urlsplit(sys.argv[1])
    port = parsed.port
except ValueError:
    raise SystemExit(1)

if (
    parsed.scheme != "https"
    or not parsed.hostname
    or parsed.username is not None
    or parsed.password is not None
    or parsed.query
    or parsed.fragment
    or parsed.path != "/admin/auth/discord/callback"
    or (port is not None and not 1 <= port <= 65535)
):
    raise SystemExit(1)
PY
}

normalize_web_owner_ids() {
    local value="${1-}"
    local remaining
    local item
    local canonical=""
    local -A seen=()

    [[ -n ${value} ]] || return 1
    remaining="${value},"
    while [[ ${remaining} == *,* ]]; do
        item="${remaining%%,*}"
        remaining="${remaining#*,}"
        while [[ ${item} == [[:space:]]* ]]; do
            item="${item#?}"
        done
        while [[ ${item} == *[[:space:]] ]]; do
            item="${item%?}"
        done
        [[ -n ${item} ]] || return 1
        is_valid_discord_guild_id "${item}" || return 1
        while [[ ${item} == 0* ]]; do
            item="${item#0}"
        done
        if [[ -z ${seen[${item}]+configured} ]]; then
            seen[${item}]=1
            canonical="${canonical:+${canonical},}${item}"
        fi
    done
    [[ -z ${remaining} && -n ${canonical} ]] || return 1
    web_owner_ids="${canonical}"
}

validate_web_installation_absent() {
    [[ ! -e ${WEB_SERVICE_HOME} ]] || \
        fail "${WEB_SERVICE_HOME} already exists; inspect the partial Web Admin installation"
    [[ ! -e ${WEB_CONFIG_FILE} ]] || \
        fail "${WEB_CONFIG_FILE} already exists; it will not be overwritten"
    [[ ! -e ${WEB_SERVICE_FILE} ]] || \
        fail "${WEB_SERVICE_FILE} already exists; it will not be overwritten"
    if id "${WEB_SERVICE_USER}" >/dev/null 2>&1; then
        fail "system user ${WEB_SERVICE_USER} already exists; inspect it manually"
    fi
}

create_web_service_user() {
    log "Creating isolated Web Admin system user ${WEB_SERVICE_USER}"
    useradd --system --user-group --home-dir "${WEB_SERVICE_HOME}" \
        --shell /usr/sbin/nologin "${WEB_SERVICE_USER}"
    install -d -m 0750 -o "${WEB_SERVICE_USER}" -g "${WEB_SERVICE_USER}" \
        "${WEB_SERVICE_HOME}" "${WEB_VENV_DIR}" "${WEB_UV_BOOTSTRAP_DIR}" \
        "${WEB_UV_CACHE_DIR}"
}

sync_web_runtime() {
    log "Installing isolated Web Admin runtime with uv ${UV_VERSION}"
    runuser -u "${WEB_SERVICE_USER}" -- python3 -m venv "${WEB_UV_BOOTSTRAP_DIR}"
    runuser -u "${WEB_SERVICE_USER}" -- \
        "${WEB_UV_BOOTSTRAP_DIR}/bin/pip" install \
        --disable-pip-version-check --no-cache-dir "uv==${UV_VERSION}"
    runuser -u "${WEB_SERVICE_USER}" -- python3 -m venv "${WEB_VENV_DIR}"
    (
        cd "${INSTALL_DIR}"
        runuser -u "${WEB_SERVICE_USER}" -- env HOME="${WEB_SERVICE_HOME}" \
            VIRTUAL_ENV="${WEB_VENV_DIR}" UV_CACHE_DIR="${WEB_UV_CACHE_DIR}" \
            "${WEB_UV_BOOTSTRAP_DIR}/bin/uv" sync \
            --active --frozen --no-dev
    )
    [[ -x "${WEB_VENV_DIR}/bin/kanami-web-admin" ]] || \
        fail "Web Admin uv sync did not create the kanami-web-admin console script"
}

configure_web_git_metadata() {
    log "Configuring read-only Git metadata access for ${WEB_SERVICE_USER}"
    runuser -u "${WEB_SERVICE_USER}" -- env HOME="${WEB_SERVICE_HOME}" \
        git config --global --add safe.directory "${INSTALL_DIR}"
    runuser -u "${WEB_SERVICE_USER}" -- env HOME="${WEB_SERVICE_HOME}" \
        git -C "${INSTALL_DIR}" rev-parse --short HEAD >/dev/null
}

apply_web_database_grants() {
    local grants_source="${INSTALL_DIR}/${WEB_GRANTS_SOURCE_RELATIVE}"

    [[ -f ${grants_source} && -r ${grants_source} && ! -L ${grants_source} ]] || \
        fail "installed Web Admin PostgreSQL grant policy is not a regular readable file"
    runuser -u postgres -- psql --set=ON_ERROR_STOP=1 \
        --file="${grants_source}" "${DB_NAME}" >/dev/null
}

write_web_configuration() {
    local web_database_url="$1"

    log "Writing protected Web Admin configuration"
    install -m 0640 -o root -g "${WEB_SERVICE_USER}" /dev/null \
        "${WEB_CONFIG_FILE}"
    {
        printf 'DATABASE_URL=%s\n' "${web_database_url}"
        printf 'DISCORD_GUILD_ID=%s\n' "${discord_guild_id}"
        printf 'REPORT_TIMEZONE=%s\n' "${report_timezone}"
        printf '%s\n' \
            'VOICE_MIN_SESSION_SECONDS=10' \
            'VOICE_CHECKPOINT_INTERVAL_SECONDS=60' \
            'GAME_TRACKING_ENABLED=false' \
            'GAME_CONFIRM_INTERVAL_SECONDS=60' \
            'WEB_ADMIN_HOST=127.0.0.1' \
            'WEB_ADMIN_PORT=8000'
        printf 'WEB_ADMIN_DISCORD_CLIENT_ID=%s\n' "${web_oauth_client_id}"
        printf 'WEB_ADMIN_DISCORD_CLIENT_SECRET=%s\n' \
            "${web_oauth_client_secret}"
        printf 'WEB_ADMIN_DISCORD_REDIRECT_URI=%s\n' \
            "${web_oauth_redirect_uri}"
        printf 'WEB_ADMIN_ALLOWED_USER_IDS=%s\n' "${web_owner_ids}"
        printf '%s\n' \
            'WEB_ADMIN_COOKIE_SECURE=true' \
            'WEB_ADMIN_SESSION_LIFETIME_SECONDS=28800' \
            'LOG_LEVEL=INFO'
    } >"${WEB_CONFIG_FILE}"
    unset web_oauth_client_secret
}

report_partial_installation() {
    local status=$?

    if [[ ${installation_confirmed} == "true" && \
        ${partial_warning_reported} == "false" && \
        ${BASHPID} == "${installer_main_bashpid}" ]]; then
        printf '%s\n' \
            '[kanami] ERROR: installation failed after final confirmation and may be partially completed; inspect PostgreSQL, /opt/kanami, /etc/kanami and systemd state manually' \
            >&2
        partial_warning_reported="true"
    fi
    return "${status}"
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

read_web_admin_choice() {
    local answer

    while true; do
        printf 'Configure optional Web Admin? [y/N]: ' >&3
        if ! IFS= read -r -u 3 answer; then
            fail "could not read Web Admin choice from the terminal"
        fi
        case ${answer} in
            y | Y | yes | YES)
                configure_web_admin="true"
                return 0
                ;;
            "" | n | N | no | NO)
                configure_web_admin="false"
                return 0
                ;;
            *)
                printf '%s\n' 'Please answer yes or no.' >&3
                ;;
        esac
    done
}

read_web_oauth_client_id() {
    while true; do
        printf 'Discord OAuth Client ID: ' >&3
        if ! IFS= read -r -u 3 web_oauth_client_id; then
            fail "could not read Discord OAuth Client ID from the terminal"
        fi
        if is_valid_discord_guild_id "${web_oauth_client_id}"; then
            return 0
        fi
        printf '%s\n' \
            "Invalid OAuth Client ID: enter decimal digits from 1 to ${MAX_DISCORD_SNOWFLAKE}." \
            >&3
    done
}

read_web_oauth_client_secret() {
    while true; do
        printf 'Discord OAuth Client Secret: ' >&3
        if ! IFS= read -r -s -u 3 web_oauth_client_secret; then
            printf '\n' >&3
            fail "could not read Discord OAuth Client Secret from the terminal"
        fi
        printf '\n' >&3
        if is_valid_web_oauth_secret "${web_oauth_client_secret}"; then
            return 0
        fi
        printf '%s\n' \
            "Invalid OAuth Client Secret: enter a non-empty value containing only letters, digits, '.', '_' and '-'." \
            >&3
    done
}

read_web_oauth_redirect_uri() {
    while true; do
        printf 'Discord Redirect URI: ' >&3
        if ! IFS= read -r -u 3 web_oauth_redirect_uri; then
            fail "could not read Discord Redirect URI from the terminal"
        fi
        if is_valid_web_redirect_uri "${web_oauth_redirect_uri}"; then
            return 0
        fi
        printf '%s\n' \
            'Invalid Redirect URI: enter an absolute HTTPS URL ending exactly in /admin/auth/discord/callback without credentials, query or fragment.' \
            >&3
    done
}

read_web_owner_ids() {
    local entered_owner_ids

    while true; do
        printf 'Allowed OWNER Discord User IDs (comma-separated): ' >&3
        if ! IFS= read -r -u 3 entered_owner_ids; then
            fail "could not read allowed OWNER IDs from the terminal"
        fi
        if normalize_web_owner_ids "${entered_owner_ids}"; then
            return 0
        fi
        printf '%s\n' \
            "Invalid OWNER IDs: enter one or more comma-separated decimal IDs from 1 to ${MAX_DISCORD_SNOWFLAKE}, without empty items." \
            >&3
    done
}

read_web_admin_configuration() {
    printf '%s\n' '' 'Kanami Web Admin configuration' '' >&3
    read_web_oauth_client_id
    read_web_oauth_client_secret
    read_web_oauth_redirect_uri
    read_web_owner_ids
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
        'Kanami installation configuration' \
        'Core: enabled' \
        'Discord Bot Token: configured (hidden)' \
        "Discord Guild ID: ${discord_guild_id}" \
        "Report timezone: ${report_timezone}" >&3
    if [[ ${configure_web_admin} == "true" ]]; then
        printf '%s\n' \
            'Web Admin: enabled' \
            "OAuth Client ID: ${web_oauth_client_id}" \
            "Redirect URI: ${web_oauth_redirect_uri}" \
            "Allowed OWNER IDs: ${web_owner_ids}" \
            'OAuth Client Secret: configured (hidden)' \
            'Bind: 127.0.0.1:8000' \
            'Cookie Secure: true' >&3
    else
        printf '%s\n' 'Web Admin: disabled' >&3
    fi
    printf '\n' >&3
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
installer_main_bashpid="${BASHPID}"
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
read_web_admin_choice
if [[ ${configure_web_admin} == "true" ]]; then
    validate_web_installation_absent
    read_web_admin_configuration
fi
show_configuration_summary
if ! confirm_installation; then
    unset discord_token
    unset web_oauth_client_secret
    exec 3>&-
    log "Installation cancelled before creating Kanami users, databases, checkout, configuration, or units"
    return 0
fi
exec 3>&-
installation_confirmed="true"
trap report_partial_installation ERR

log "Starting local PostgreSQL"
systemctl enable --now postgresql

role_exists="$(runuser -u postgres -- psql -tAc \
    "SELECT 1 FROM pg_roles WHERE rolname = '${DB_ROLE}'")"
database_exists="$(runuser -u postgres -- psql -tAc \
    "SELECT 1 FROM pg_database WHERE datname = '${DB_NAME}'")"
web_role_exists=""
if [[ ${configure_web_admin} == "true" ]]; then
    web_role_exists="$(runuser -u postgres -- psql -tAc \
        "SELECT 1 FROM pg_roles WHERE rolname = '${WEB_DB_ROLE}'")"
fi
if [[ -n ${role_exists} || -n ${database_exists} ]]; then
    fail "PostgreSQL role/database already exists; configure it manually per docs/INSTALL.md"
fi
if [[ -n ${web_role_exists} ]]; then
    fail "PostgreSQL role ${WEB_DB_ROLE} already exists; inspect it manually"
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
if [[ ${configure_web_admin} == "true" ]]; then
    create_web_service_user
fi

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

if [[ ${configure_web_admin} == "true" ]]; then
    configure_web_git_metadata
fi

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

if [[ ${configure_web_admin} == "true" ]]; then
    sync_web_runtime
fi

log "Creating PostgreSQL role and database"
db_password="$(openssl rand -hex 32)"
web_db_password=""
if [[ ${configure_web_admin} == "true" ]]; then
    web_db_password="$(openssl rand -hex 32)"
fi
sql_file="$(mktemp)"
cleanup() {
    rm -f -- "${sql_file}"
}
trap cleanup EXIT
chmod 0600 "${sql_file}"
printf "CREATE ROLE %s LOGIN PASSWORD '%s';\n" \
    "${DB_ROLE}" "${db_password}" >"${sql_file}"
if [[ ${configure_web_admin} == "true" ]]; then
    printf "CREATE ROLE %s LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD '%s';\n" \
        "${WEB_DB_ROLE}" "${web_db_password}" >>"${sql_file}"
fi
chown postgres:postgres "${sql_file}"
runuser -u postgres -- psql --set=ON_ERROR_STOP=1 --file="${sql_file}" postgres \
    >/dev/null
runuser -u postgres -- createdb --owner="${DB_ROLE}" "${DB_NAME}"

database_url="postgresql+asyncpg://${DB_ROLE}:${db_password}@127.0.0.1:5432/${DB_NAME}"
web_database_url=""
if [[ ${configure_web_admin} == "true" ]]; then
    web_database_url="postgresql+asyncpg://${WEB_DB_ROLE}:${web_db_password}@127.0.0.1:5432/${DB_NAME}"
    unset web_db_password
fi

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

if [[ ${configure_web_admin} == "true" ]]; then
    log "Applying least-privilege Web Admin PostgreSQL grants"
    apply_web_database_grants
    write_web_configuration "${web_database_url}"
    unset web_database_url
fi

log "Installing Kanami Manager command"
install_manager

log "Installing systemd unit without starting the bot"
install_service_unit
if [[ ${configure_web_admin} == "true" ]]; then
    log "Installing Web Admin systemd unit without enabling or starting it"
    install_web_service_unit
fi
systemctl daemon-reload

log "Installation complete. Next steps:"
printf '%s\n' \
    "  1. kanami doctor" \
    "  2. Review ${CONFIG_FILE} with sudoedit if optional settings are needed" \
    "  3. sudo systemctl enable kanami" \
    "  4. sudo kanami start" \
    "  5. kanami logs"
if [[ ${configure_web_admin} == "true" ]]; then
    printf '%s\n' \
        '' \
        'Web Admin installed but not started.' \
        'Configure reverse proxy/TLS and verify the OAuth redirect before starting it.' \
        'Continue with docs/WEB_ADMIN_DEPLOYMENT.md; do not expose port 8000 directly.'
fi
trap - ERR
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
