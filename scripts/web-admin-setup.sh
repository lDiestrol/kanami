#!/usr/bin/env bash
set -Eeuo pipefail
set +x

readonly PROGRAM_NAME="kanami web-setup"
readonly INSTALL_DIR="/opt/kanami"
readonly SERVICE_USER="kanami"
readonly WEB_SERVICE_USER="kanami-web"
readonly WEB_SERVICE_HOME="/var/lib/kanami-web"
readonly WEB_VENV_DIR="${WEB_SERVICE_HOME}/.venv"
readonly WEB_UV_BOOTSTRAP_DIR="${WEB_SERVICE_HOME}/uv"
readonly WEB_UV_CACHE_DIR="${WEB_SERVICE_HOME}/.cache/uv"
readonly CONFIG_DIR="/etc/kanami"
readonly CORE_CONFIG_FILE="${CONFIG_DIR}/kanami.env"
readonly WEB_CONFIG_FILE="${CONFIG_DIR}/kanami-web-admin.env"
readonly CORE_SERVICE_FILE="/etc/systemd/system/kanami.service"
readonly WEB_SERVICE_FILE="/etc/systemd/system/kanami-web-admin.service"
readonly CADDY_CONFIG_DIR="/etc/caddy"
readonly CADDY_CONFIG_FILE="${CADDY_CONFIG_DIR}/Caddyfile"
readonly CADDY_TEMPLATE="${INSTALL_DIR}/deploy/caddy/Caddyfile.managed.template"
readonly CADDY_STATE_DIR="/var/lib/caddy"
readonly CADDY_VENDOR_UNIT="/usr/lib/systemd/system/caddy.service"
readonly CADDY_VENDOR_UNIT_LEGACY="/lib/systemd/system/caddy.service"
readonly CADDY_UNIT_MASK="/etc/systemd/system/caddy.service"
readonly CADDY_UNIT_DROPIN_DIR="/etc/systemd/system/caddy.service.d"
readonly CADDY_API_UNIT_OVERRIDE="/etc/systemd/system/caddy-api.service"
readonly CADDY_API_UNIT_DROPIN_DIR="/etc/systemd/system/caddy-api.service.d"
readonly CORE_SERVICE="kanami.service"
readonly WEB_SERVICE="kanami-web-admin.service"
readonly CADDY_SERVICE="caddy.service"
readonly BOT_CONTROL_URL="http://127.0.0.1:8765"
readonly WEB_HEALTH_URL="http://127.0.0.1:8000/admin/health"
readonly OAUTH_CALLBACK_PATH="/admin/auth/discord/callback"
readonly MAX_ENV_BYTES=1048576
readonly MAX_ENV_LINES=4096

readonly SYSTEMCTL="/usr/bin/systemctl"
readonly STAT="/usr/bin/stat"
readonly FIND="/usr/bin/find"
readonly GETENT="/usr/bin/getent"
readonly ID="/usr/bin/id"
readonly RUNUSER="/usr/sbin/runuser"
readonly PYTHON="/usr/bin/python3"
readonly OPENSSL="/usr/bin/openssl"
readonly INSTALL="/usr/bin/install"
readonly MKTEMP="/usr/bin/mktemp"
readonly MV="/usr/bin/mv"
readonly CHOWN="/usr/bin/chown"
readonly CHMOD="/usr/bin/chmod"
readonly LN="/usr/bin/ln"
readonly RM="/usr/bin/rm"
readonly DPKG_QUERY="/usr/bin/dpkg-query"
readonly APT_GET="/usr/bin/apt-get"
readonly CADDY="/usr/bin/caddy"
readonly CURL="/usr/bin/curl"
readonly TIMEOUT="/usr/bin/timeout"
readonly CMP="/usr/bin/cmp"
readonly ENV="/usr/bin/env"
readonly READLINK="/usr/bin/readlink"

declare -a TEMP_FILES=()
public_hostname=""
oauth_callback=""
bot_control_state=""
bot_control_secret=""
web_gid=""
core_gid=""
web_uid=""
caddy_uid=""
caddy_gid=""
caddy_installed="false"
caddy_mask_created="false"
caddy_install_attempted="false"
mutation_confirmed="false"
core_env_replaced="false"
web_env_replaced="false"
activation_complete="false"

log() {
    printf '[kanami] %s\n' "$*"
}

warn() {
    printf '[kanami] WARNING: %s\n' "$*" >&2
}

fail() {
    printf '[kanami] ERROR: %s\n' "$*" >&2
    exit 1
}

cleanup() {
    local status=$?
    local temp

    set +e
    for temp in "${TEMP_FILES[@]}"; do
        if [[ -n ${temp} && ( -e ${temp} || -L ${temp} ) ]]; then
            "${RM}" -f -- "${temp}"
        fi
    done
    if [[ ${caddy_mask_created} == "true" ]]; then
        if [[ ${caddy_install_attempted} == "true" && -x ${SYSTEMCTL} ]]; then
            "${SYSTEMCTL}" stop "${CADDY_SERVICE}" caddy-api.service >/dev/null 2>&1 || true
            "${SYSTEMCTL}" disable "${CADDY_SERVICE}" >/dev/null 2>&1 || true
            "${SYSTEMCTL}" disable caddy-api.service >/dev/null 2>&1 || true
        fi
        if [[ -L ${CADDY_UNIT_MASK} && \
            $("${READLINK}" -- "${CADDY_UNIT_MASK}" 2>/dev/null) == "/dev/null" ]]; then
            "${RM}" -f -- "${CADDY_UNIT_MASK}"
            "${SYSTEMCTL}" daemon-reload >/dev/null 2>&1 || true
        fi
    fi
    unset bot_control_secret
    if ((status != 0)) && [[ ${mutation_confirmed} == "true" ]]; then
        printf '%s\n' \
            '[kanami] ERROR: activation failed after confirmation and may be partial.' \
            '[kanami] Inspect both env files, kanami.service, kanami-web-admin.service, caddy.service and /etc/caddy/Caddyfile manually.' >&2
        if [[ ${core_env_replaced} != "${web_env_replaced}" ]]; then
            printf '%s\n' \
                '[kanami] ERROR: only one protected env file was replaced; Bot Control pairing requires immediate manual inspection.' >&2
        fi
    fi
    return "${status}"
}

trap cleanup EXIT

require_command() {
    local path="$1"
    local description="$2"

    [[ -x ${path} ]] || fail "required ${description} is unavailable at ${path}"
}

validate_no_arguments() {
    (($# == 0)) || fail "this command does not accept arguments"
}

validate_platform() {
    local os_id=""
    local version_id=""
    local line

    [[ -r /etc/os-release ]] || fail "cannot verify Debian release from /etc/os-release"
    while IFS= read -r line || [[ -n ${line} ]]; do
        case ${line} in
            ID=*) os_id="${line#ID=}" ;;
            VERSION_ID=*) version_id="${line#VERSION_ID=}" ;;
        esac
    done </etc/os-release
    os_id="${os_id%\"}"
    os_id="${os_id#\"}"
    version_id="${version_id%\"}"
    version_id="${version_id#\"}"
    [[ ${os_id} == "debian" && ${version_id} == "13" ]] || \
        fail "managed web-setup supports Debian 13 only; found ${os_id:-unknown} ${version_id:-unknown}"
}

validate_checkout_trust() {
    local unexpected_source
    local writable_source
    local unexpected_venv

    [[ -d ${INSTALL_DIR} && ! -L ${INSTALL_DIR} ]] || \
        fail "production checkout must be a regular directory at ${INSTALL_DIR}"
    [[ -d "${INSTALL_DIR}/.git" && ! -L "${INSTALL_DIR}/.git" ]] || \
        fail "production checkout must contain a regular .git directory"
    [[ -d "${INSTALL_DIR}/.venv" && ! -L "${INSTALL_DIR}/.venv" ]] || \
        fail "core project environment is missing or is not a regular directory"

    unexpected_source="$("${FIND}" -P "${INSTALL_DIR}" -xdev \
        -path "${INSTALL_DIR}/.venv" -prune -o \
        \( ! -uid 0 -o ! -gid 0 \) -print -quit)"
    [[ -z ${unexpected_source} ]] || \
        fail "production checkout source must be root-owned: ${unexpected_source}"
    writable_source="$("${FIND}" -P "${INSTALL_DIR}" -xdev \
        -path "${INSTALL_DIR}/.venv" -prune -o \
        ! -type l -perm /022 -print -quit)"
    [[ -z ${writable_source} ]] || \
        fail "production checkout source is group/other writable: ${writable_source}"
    unexpected_venv="$("${FIND}" -P "${INSTALL_DIR}/.venv" -xdev \
        \( ! -user "${SERVICE_USER}" -o ! -group "${SERVICE_USER}" \) \
        -print -quit)"
    [[ -z ${unexpected_venv} ]] || \
        fail "core project environment must be owned by ${SERVICE_USER}: ${unexpected_venv}"
    "${RUNUSER}" -u "${SERVICE_USER}" -- test -w "${INSTALL_DIR}/.venv" || \
        fail "core project environment is not writable by ${SERVICE_USER}"

    [[ -f ${CADDY_TEMPLATE} && -r ${CADDY_TEMPLATE} && ! -L ${CADDY_TEMPLATE} ]] || \
        fail "canonical managed Caddy template is missing, unreadable or a symlink"
    [[ $(/usr/bin/grep -Fc '__KANAMI_PUBLIC_HOST__' "${CADDY_TEMPLATE}") == "1" ]] || \
        fail "canonical managed Caddy template must contain exactly one hostname placeholder"
}

path_metadata() {
    "${STAT}" -c '%u:%g:%a' -- "$1"
}

validate_regular_file() {
    local path="$1"
    local expected="$2"
    local description="$3"
    local actual

    [[ -f ${path} && ! -L ${path} ]] || \
        fail "${description} must be a regular non-symlink file: ${path}"
    actual="$(path_metadata "${path}")" || fail "cannot inspect ${description} metadata"
    [[ ${actual} == "${expected}" ]] || \
        fail "${description} must have owner/group/mode ${expected}; found ${actual}"
}

validate_regular_directory() {
    local path="$1"
    local expected="$2"
    local description="$3"
    local actual

    [[ -d ${path} && ! -L ${path} ]] || \
        fail "${description} must be a regular non-symlink directory: ${path}"
    actual="$(path_metadata "${path}")" || fail "cannot inspect ${description} metadata"
    [[ ${actual} == "${expected}" ]] || \
        fail "${description} must have owner/group/mode ${expected}; found ${actual}"
}

validate_root_owned_non_writable_directory() {
    local path="$1"
    local description="$2"
    local actual
    local owner
    local group
    local mode
    local mode_value

    [[ -d ${path} && ! -L ${path} ]] || \
        fail "${description} must be a regular non-symlink directory: ${path}"
    actual="$(path_metadata "${path}")" || fail "cannot inspect ${description} metadata"
    IFS=: read -r owner group mode <<<"${actual}"
    [[ ${owner} == "0" && ${group} =~ ^[0-9]+$ && ${mode} =~ ^[0-7]{3,4}$ ]] || \
        fail "${description} must be root-owned with valid metadata; found ${actual}"
    mode_value=$((8#${mode}))
    (( (mode_value & 0022) == 0 )) || \
        fail "${description} must not be writable by group or others; found ${actual}"
}

validate_owned_non_writable_directory() {
    local path="$1"
    local expected_uid="$2"
    local expected_gid="$3"
    local description="$4"
    local actual
    local owner
    local group
    local mode
    local mode_value

    [[ -d ${path} && ! -L ${path} ]] || \
        fail "${description} must be a regular non-symlink directory: ${path}"
    actual="$(path_metadata "${path}")" || fail "cannot inspect ${description} metadata"
    IFS=: read -r owner group mode <<<"${actual}"
    [[ ${owner} == "${expected_uid}" && ${group} == "${expected_gid}" && \
        ${mode} =~ ^[0-7]{3,4}$ ]] || \
        fail "${description} must have owner/group ${expected_uid}:${expected_gid} and valid mode; found ${actual}"
    mode_value=$((8#${mode}))
    (( (mode_value & 0022) == 0 )) || \
        fail "${description} must not be writable by group or others; found ${actual}"
}

validate_unit_loaded() {
    local unit="$1"
    local state

    state="$("${SYSTEMCTL}" show "${unit}" --property=LoadState --value 2>/dev/null)" || \
        fail "cannot inspect ${unit} load state"
    [[ ${state} == "loaded" ]] || \
        fail "${unit} must be installed and loaded; found ${state:-unknown}"
}

validate_service_identity_and_groups() {
    local core_primary_group
    local core_supplementary_groups
    local web_primary_group
    local web_supplementary_groups

    "${ID}" "${SERVICE_USER}" >/dev/null 2>&1 || \
        fail "core service user ${SERVICE_USER} is missing"
    "${ID}" "${WEB_SERVICE_USER}" >/dev/null 2>&1 || \
        fail "complete D2.12 Web Admin installation is required; ${WEB_SERVICE_USER} is missing"
    core_gid="$("${ID}" -g "${SERVICE_USER}")" || fail "cannot resolve ${SERVICE_USER} group"
    web_gid="$("${ID}" -g "${WEB_SERVICE_USER}")" || fail "cannot resolve ${WEB_SERVICE_USER} group"
    web_uid="$("${ID}" -u "${WEB_SERVICE_USER}")" || fail "cannot resolve ${WEB_SERVICE_USER} UID"
    core_primary_group="$("${ID}" -gn "${SERVICE_USER}")" || \
        fail "cannot resolve ${SERVICE_USER} primary group"
    web_primary_group="$("${ID}" -gn "${WEB_SERVICE_USER}")" || \
        fail "cannot resolve ${WEB_SERVICE_USER} primary group"
    [[ ${core_primary_group} == "${SERVICE_USER}" ]] || \
        fail "${SERVICE_USER} primary group must be ${SERVICE_USER}; found ${core_primary_group}"
    [[ ${web_primary_group} == "${WEB_SERVICE_USER}" ]] || \
        fail "${WEB_SERVICE_USER} primary group must be ${WEB_SERVICE_USER}; found ${web_primary_group}"
    core_supplementary_groups="$("${ID}" -G "${SERVICE_USER}")" || \
        fail "cannot resolve ${SERVICE_USER} numeric group membership"
    web_supplementary_groups="$("${ID}" -G "${WEB_SERVICE_USER}")" || \
        fail "cannot resolve ${WEB_SERVICE_USER} numeric group membership"
    [[ " ${web_supplementary_groups} " != *" ${core_gid} "* ]] || \
        fail "${WEB_SERVICE_USER} must not be a member of the ${SERVICE_USER} group"
    [[ " ${core_supplementary_groups} " != *" ${web_gid} "* ]] || \
        fail "${SERVICE_USER} must not be a member of the ${WEB_SERVICE_USER} group"
}

validate_d212_installation() {
    local marker

    validate_service_identity_and_groups
    validate_regular_directory "${CONFIG_DIR}" "0:${core_gid}:750" "Kanami configuration directory"
    validate_regular_directory "${WEB_SERVICE_HOME}" "${web_uid}:${web_gid}:750" "Web Admin runtime home"
    for marker in "${WEB_VENV_DIR}" "${WEB_UV_BOOTSTRAP_DIR}" "${WEB_UV_CACHE_DIR}"; do
        validate_regular_directory "${marker}" "${web_uid}:${web_gid}:750" "Web Admin runtime directory"
        "${RUNUSER}" -u "${WEB_SERVICE_USER}" -- test -w "${marker}" || \
            fail "Web Admin runtime directory is not writable by ${WEB_SERVICE_USER}: ${marker}"
    done
    validate_regular_file "${CORE_CONFIG_FILE}" "0:${core_gid}:640" "core environment"
    validate_regular_file "${WEB_CONFIG_FILE}" "0:${web_gid}:640" "Web Admin environment"
    validate_regular_file "${CORE_SERVICE_FILE}" "0:0:644" "core systemd unit"
    validate_regular_file "${WEB_SERVICE_FILE}" "0:0:644" "Web Admin systemd unit"
    [[ -x "${WEB_VENV_DIR}/bin/kanami-web-admin" && \
        ! -L "${WEB_VENV_DIR}/bin/kanami-web-admin" ]] || \
        fail "Web Admin executable is missing or is a symlink"
    "${RUNUSER}" -u "${WEB_SERVICE_USER}" -- test -x \
        "${WEB_UV_BOOTSTRAP_DIR}/bin/uv" || \
        fail "Web Admin uv bootstrap is not executable by ${WEB_SERVICE_USER}"
    validate_unit_loaded "${CORE_SERVICE}"
    validate_unit_loaded "${WEB_SERVICE}"
}

validate_web_network_invariants() {
    local web_host="" web_port="" cookie_secure="" allow_private_bind=""
    local p_web_host="false" p_web_port="false" p_cookie_secure="false"
    local p_allow_private_bind="false"

    read_env_key "${WEB_CONFIG_FILE}" WEB_ADMIN_HOST web_host p_web_host
    read_env_key "${WEB_CONFIG_FILE}" WEB_ADMIN_PORT web_port p_web_port
    read_env_key "${WEB_CONFIG_FILE}" WEB_ADMIN_COOKIE_SECURE cookie_secure p_cookie_secure
    read_env_key "${WEB_CONFIG_FILE}" WEB_ADMIN_ALLOW_PRIVATE_BIND \
        allow_private_bind p_allow_private_bind
    [[ ${p_web_host} == "true" && ${p_web_port} == "true" && \
        ${p_cookie_secure} == "true" && ${web_host} == "127.0.0.1" && \
        ${web_port} == "8000" && ${cookie_secure} == "true" && \
        ( ${p_allow_private_bind} == "false" || ${allow_private_bind} == "false" ) ]] || \
        fail "managed same-host Caddy requires WEB_ADMIN_HOST=127.0.0.1, WEB_ADMIN_PORT=8000, WEB_ADMIN_COOKIE_SECURE=true and WEB_ADMIN_ALLOW_PRIVATE_BIND absent or false; correct D2.12 configuration drift manually"
}

read_env_key() {
    local file="$1"
    local key="$2"
    local value_var="$3"
    local present_var="$4"
    local size
    local count=0
    local lines=0
    local line
    local found_value=""

    size="$("${STAT}" -c '%s' -- "${file}")" || fail "cannot inspect protected env size"
    ((size <= MAX_ENV_BYTES)) || fail "protected env exceeds ${MAX_ENV_BYTES} bytes: ${file}"
    while IFS= read -r line || [[ -n ${line} ]]; do
        lines=$((lines + 1))
        ((lines <= MAX_ENV_LINES)) || fail "protected env exceeds ${MAX_ENV_LINES} lines: ${file}"
        line="${line%$'\r'}"
        if [[ ${line} == "${key}="* ]]; then
            count=$((count + 1))
            found_value="${line#*=}"
        fi
    done <"${file}"
    ((count <= 1)) || fail "duplicate critical key ${key} in ${file}"
    if ((count == 1)); then
        printf -v "${value_var}" '%s' "${found_value}"
        printf -v "${present_var}" '%s' "true"
    else
        printf -v "${value_var}" '%s' ""
        printf -v "${present_var}" '%s' "false"
    fi
}

validate_redirect_and_hostname() {
    local redirect_present="false"

    read_env_key "${WEB_CONFIG_FILE}" WEB_ADMIN_DISCORD_REDIRECT_URI \
        oauth_callback redirect_present
    [[ ${redirect_present} == "true" ]] || \
        fail "WEB_ADMIN_DISCORD_REDIRECT_URI is missing from the Web Admin environment"
    public_hostname="$("${PYTHON}" - "${oauth_callback}" "${OAUTH_CALLBACK_PATH}" <<'PY'
import ipaddress
import re
import sys
from urllib.parse import urlsplit

value, expected_path = sys.argv[1:]
try:
    parsed = urlsplit(value)
    port = parsed.port
except ValueError:
    raise SystemExit(1)
host = parsed.hostname
if (
    parsed.scheme != "https"
    or not host
    or parsed.username is not None
    or parsed.password is not None
    or parsed.path != expected_path
    or parsed.query
    or parsed.fragment
    or port not in (None, 443)
):
    raise SystemExit(1)
host = host.lower()
if host == "localhost" or host.endswith(".localhost") or "*" in host:
    raise SystemExit(1)
try:
    ipaddress.ip_address(host)
except ValueError:
    pass
else:
    raise SystemExit(1)
if len(host) > 253 or host.endswith(".") or "." not in host:
    raise SystemExit(1)
labels = host.split(".")
if any(
    not 1 <= len(label) <= 63
    or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
    for label in labels
):
    raise SystemExit(1)
if len(labels[-1]) < 2 or labels[-1].isdigit():
    raise SystemExit(1)
print(host)
PY
)" || fail "OAuth redirect must be exact public HTTPS ${OAUTH_CALLBACK_PATH} on a DNS hostname"
    [[ -n ${public_hostname} ]] || fail "could not derive public hostname from OAuth redirect"
}

inspect_bot_control_pairing() {
    local core_enabled="" core_host="" core_port="" core_secret=""
    local web_url="" web_secret=""
    local p_core_enabled="false" p_core_host="false" p_core_port="false"
    local p_core_secret="false" p_web_url="false" p_web_secret="false"
    local present_count=0
    local present

    read_env_key "${CORE_CONFIG_FILE}" DISCORD_BOT_CONTROL_ENABLED core_enabled p_core_enabled
    read_env_key "${CORE_CONFIG_FILE}" DISCORD_BOT_CONTROL_HOST core_host p_core_host
    read_env_key "${CORE_CONFIG_FILE}" DISCORD_BOT_CONTROL_PORT core_port p_core_port
    read_env_key "${CORE_CONFIG_FILE}" DISCORD_BOT_CONTROL_SHARED_SECRET core_secret p_core_secret
    read_env_key "${WEB_CONFIG_FILE}" WEB_ADMIN_BOT_CONTROL_URL web_url p_web_url
    read_env_key "${WEB_CONFIG_FILE}" WEB_ADMIN_BOT_CONTROL_SHARED_SECRET web_secret p_web_secret
    for present in "${p_core_enabled}" "${p_core_host}" "${p_core_port}" \
        "${p_core_secret}" "${p_web_url}" "${p_web_secret}"; do
        [[ ${present} == "true" ]] && present_count=$((present_count + 1))
    done
    if ((present_count == 0)); then
        bot_control_state="new pairing required"
        return 0
    fi
    ((present_count == 6)) || \
        fail "partial Bot Control configuration detected; inspect both env files manually"
    [[ ${core_enabled} == "true" && ${core_host} == "127.0.0.1" && \
        ${core_port} == "8765" && ${web_url} == "${BOT_CONTROL_URL}" ]] || \
        fail "contradictory Bot Control configuration detected; expected fixed loopback endpoints"
    [[ ${#core_secret} -ge 32 && ${core_secret} == "${web_secret}" && \
        ! ${core_secret} =~ [[:cntrl:]] && \
        ${core_secret} != [[:space:]]* && ${core_secret} != *[[:space:]] ]] || \
        fail "Bot Control shared secrets are missing, too short or different; inspect manually"
    bot_control_state="already paired"
    bot_control_secret="${core_secret}"
    unset core_secret web_secret
}

render_managed_caddy() {
    local line

    while IFS= read -r line || [[ -n ${line} ]]; do
        printf '%s\n' "${line//__KANAMI_PUBLIC_HOST__/${public_hostname}}"
    done <"${CADDY_TEMPLATE}"
}

existing_config_is_managed_exact() {
    [[ -f ${CADDY_CONFIG_FILE} && ! -L ${CADDY_CONFIG_FILE} ]] || return 1
    "${CMP}" -s -- <(render_managed_caddy) "${CADDY_CONFIG_FILE}"
}

unit_is_present() {
    local unit="$1"
    local state

    state="$("${SYSTEMCTL}" show "${unit}" --property=LoadState --value 2>/dev/null)" || return 1
    [[ ${state} != "not-found" && -n ${state} ]]
}

require_unit_absent() {
    local unit="$1"
    local state

    state="$("${SYSTEMCTL}" show "${unit}" --property=LoadState --value 2>/dev/null)" || \
        fail "cannot inspect ${unit} load state"
    [[ ${state} == "not-found" ]] || \
        fail "${unit} must be absent before managed Caddy installation; found ${state:-unknown}"
}

validate_caddy_identity() {
    local caddy_primary_group
    local caddy_groups

    "${ID}" caddy >/dev/null 2>&1 || fail "Debian Caddy service user is missing"
    caddy_uid="$("${ID}" -u caddy)" || fail "cannot resolve Debian Caddy UID"
    caddy_gid="$("${ID}" -g caddy)" || fail "cannot resolve Debian Caddy group"
    caddy_primary_group="$("${ID}" -gn caddy)" || \
        fail "cannot resolve Debian Caddy primary group"
    [[ ${caddy_primary_group} == "caddy" ]] || \
        fail "Debian Caddy primary group must be caddy; found ${caddy_primary_group}"
    caddy_groups="$("${ID}" -G caddy)" || \
        fail "cannot resolve Debian Caddy numeric group membership"
    [[ " ${caddy_groups} " != *" ${core_gid} "* ]] || \
        fail "caddy must not be a member of the ${SERVICE_USER} group"
    [[ " ${caddy_groups} " != *" ${web_gid} "* ]] || \
        fail "caddy must not be a member of the ${WEB_SERVICE_USER} group"
}

validate_caddy_effective_unit() {
    local fragment_path
    local normalized_fragment
    local normalized_vendor
    local drop_in_paths

    validate_unit_loaded "${CADDY_SERVICE}"
    fragment_path="$("${SYSTEMCTL}" show "${CADDY_SERVICE}" \
        --property=FragmentPath --value 2>/dev/null)" || \
        fail "cannot inspect ${CADDY_SERVICE} effective fragment"
    [[ ${fragment_path} == "${CADDY_VENDOR_UNIT}" || \
        ${fragment_path} == "${CADDY_VENDOR_UNIT_LEGACY}" ]] || \
        fail "${CADDY_SERVICE} must use the canonical Debian vendor unit; found ${fragment_path:-unknown}"
    normalized_fragment="$("${READLINK}" -f -- "${fragment_path}")" || \
        fail "cannot normalize ${CADDY_SERVICE} effective fragment"
    normalized_vendor="$("${READLINK}" -f -- "${CADDY_VENDOR_UNIT}")" || \
        fail "cannot normalize canonical Debian Caddy vendor unit"
    [[ ${normalized_fragment} == "${normalized_vendor}" ]] || \
        fail "${CADDY_SERVICE} fragment does not resolve to the canonical Debian vendor unit"
    drop_in_paths="$("${SYSTEMCTL}" show "${CADDY_SERVICE}" \
        --property=DropInPaths --value 2>/dev/null)" || \
        fail "cannot inspect ${CADDY_SERVICE} effective drop-ins"
    [[ -z ${drop_in_paths} ]] || \
        fail "${CADDY_SERVICE} has unknown effective drop-ins; inspect manually"
}

validate_caddy_api_boot_state() {
    local active_state
    local enabled_state

    active_state="$("${SYSTEMCTL}" show caddy-api.service \
        --property=ActiveState --value 2>/dev/null)" || \
        fail "cannot inspect caddy-api.service active state"
    [[ ${active_state} == "inactive" ]] || \
        fail "caddy-api.service must be inactive for the managed Caddy topology; found ${active_state:-unknown}"
    enabled_state="$("${SYSTEMCTL}" is-enabled caddy-api.service 2>/dev/null)" || true
    [[ ${enabled_state} == "disabled" ]] || \
        fail "caddy-api.service must be disabled for the managed Caddy topology; found ${enabled_state:-unknown}"
}

reject_local_caddy_overrides() {
    [[ ! -e ${CADDY_UNIT_MASK} && ! -L ${CADDY_UNIT_MASK} ]] || \
        fail "${CADDY_UNIT_MASK} already exists; refusing to alter an existing Caddy unit or mask"
    [[ ! -e ${CADDY_UNIT_DROPIN_DIR} && ! -L ${CADDY_UNIT_DROPIN_DIR} ]] || \
        fail "foreign Caddy service drop-ins exist at ${CADDY_UNIT_DROPIN_DIR}; inspect them manually"
    [[ ! -e ${CADDY_API_UNIT_OVERRIDE} && ! -L ${CADDY_API_UNIT_OVERRIDE} ]] || \
        fail "local caddy-api.service override exists; inspect the Caddy deployment manually"
    [[ ! -e ${CADDY_API_UNIT_DROPIN_DIR} && ! -L ${CADDY_API_UNIT_DROPIN_DIR} ]] || \
        fail "local caddy-api.service drop-ins exist; inspect the Caddy deployment manually"
}

validate_installed_caddy_preflight() {
    validate_root_owned_non_writable_directory "${CADDY_CONFIG_DIR}" "Caddy configuration directory"
    validate_regular_file "${CADDY_CONFIG_FILE}" "0:0:644" "Kanami-managed Caddyfile"
    existing_config_is_managed_exact || \
        fail "existing Caddyfile is not the exact Kanami-managed config for ${public_hostname}; use the documented existing-proxy path"
    validate_caddy_identity
    validate_owned_non_writable_directory "${CADDY_STATE_DIR}" \
        "${caddy_uid}" "${caddy_gid}" "Caddy state directory"
    reject_local_caddy_overrides
    validate_caddy_effective_unit
    validate_caddy_api_boot_state
}

validate_core_activation() {
    local active_state
    local enabled_state

    validate_unit_loaded "${CORE_SERVICE}"
    active_state="$("${SYSTEMCTL}" show "${CORE_SERVICE}" --property=ActiveState --value 2>/dev/null)" || \
        fail "cannot inspect ${CORE_SERVICE} active state"
    [[ ${active_state} == "active" ]] || \
        fail "${CORE_SERVICE} must be active before Web activation; finish Core activation through the installer/Manager steps, then retry sudo kanami web-setup"
    enabled_state="$("${SYSTEMCTL}" is-enabled "${CORE_SERVICE}" 2>/dev/null)" || \
        fail "${CORE_SERVICE} must be enabled before Web activation; finish Core activation through the installer/Manager steps, then retry sudo kanami web-setup"
    [[ ${enabled_state} == "enabled" ]] || \
        fail "${CORE_SERVICE} must be enabled before Web activation; finish Core activation through the installer/Manager steps, then retry sudo kanami web-setup"
}

preflight_caddy() {
    local proxy_unit
    local package_status=""

    for proxy_unit in nginx.service traefik.service apache2.service; do
        if unit_is_present "${proxy_unit}"; then
            fail "existing proxy unit ${proxy_unit} detected; use the manual existing-proxy path in docs/WEB_ADMIN_DEPLOYMENT.md"
        fi
    done
    package_status="$("${DPKG_QUERY}" -W -f='${db:Status-Status}' caddy 2>/dev/null)" || true
    if [[ ${package_status} == "installed" ]]; then
        caddy_installed="true"
        [[ -x ${CADDY} ]] || fail "caddy package is installed but ${CADDY} is unavailable"
        validate_installed_caddy_preflight
    else
        caddy_installed="false"
        [[ ! -e ${CADDY} && ! -L ${CADDY} ]] || \
            fail "${CADDY} exists outside an installed Debian caddy package; inspect it manually"
        [[ ! -e ${CADDY_CONFIG_DIR} && ! -L ${CADDY_CONFIG_DIR} ]] || \
            fail "Caddy is not installed but ${CADDY_CONFIG_DIR} already exists; inspect the proxy deployment manually"
        require_unit_absent "${CADDY_SERVICE}"
        require_unit_absent caddy-api.service
        reject_local_caddy_overrides
    fi
}

dns_preflight() {
    local dns_result

    dns_result="$("${TIMEOUT}" 10 "${GETENT}" ahosts "${public_hostname}" \
        2>/dev/null | /usr/bin/head -n 1)" || true
    [[ -n ${dns_result} ]] || \
        fail "public hostname ${public_hostname} does not resolve; configure DNS before activation"
}

show_summary() {
    printf '%s\n' \
        '' \
        'Kanami Web Admin production activation' \
        '' \
        "  Public hostname: ${public_hostname}" \
        "  OAuth callback: ${oauth_callback}" \
        '  Web backend: 127.0.0.1:8000' \
        '  Bot Control: 127.0.0.1:8765 (loopback only)' \
        "  Bot Control state: ${bot_control_state}" \
        "  Caddy package: $([[ ${caddy_installed} == true ]] && printf 'installed, exact Kanami config' || printf 'install from configured APT sources')" \
        '' \
        'This will modify both protected env files when pairing is new, install/configure Caddy if needed,' \
        'restart kanami.service, start or restart Web Admin, start or reload Caddy, and enable Web Admin/Caddy after local smoke tests.' \
        'Public DNS and inbound TCP/80+443 must reach this Caddy host; Kanami will not change the firewall.' \
        ''
}

confirm_activation() {
    local answer

    exec 3<>/dev/tty || fail "final confirmation requires a usable terminal (/dev/tty)"
    [[ -t 3 ]] || fail "final confirmation requires a usable terminal (/dev/tty)"
    printf 'Proceed with production activation? [y/N]: ' >&3
    if ! IFS= read -r -u 3 answer; then
        printf '\n' >&3
        log "Activation cancelled; no production state was changed."
        return 1
    fi
    answer="${answer%$'\r'}"
    case ${answer} in
        y | Y | yes | YES) return 0 ;;
        *)
            log "Activation cancelled; no production state was changed."
            return 1
            ;;
    esac
}

register_temp() {
    TEMP_FILES+=("$1")
}

stage_paired_env_files() {
    local core_temp
    local web_temp
    local line

    [[ ${bot_control_state} == "new pairing required" ]] || return 0
    bot_control_secret="$("${OPENSSL}" rand -hex 32)" || fail "could not generate Bot Control shared secret"
    [[ ${#bot_control_secret} -ge 32 ]] || fail "generated Bot Control shared secret is unexpectedly short"
    umask 077
    core_temp="$("${MKTEMP}" "${CONFIG_DIR}/.kanami.env.web-setup.XXXXXX")"
    register_temp "${core_temp}"
    web_temp="$("${MKTEMP}" "${CONFIG_DIR}/.kanami-web-admin.env.web-setup.XXXXXX")"
    register_temp "${web_temp}"

    while IFS= read -r line || [[ -n ${line} ]]; do
        case ${line} in
            DISCORD_BOT_CONTROL_ENABLED=* | DISCORD_BOT_CONTROL_HOST=* | \
                DISCORD_BOT_CONTROL_PORT=* | DISCORD_BOT_CONTROL_SHARED_SECRET=*) ;;
            *) printf '%s\n' "${line%$'\r'}" ;;
        esac
    done <"${CORE_CONFIG_FILE}" >"${core_temp}"
    printf '%s\n' \
        'DISCORD_BOT_CONTROL_ENABLED=true' \
        'DISCORD_BOT_CONTROL_HOST=127.0.0.1' \
        'DISCORD_BOT_CONTROL_PORT=8765' >>"${core_temp}"
    printf 'DISCORD_BOT_CONTROL_SHARED_SECRET=%s\n' "${bot_control_secret}" >>"${core_temp}"

    while IFS= read -r line || [[ -n ${line} ]]; do
        case ${line} in
            WEB_ADMIN_BOT_CONTROL_URL=* | WEB_ADMIN_BOT_CONTROL_SHARED_SECRET=*) ;;
            *) printf '%s\n' "${line%$'\r'}" ;;
        esac
    done <"${WEB_CONFIG_FILE}" >"${web_temp}"
    printf 'WEB_ADMIN_BOT_CONTROL_URL=%s\n' "${BOT_CONTROL_URL}" >>"${web_temp}"
    printf 'WEB_ADMIN_BOT_CONTROL_SHARED_SECRET=%s\n' "${bot_control_secret}" >>"${web_temp}"

    "${CHOWN}" "0:${core_gid}" "${core_temp}"
    "${CHOWN}" "0:${web_gid}" "${web_temp}"
    [[ $(path_metadata "${core_temp}") == "0:${core_gid}:600" ]] || \
        fail "staged core environment metadata validation failed"
    [[ $(path_metadata "${web_temp}") == "0:${web_gid}:600" ]] || \
        fail "staged Web Admin environment metadata validation failed"

    "${MV}" -f -- "${core_temp}" "${CORE_CONFIG_FILE}"
    core_env_replaced="true"
    "${CHMOD}" 0640 "${CORE_CONFIG_FILE}"
    validate_regular_file "${CORE_CONFIG_FILE}" "0:${core_gid}:640" "core environment"
    "${MV}" -f -- "${web_temp}" "${WEB_CONFIG_FILE}"
    web_env_replaced="true"
    "${CHMOD}" 0640 "${WEB_CONFIG_FILE}"
    validate_regular_file "${WEB_CONFIG_FILE}" "0:${web_gid}:640" "Web Admin environment"
    log "Bot Control pairing written to both protected env files."
}

create_temporary_caddy_mask() {
    "${LN}" -s /dev/null "${CADDY_UNIT_MASK}"
    caddy_mask_created="true"
    "${SYSTEMCTL}" daemon-reload
}

remove_temporary_caddy_mask() {
    [[ ${caddy_mask_created} == "true" ]] || return 0
    [[ -L ${CADDY_UNIT_MASK} && \
        $("${READLINK}" -- "${CADDY_UNIT_MASK}") == "/dev/null" ]] || \
        fail "temporary Caddy mask changed unexpectedly; inspect ${CADDY_UNIT_MASK}"
    "${RM}" -f -- "${CADDY_UNIT_MASK}"
    caddy_mask_created="false"
    "${SYSTEMCTL}" daemon-reload
}

install_caddy_package_safely() {
    [[ ${caddy_installed} == "false" ]] || return 0
    log "Temporarily masking first Caddy start."
    create_temporary_caddy_mask
    caddy_install_attempted="true"
    log "Installing Caddy from configured APT sources."
    (
        umask 022
        "${APT_GET}" install --no-install-recommends -y caddy
    )
    [[ $("${DPKG_QUERY}" -W -f='${db:Status-Status}' caddy 2>/dev/null) == "installed" ]] || \
        fail "apt completed without an installed Debian caddy package state"
    [[ -x ${CADDY} ]] || fail "Debian Caddy installation did not provide ${CADDY}"
    validate_root_owned_non_writable_directory "${CADDY_CONFIG_DIR}" "Caddy configuration directory"
    validate_caddy_identity
    validate_owned_non_writable_directory "${CADDY_STATE_DIR}" \
        "${caddy_uid}" "${caddy_gid}" "Caddy state directory"
    "${SYSTEMCTL}" disable "${CADDY_SERVICE}" >/dev/null
    "${SYSTEMCTL}" disable caddy-api.service >/dev/null
    validate_caddy_api_boot_state
    if "${SYSTEMCTL}" is-active --quiet "${CADDY_SERVICE}"; then
        fail "Caddy became active while the temporary first-start mask was installed"
    fi
    caddy_installed="true"
}

install_and_validate_caddy_config() {
    local config_temp
    local formatted_diff
    validate_root_owned_non_writable_directory "${CADDY_CONFIG_DIR}" "Caddy configuration directory"
    umask 077
    config_temp="$("${MKTEMP}" "${CADDY_CONFIG_DIR}/.Caddyfile.kanami.XXXXXX")"
    register_temp "${config_temp}"
    render_managed_caddy >"${config_temp}"
    formatted_diff="$("${CADDY}" fmt --diff "${config_temp}")" || \
        fail "caddy fmt failed for the managed config"
    [[ -z ${formatted_diff} ]] || \
        fail "tracked managed Caddy template is not canonically formatted"
    validate_caddy_identity
    "${CHOWN}" "0:${caddy_gid}" "${config_temp}"
    "${CHMOD}" 0640 "${config_temp}"
    "${RUNUSER}" -u caddy -- "${ENV}" HOME=/var/lib/caddy \
        "${CADDY}" validate --config "${config_temp}" --adapter caddyfile \
        >/dev/null || \
        fail "caddy validate rejected the managed config"
    "${INSTALL}" -m 0644 -o root -g root "${config_temp}" "${CADDY_CONFIG_FILE}"
    validate_regular_file "${CADDY_CONFIG_FILE}" "0:0:644" "Kanami-managed Caddyfile"
    existing_config_is_managed_exact || fail "installed Caddyfile differs from the canonical managed config"
    remove_temporary_caddy_mask
    validate_caddy_effective_unit
}

restart_core_and_require_active() {
    log "Restarting ${CORE_SERVICE} so loopback Bot Control becomes active."
    "${SYSTEMCTL}" restart "${CORE_SERVICE}"
    "${SYSTEMCTL}" is-active --quiet "${CORE_SERVICE}" || \
        fail "${CORE_SERVICE} is not active after restart"
}

smoke_bot_control() {
    log "Checking authenticated Bot Control on 127.0.0.1:8765."
    "${PYTHON}" - "${CORE_CONFIG_FILE}" <<'PY'
import http.client
import time
import sys

env_path = sys.argv[1]
keys = {
    "DISCORD_BOT_CONTROL_ENABLED",
    "DISCORD_BOT_CONTROL_HOST",
    "DISCORD_BOT_CONTROL_PORT",
    "DISCORD_BOT_CONTROL_SHARED_SECRET",
}
values = {}
counts = {key: 0 for key in keys}
with open(env_path, encoding="utf-8") as stream:
    for number, raw_line in enumerate(stream, 1):
        if number > 4096:
            raise SystemExit(1)
        line = raw_line.rstrip("\r\n")
        key, separator, value = line.partition("=")
        if separator and key in keys:
            counts[key] += 1
            values[key] = value
if any(counts[key] != 1 for key in keys):
    raise SystemExit(1)
if (
    values["DISCORD_BOT_CONTROL_ENABLED"] != "true"
    or values["DISCORD_BOT_CONTROL_HOST"] != "127.0.0.1"
    or values["DISCORD_BOT_CONTROL_PORT"] != "8765"
    or len(values["DISCORD_BOT_CONTROL_SHARED_SECRET"]) < 32
):
    raise SystemExit(1)
deadline = time.monotonic() + 30
while True:
    connection = None
    try:
        connection = http.client.HTTPConnection("127.0.0.1", 8765, timeout=3)
        connection.request(
            "GET",
            "/control/v1/server-settings/options",
            headers={"Authorization": "Bearer " + values["DISCORD_BOT_CONTROL_SHARED_SECRET"]},
        )
        response = connection.getresponse()
        if response.status == 200:
            response.close()
            raise SystemExit(0)
        response.close()
    except (OSError, http.client.HTTPException):
        pass
    finally:
        if connection is not None:
            connection.close()
    if time.monotonic() >= deadline:
        raise SystemExit(1)
    time.sleep(1)
PY
}

start_web_and_smoke() {
    log "Starting ${WEB_SERVICE}."
    if "${SYSTEMCTL}" is-active --quiet "${WEB_SERVICE}"; then
        "${SYSTEMCTL}" restart "${WEB_SERVICE}"
    else
        "${SYSTEMCTL}" start "${WEB_SERVICE}"
    fi
    "${PYTHON}" - <<'PY'
import json
import http.client
import time

deadline = time.monotonic() + 30
while True:
    connection = None
    try:
        connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=3)
        connection.request("GET", "/admin/health")
        response = connection.getresponse()
        body = response.read(1025)
        if (
            response.status == 200
            and len(body) <= 1024
            and json.loads(body) == {"status": "healthy"}
        ):
            response.close()
            raise SystemExit(0)
        response.close()
    except (OSError, ValueError, json.JSONDecodeError, http.client.HTTPException):
        pass
    finally:
        if connection is not None:
            connection.close()
    if time.monotonic() >= deadline:
        raise SystemExit(1)
    time.sleep(1)
PY
    "${SYSTEMCTL}" is-active --quiet "${WEB_SERVICE}" || \
        fail "${WEB_SERVICE} is not active after local health smoke"
    log "Web Admin loopback health is healthy."
}

activate_caddy() {
    if "${SYSTEMCTL}" is-active --quiet "${CADDY_SERVICE}"; then
        log "Reloading ${CADDY_SERVICE} with the validated managed config."
        "${SYSTEMCTL}" reload "${CADDY_SERVICE}"
    else
        log "Starting ${CADDY_SERVICE} with the validated managed config."
        "${SYSTEMCTL}" start "${CADDY_SERVICE}"
    fi
    "${SYSTEMCTL}" is-active --quiet "${CADDY_SERVICE}" || \
        fail "${CADDY_SERVICE} is not active after activation"
}

public_https_smoke() {
    local public_url="https://${public_hostname}/admin/health"
    local http_status=""

    log "Running best-effort public HTTPS health smoke."
    if http_status="$("${CURL}" --silent --show-error --max-time 15 \
        --noproxy '*' --output /dev/null --write-out '%{http_code}' "${public_url}")" && \
        [[ ${http_status} == "200" ]]; then
        log "Public HTTPS health endpoint responded with HTTP 200."
    else
        warn "public HTTPS smoke failed from this host. NAT hairpin or DNS routing may prevent local verification; inspect Caddy/TLS logs and test externally."
    fi
}

enable_services_after_smoke() {
    log "Enabling Web Admin and Caddy after mandatory local smoke tests."
    "${SYSTEMCTL}" enable "${WEB_SERVICE}" "${CADDY_SERVICE}" >/dev/null
}

show_completion() {
    printf '%s\n' \
        '' \
        'Kanami Web Admin production activation completed.' \
        '' \
        'Human browser checklist:' \
        "  1. Open https://${public_hostname}/ and verify the 302 redirect to /admin/." \
        '  2. Complete Discord OAuth.' \
        '  3. Confirm that a break-glass OWNER is recognized.' \
        '  4. Check read-only pages.' \
        '  5. Perform one controlled write action.' \
        '  6. Check Rules publication/control if it is part of this deployment.' \
        '  7. Only after browser smoke, decide whether to enable HSTS manually.' \
        '' \
        "Before 'kanami update', stop kanami-web-admin.service; the updater intentionally refuses an active Web Admin." \
        'No firewall rules or Discord Developer Portal settings were changed.'
}

main() {
    validate_no_arguments "$@"
    ((EUID == 0)) || fail "root is required; run: sudo kanami web-setup"
    require_command "${SYSTEMCTL}" systemctl
    require_command "${STAT}" stat
    require_command "${FIND}" find
    require_command "${GETENT}" getent
    require_command "${ID}" id
    require_command "${RUNUSER}" runuser
    require_command "${PYTHON}" python3
    require_command "${OPENSSL}" openssl
    require_command "${INSTALL}" install
    require_command "${MKTEMP}" mktemp
    require_command "${MV}" mv
    require_command "${CHOWN}" chown
    require_command "${CHMOD}" chmod
    require_command "${LN}" ln
    require_command "${RM}" rm
    require_command "${DPKG_QUERY}" dpkg-query
    require_command "${APT_GET}" apt-get
    require_command "${CURL}" curl
    require_command "${TIMEOUT}" timeout
    require_command "${CMP}" cmp
    require_command "${ENV}" env
    require_command "${READLINK}" readlink

    log "Running read-only D2.13 pre-flight."
    validate_platform
    validate_checkout_trust
    validate_d212_installation
    validate_web_network_invariants
    validate_core_activation
    validate_redirect_and_hostname
    inspect_bot_control_pairing
    dns_preflight
    preflight_caddy
    show_summary
    if ! confirm_activation; then
        return 0
    fi
    mutation_confirmed="true"

    stage_paired_env_files
    install_caddy_package_safely
    install_and_validate_caddy_config
    "${SYSTEMCTL}" daemon-reload
    restart_core_and_require_active
    smoke_bot_control || fail "authenticated Bot Control readiness check did not succeed before its deadline"
    start_web_and_smoke || fail "Web Admin local health did not become healthy before its deadline"
    activate_caddy
    public_https_smoke
    enable_services_after_smoke
    activation_complete="true"
    show_completion
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
