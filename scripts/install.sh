#!/usr/bin/env bash
set -Eeuo pipefail

readonly INSTALL_DIR="/opt/kanami"
readonly UV_BOOTSTRAP_DIR="/opt/kanami-uv"
readonly UV_CACHE_DIR="/var/cache/kanami/uv"
readonly SERVICE_HOME="/var/lib/kanami"
readonly CONFIG_DIR="/etc/kanami"
readonly CONFIG_FILE="${CONFIG_DIR}/kanami.env"
readonly SERVICE_FILE="/etc/systemd/system/kanami.service"
readonly SERVICE_USER="kanami"
readonly DB_NAME="discord_stats_prod"
readonly DB_ROLE="kanami_app"
readonly UV_VERSION="0.12.3"

log() {
    printf '[kanami] %s\n' "$*"
}

fail() {
    printf '[kanami] ERROR: %s\n' "$*" >&2
    exit 1
}

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

log "Installing Debian packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates git openssl postgresql postgresql-client \
    python3 python3-pip python3-venv

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
git clone --local --no-hardlinks "${SOURCE_DIR}" "${INSTALL_DIR}"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
[[ -d "${INSTALL_DIR}/.git" ]] || fail "installed checkout is missing .git"
runuser -u "${SERVICE_USER}" -- env HOME="${SERVICE_HOME}" \
    git -C "${INSTALL_DIR}" remote set-url origin "${remote_url}"

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

log "Writing protected configuration template"
install -d -m 0750 -o root -g "${SERVICE_USER}" "${CONFIG_DIR}"
install -m 0640 -o root -g "${SERVICE_USER}" /dev/null "${CONFIG_FILE}"
{
    printf '%s\n' \
        '# Discord credentials: replace both placeholders before starting Kanami.' \
        'DISCORD_TOKEN=replace_me' \
        'DISCORD_GUILD_ID=123456789012345678' \
        '' \
        '# Optional Discord features; uncomment and set an ID to enable.' \
        '# DISCORD_AUDIT_LOG_CHANNEL_ID=123456789012345678' \
        '# DISCORD_AUTOROLE_ID=123456789012345678' \
        '# DISCORD_ANNIVERSARY_CHANNEL_ID=123456789012345678' \
        '# DISCORD_RETURN_CHANNEL_ID=123456789012345678' \
        ''
    printf 'DATABASE_URL=%s\n' "${database_url}"
    printf '%s\n' \
        'REPORT_TIMEZONE=UTC' \
        'VOICE_MIN_SESSION_SECONDS=10' \
        'VOICE_CHECKPOINT_INTERVAL_SECONDS=60' \
        'MEMBER_RETURN_MIN_ABSENCE_SECONDS=86400' \
        'AUDIT_TRANSIENT_RETENTION_DAYS=90' \
        'RAW_MESSAGE_RETENTION_DAYS=90' \
        'SERVER_EVENT_RETENTION_DAYS=365' \
        'LOG_LEVEL=INFO'
} >"${CONFIG_FILE}"
unset db_password

log "Applying Alembic migrations"
(
    cd "${INSTALL_DIR}"
    runuser -u "${SERVICE_USER}" -- env HOME="${SERVICE_HOME}" \
        DATABASE_URL="${database_url}" \
        "${INSTALL_DIR}/.venv/bin/alembic" -c alembic.ini upgrade head
)
unset database_url

log "Installing systemd unit without starting the bot"
install -m 0644 "${INSTALL_DIR}/deploy/kanami.service" "${SERVICE_FILE}"
systemctl daemon-reload

log "Installation complete. Next steps:"
printf '%s\n' \
    "  1. sudoedit ${CONFIG_FILE}" \
    "  2. Replace DISCORD_TOKEN and DISCORD_GUILD_ID placeholders" \
    "  3. sudo systemctl enable --now kanami" \
    "  4. systemctl status kanami --no-pager" \
    "  5. journalctl -u kanami -n 100 --no-pager"
