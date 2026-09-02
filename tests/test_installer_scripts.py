import os
import shutil
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPOSITORY_ROOT / "scripts/install.sh"
WEB_GRANTS = REPOSITORY_ROOT / "deploy/postgresql/kanami-web-admin-grants.sql"
type BashRunner = Callable[[str], subprocess.CompletedProcess[str]]


def installer_source() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def test_installer_is_tracked_as_executable() -> None:
    result = subprocess.run(
        ["git", "ls-files", "--stage", "--", "scripts/install.sh"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.split(maxsplit=1)[0] == "100755"


def manager_install_function(source: str) -> str:
    start = source.index("install_manager() {")
    end = source.index("\n}\n", start) + 3
    return source[start:end]


def installer_function(source: str, name: str) -> str:
    start = source.index(f"{name}() {{")
    end = source.index("\n}\n", start) + 3
    return source[start:end]


@pytest.fixture
def run_bash(tmp_path: Path) -> BashRunner:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is required for isolated installer helper tests")

    shim_directory = tmp_path / "bin"
    shim_directory.mkdir()
    python3_shim = shim_directory / "python3"
    python3_shim.write_text(
        '#!/usr/bin/env bash\nexec "$KANAMI_TEST_PYTHON" "$@"\n',
        encoding="utf-8",
    )
    python3_shim.chmod(0o755)

    environment = os.environ.copy()
    environment["KANAMI_TEST_PYTHON"] = sys.executable
    environment["PATH"] = os.pathsep.join(
        (str(shim_directory), environment.get("PATH", ""))
    )

    def invoke(script: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [bash, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    return invoke


def test_installer_copies_manager_from_installed_checkout_with_safe_metadata() -> None:
    source = installer_source()
    function = manager_install_function(source)

    assert 'readonly MANAGER_FILE="/usr/local/bin/kanami"' in source
    assert 'local manager_source="${INSTALL_DIR}/scripts/manager.sh"' in function
    assert (
        'install -m 0755 -o root -g root "${manager_source}" "${MANAGER_FILE}"'
        in function
    )
    assert source.index(
        'git clone --local --no-hardlinks "${SOURCE_DIR}"'
    ) < source.index('log "Installing Kanami Manager command"')


def test_manager_install_step_is_copy_based_and_idempotent() -> None:
    function = manager_install_function(installer_source())

    assert "install -m 0755" in function
    assert "ln -s" not in function
    assert "ln -sf" not in function
    assert "readlink" not in function


def test_manager_install_step_does_not_read_or_write_secrets() -> None:
    function = manager_install_function(installer_source())

    for secret_marker in (
        "CONFIG_FILE",
        "kanami.env",
        "DISCORD_TOKEN",
        "DATABASE_URL",
        "db_password",
    ):
        assert secret_marker not in function


def test_installer_keeps_production_checkout_root_owned() -> None:
    source = installer_source()
    clone = 'git clone --local --no-hardlinks "${SOURCE_DIR}" "${INSTALL_DIR}"'

    assert clone in source
    assert source.index("umask 022") < source.index(clone)
    assert 'chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"' not in source
    assert "chown -R kanami:kanami /opt/kanami" not in source
    assert (
        'git -c safe.directory="${INSTALL_DIR}" -C "${INSTALL_DIR}" \\\n'
        '    remote set-url origin "${remote_url}"'
    ) in source
    assert source.index(clone) < source.index(
        'install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_USER}" \\\n'
        '    "${INSTALL_DIR}/.venv"'
    )


def test_installer_prepares_only_venv_as_service_user_writable() -> None:
    source = installer_source()
    venv_setup = (
        'install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_USER}" \\\n'
        '    "${INSTALL_DIR}/.venv"'
    )
    uv_sync = '"${UV_BOOTSTRAP_DIR}/bin/uv" sync --frozen --no-dev'

    assert venv_setup in source
    assert source.index(venv_setup) < source.index(uv_sync)
    assert (
        'runuser -u "${SERVICE_USER}" -- env HOME="${SERVICE_HOME}" \\\n'
        '        UV_CACHE_DIR="${UV_CACHE_DIR}"'
    ) in source
    assert ".venv/" in (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8")


def test_installer_uses_regular_root_owned_privileged_sources() -> None:
    source = installer_source()
    manager_function = manager_install_function(source)
    service_function = installer_function(source, "install_service_unit")

    for function in (manager_function, service_function):
        assert "-f ${" in function
        assert "-r ${" in function
        assert "! -L ${" in function
        assert "-o root -g root" in function
    assert 'local service_source="${INSTALL_DIR}/deploy/kanami.service"' in (
        service_function
    )


def test_installer_requires_tty_and_reads_token_hidden() -> None:
    source = installer_source()
    tty_function = installer_function(source, "require_configuration_tty")
    token_function = installer_function(source, "read_hidden_token")

    assert "exec 3<>/dev/tty" in tty_function
    assert "[[ -t 3 ]]" in tty_function
    assert "read -r -s -u 3 discord_token" in token_function
    assert "printf '\\n' >&3" in token_function
    assert "--discord-token" not in source
    assert "DISCORD_TOKEN:-" not in source
    assert "set -x" not in source
    for child_process_marker in ("python", "env ", "openssl", "runuser"):
        assert child_process_marker not in token_function


def test_xtrace_boundary_is_scoped_to_main_before_secret_handling() -> None:
    source = installer_source()
    main_start = source.index("main() {")
    top_level_before_main = source[:main_start]
    main_body = source[main_start:]

    assert "set +x" not in top_level_before_main
    assert main_body.startswith("main() {\n    set +x\n")
    assert main_body.index("set +x") < main_body.index("require_configuration_tty")
    assert main_body.index("set +x") < main_body.index("db_password=")


@pytest.mark.parametrize(
    ("token", "expected_valid"),
    [
        ("opaque.Token_value-123", True),
        ("", False),
        ("token with spaces", False),
        ("token\twith-tab", False),
        ("token\nwith-newline", False),
        ("token=value", False),
    ],
)
def test_token_validator_is_dotenv_safe(
    token: str, expected_valid: bool, run_bash: BashRunner
) -> None:
    function = installer_function(installer_source(), "is_valid_discord_token")
    result = run_bash(f"{function}\nis_valid_discord_token {shlex_quote(token)}")

    assert (result.returncode == 0) is expected_valid


def test_token_validation_error_matches_allowed_charset_without_secret() -> None:
    function = installer_function(installer_source(), "read_hidden_token")

    assert (
        "enter a non-empty token containing only letters, digits, '.', '_' and '-'."
        in function
    )
    assert "without whitespace or control characters" not in function
    assert "never-print-this-token" not in function


def test_installer_has_no_required_secret_placeholder() -> None:
    source = installer_source()

    assert "DISCORD_TOKEN=replace_me" not in source
    assert "Replace DISCORD_TOKEN and DISCORD_GUILD_ID placeholders" not in source
    assert "printf 'DISCORD_TOKEN=%s\\n' \"${discord_token}\"" in source


def test_sourcing_installer_does_not_run_main() -> None:
    source = installer_source()

    assert 'if [[ ${BASH_SOURCE[0]} == "$0" ]]; then' in source
    assert '    main "$@"' in source


@pytest.mark.parametrize(
    ("guild_id", "expected_valid"),
    [
        ("1", True),
        ("123456789012345678", True),
        ("18446744073709551615", True),
        ("0", False),
        ("18446744073709551616", False),
        ("abc", False),
        ("-1", False),
        ("123 456", False),
        ("1.0", False),
        ("1e5", False),
    ],
)
def test_guild_id_validator_boundaries(
    guild_id: str, expected_valid: bool, run_bash: BashRunner
) -> None:
    source = installer_source()
    function = installer_function(source, "is_valid_discord_guild_id")
    script = textwrap.dedent(
        f"""
        readonly MAX_DISCORD_SNOWFLAKE="18446744073709551615"
        {function}
        is_valid_discord_guild_id {shlex_quote(guild_id)}
        """
    )

    result = run_bash(script)

    assert (result.returncode == 0) is expected_valid


def shlex_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


@pytest.mark.parametrize("timezone", ["UTC", "Europe/Stockholm", "Asia/Yekaterinburg"])
def test_timezone_validator_accepts_iana_zones(
    timezone: str, run_bash: BashRunner
) -> None:
    function = installer_function(installer_source(), "is_valid_report_timezone")
    result = run_bash(f"{function}\nis_valid_report_timezone {shlex_quote(timezone)}")

    assert result.returncode == 0


@pytest.mark.parametrize("timezone", ["UTC+5", "Not/AZone", "UTC "])
def test_timezone_validator_rejects_invalid_zones(
    timezone: str, run_bash: BashRunner
) -> None:
    function = installer_function(installer_source(), "is_valid_report_timezone")
    result = run_bash(f"{function}\nis_valid_report_timezone {shlex_quote(timezone)}")

    assert result.returncode != 0


def test_timezone_prompt_defaults_to_utc(run_bash: BashRunner) -> None:
    source = installer_source()
    functions = installer_function(source, "read_report_timezone")
    result = run_bash(
        f'{functions}\nexec 3<<<""\nread_report_timezone\nprintf "%s" "$report_timezone"'
    )

    assert result.returncode == 0
    assert result.stdout == "UTC"


@pytest.mark.parametrize("answer", ["", "y", "Y", "yes", "YES"])
def test_confirmation_accepts_positive_answers(
    answer: str, run_bash: BashRunner
) -> None:
    function = installer_function(installer_source(), "confirm_installation")
    result = run_bash(
        f"{function}\nexec 3<<<{shlex_quote(answer)}\nconfirm_installation"
    )

    assert result.returncode == 0


@pytest.mark.parametrize("answer", ["n", "N", "no", "NO"])
def test_confirmation_accepts_negative_answers(
    answer: str, run_bash: BashRunner
) -> None:
    function = installer_function(installer_source(), "confirm_installation")
    result = run_bash(
        f"{function}\nexec 3<<<{shlex_quote(answer)}\nconfirm_installation"
    )

    assert result.returncode == 1


def test_safe_summary_never_contains_actual_token(run_bash: BashRunner) -> None:
    source = installer_source()
    function = installer_function(source, "show_configuration_summary")
    result = run_bash(
        f"""
        {function}
        discord_token='never-print-this-token'
        discord_guild_id='123456789012345678'
        report_timezone='Europe/Stockholm'
        exec 3>&1
        show_configuration_summary
        """
    )

    assert result.returncode == 0
    assert "Discord Bot Token: configured (hidden)" in result.stdout
    assert "Discord Guild ID: 123456789012345678" in result.stdout
    assert "Report timezone: Europe/Stockholm" in result.stdout
    assert "never-print-this-token" not in result.stdout


def test_cancellation_precedes_all_kanami_stateful_stages() -> None:
    source = installer_source()
    confirmation = source.index("if ! confirm_installation; then")
    cancellation = source.index("Installation cancelled before creating Kanami")

    assert confirmation < cancellation
    for stage in (
        'log "Starting local PostgreSQL"',
        'log "Installing committed checkout into ${INSTALL_DIR}"',
        'log "Creating PostgreSQL role and database"',
        'log "Writing protected core configuration"',
    ):
        assert cancellation < source.index(stage)


def test_fail_before_confirmation_has_no_partial_warning(run_bash: BashRunner) -> None:
    result = run_bash(
        f"source {shlex_quote(INSTALLER.as_posix())}\n"
        'installer_main_bashpid="${BASHPID}"\n'
        'fail "pre-confirmation failure"'
    )

    assert result.returncode != 0
    assert "pre-confirmation failure" in result.stderr
    assert "may be partially completed" not in result.stderr


def test_cancellation_has_no_partial_warning(run_bash: BashRunner) -> None:
    result = run_bash(
        f"source {shlex_quote(INSTALLER.as_posix())}\n"
        'installer_main_bashpid="${BASHPID}"\n'
        'exec 3<<<"n"\n'
        "if ! confirm_installation; then\n"
        '  log "Installation cancelled before creating Kanami production state"\n'
        "fi"
    )

    assert result.returncode == 0
    assert "Installation cancelled" in result.stdout
    assert "may be partially completed" not in result.stderr


def test_explicit_fail_after_confirmation_warns_once(run_bash: BashRunner) -> None:
    result = run_bash(
        f"source {shlex_quote(INSTALLER.as_posix())}\n"
        'installer_main_bashpid="${BASHPID}"\n'
        'installation_confirmed="true"\n'
        "trap report_partial_installation ERR\n"
        'fail "post-confirmation failure"'
    )

    assert result.returncode != 0
    assert "post-confirmation failure" in result.stderr
    assert result.stderr.count("may be partially completed") == 1


def test_unexpected_failure_after_confirmation_warns_once(
    run_bash: BashRunner,
) -> None:
    result = run_bash(
        f"source {shlex_quote(INSTALLER.as_posix())}\n"
        'installer_main_bashpid="${BASHPID}"\n'
        'installation_confirmed="true"\n'
        "trap report_partial_installation ERR\n"
        "false"
    )

    assert result.returncode != 0
    assert result.stderr.count("may be partially completed") == 1


def test_protected_config_uses_collected_values_and_keeps_defaults() -> None:
    source = installer_source()

    assert 'install -d -m 0750 -o root -g "${SERVICE_USER}" "${CONFIG_DIR}"' in source
    assert (
        'install -m 0640 -o root -g "${SERVICE_USER}" /dev/null "${CONFIG_FILE}"'
        in source
    )
    assert "printf 'DISCORD_GUILD_ID=%s\\n' \"${discord_guild_id}\"" in source
    assert "printf 'REPORT_TIMEZONE=%s\\n' \"${report_timezone}\"" in source
    assert "printf 'DATABASE_URL=%s\\n' \"${database_url}\"" in source
    for setting in (
        "VOICE_MIN_SESSION_SECONDS=10",
        "VOICE_CHECKPOINT_INTERVAL_SECONDS=60",
        "MEMBER_RETURN_MIN_ABSENCE_SECONDS=86400",
        "AUDIT_TRANSIENT_RETENTION_DAYS=90",
        "RAW_MESSAGE_RETENTION_DAYS=90",
        "SERVER_EVENT_RETENTION_DAYS=365",
        "LOG_LEVEL=INFO",
        "# DISCORD_AUDIT_LOG_CHANNEL_ID=123456789012345678",
        "# DISCORD_AUTOROLE_ID=123456789012345678",
    ):
        assert setting in source


def test_installer_generates_database_password_and_does_not_start_bot() -> None:
    source = installer_source()

    assert 'db_password="$(openssl rand -hex 32)"' in source
    assert "DISCORD_TOKEN=%s" not in installer_function(
        source, "show_configuration_summary"
    )
    assert "systemctl enable --now kanami" not in source
    assert "systemctl start kanami" not in source
    assert "kanami start" not in source[: source.index('log "Installation complete')]
    assert "systemctl daemon-reload" in source


def test_installer_keeps_dependency_and_migration_ordering() -> None:
    source = installer_source()

    assert "python3 python3-pip python3-venv tzdata" in source
    assert source.index('"${UV_BOOTSTRAP_DIR}/bin/uv" sync --frozen --no-dev') < (
        source.index('log "Creating PostgreSQL role and database"')
    )
    assert source.index('log "Writing protected core configuration"') < source.index(
        'log "Applying Alembic migrations"'
    )
    assert source.index('log "Applying Alembic migrations"') < source.index(
        'log "Installing systemd unit without starting the bot"'
    )


def test_web_admin_choice_defaults_to_disabled(run_bash: BashRunner) -> None:
    function = installer_function(installer_source(), "read_web_admin_choice")
    result = run_bash(
        f'{function}\nconfigure_web_admin=true\nexec 3<<<""\n'
        'read_web_admin_choice\nprintf "%s" "$configure_web_admin"'
    )

    assert result.returncode == 0
    assert result.stdout == "false"


@pytest.mark.parametrize(
    ("client_id", "expected_valid"),
    [
        ("1", True),
        ("123456789012345678", True),
        ("18446744073709551615", True),
        ("0", False),
        ("18446744073709551616", False),
        ("client", False),
        ("-1", False),
    ],
)
def test_web_oauth_client_id_uses_snowflake_boundaries(
    client_id: str, expected_valid: bool, run_bash: BashRunner
) -> None:
    function = installer_function(installer_source(), "is_valid_discord_guild_id")
    result = run_bash(
        'readonly MAX_DISCORD_SNOWFLAKE="18446744073709551615"\n'
        f"{function}\nis_valid_discord_guild_id {shlex_quote(client_id)}"
    )

    assert (result.returncode == 0) is expected_valid


@pytest.mark.parametrize(
    ("raw_ids", "expected"),
    [
        ("1", "1"),
        ("1,2,18446744073709551615", "1,2,18446744073709551615"),
        (" 42 , 7 ", "42,7"),
        ("42,7,42,007", "42,7"),
    ],
)
def test_web_owner_ids_are_canonicalized(
    raw_ids: str, expected: str, run_bash: BashRunner
) -> None:
    source = installer_source()
    functions = "\n".join(
        (
            installer_function(source, "is_valid_discord_guild_id"),
            installer_function(source, "normalize_web_owner_ids"),
        )
    )
    result = run_bash(
        'readonly MAX_DISCORD_SNOWFLAKE="18446744073709551615"\n'
        f'{functions}\nweb_owner_ids=""\nnormalize_web_owner_ids '
        f'{shlex_quote(raw_ids)}\nprintf "%s" "$web_owner_ids"'
    )

    assert result.returncode == 0
    assert result.stdout == expected


@pytest.mark.parametrize(
    "raw_ids",
    ["", "0", "18446744073709551616", "1,,2", ",1", "1,", "1,alpha"],
)
def test_web_owner_ids_reject_invalid_lists(raw_ids: str, run_bash: BashRunner) -> None:
    source = installer_source()
    functions = "\n".join(
        (
            installer_function(source, "is_valid_discord_guild_id"),
            installer_function(source, "normalize_web_owner_ids"),
        )
    )
    result = run_bash(
        'readonly MAX_DISCORD_SNOWFLAKE="18446744073709551615"\n'
        f"{functions}\nnormalize_web_owner_ids {shlex_quote(raw_ids)}"
    )

    assert result.returncode != 0


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "https://kanami.example.com/admin/auth/discord/callback",
        "https://kanami.example.com:8443/admin/auth/discord/callback",
    ],
)
def test_web_redirect_uri_accepts_production_https(
    redirect_uri: str, run_bash: BashRunner
) -> None:
    function = installer_function(installer_source(), "is_valid_web_redirect_uri")
    result = run_bash(
        f"{function}\nis_valid_web_redirect_uri {shlex_quote(redirect_uri)}"
    )

    assert result.returncode == 0


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "http://kanami.example.com/admin/auth/discord/callback",
        "/admin/auth/discord/callback",
        "https://user:pass@kanami.example.com/admin/auth/discord/callback",
        "https://kanami.example.com/admin/auth/discord/callback?next=1",
        "https://kanami.example.com/admin/auth/discord/callback#fragment",
        "https://kanami.example.com/admin/callback",
        "https:///admin/auth/discord/callback",
        "https://kanami.example.com:99999/admin/auth/discord/callback",
    ],
)
def test_web_redirect_uri_rejects_unsafe_shapes(
    redirect_uri: str, run_bash: BashRunner
) -> None:
    function = installer_function(installer_source(), "is_valid_web_redirect_uri")
    result = run_bash(
        f"{function}\nis_valid_web_redirect_uri {shlex_quote(redirect_uri)}"
    )

    assert result.returncode != 0


def test_web_oauth_secret_is_hidden_and_never_in_summary() -> None:
    source = installer_source()
    reader = installer_function(source, "read_web_oauth_client_secret")
    summary = installer_function(source, "show_configuration_summary")

    assert "read -r -s -u 3 web_oauth_client_secret" in reader
    assert "OAuth Client Secret: configured (hidden)" in summary
    assert "web_oauth_client_secret" not in summary
    for child_process_marker in ("python", "env ", "openssl", "runuser"):
        assert child_process_marker not in reader


def test_web_summary_contains_only_safe_configuration(run_bash: BashRunner) -> None:
    function = installer_function(installer_source(), "show_configuration_summary")
    result = run_bash(
        f"""
        {function}
        configure_web_admin=true
        discord_token='hidden-bot-token'
        discord_guild_id='123456789012345678'
        report_timezone='UTC'
        web_oauth_client_id='987654321'
        web_oauth_client_secret='hidden-oauth-secret'
        web_oauth_redirect_uri='https://kanami.example.com/admin/auth/discord/callback'
        web_owner_ids='42,7'
        web_database_url='postgresql+asyncpg://hidden-password@localhost/db'
        exec 3>&1
        show_configuration_summary
        """
    )

    assert result.returncode == 0
    assert "Web Admin: enabled" in result.stdout
    assert "OAuth Client ID: 987654321" in result.stdout
    assert "Allowed OWNER IDs: 42,7" in result.stdout
    assert "Bind: 127.0.0.1:8000" in result.stdout
    assert "Cookie Secure: true" in result.stdout
    for secret in ("hidden-bot-token", "hidden-oauth-secret", "hidden-password"):
        assert secret not in result.stdout


def test_generated_web_configuration_is_isolated_and_protected() -> None:
    source = installer_source()
    function = installer_function(source, "write_web_configuration")

    assert 'readonly WEB_CONFIG_FILE="${CONFIG_DIR}/kanami-web-admin.env"' in source
    assert 'install -m 0640 -o root -g "${WEB_SERVICE_USER}" /dev/null' in function
    for required in (
        "DISCORD_GUILD_ID=%s",
        "REPORT_TIMEZONE=%s",
        "VOICE_MIN_SESSION_SECONDS=10",
        "VOICE_CHECKPOINT_INTERVAL_SECONDS=60",
        "GAME_TRACKING_ENABLED=false",
        "GAME_CONFIRM_INTERVAL_SECONDS=60",
        "WEB_ADMIN_HOST=127.0.0.1",
        "WEB_ADMIN_PORT=8000",
        "WEB_ADMIN_COOKIE_SECURE=true",
        "WEB_ADMIN_SESSION_LIFETIME_SECONDS=28800",
    ):
        assert required in function
    for forbidden in (
        "DISCORD_TOKEN",
        "WEB_ADMIN_BOT_CONTROL_URL",
        "WEB_ADMIN_BOT_CONTROL_SHARED_SECRET",
        "DISCORD_BOT_CONTROL_",
    ):
        assert forbidden not in function


def test_web_runtime_is_separate_and_checkout_trust_is_preserved() -> None:
    source = installer_source()
    create_user = installer_function(source, "create_web_service_user")
    sync_runtime = installer_function(source, "sync_web_runtime")
    git_metadata = installer_function(source, "configure_web_git_metadata")

    assert 'readonly WEB_SERVICE_USER="kanami-web"' in source
    assert 'readonly WEB_VENV_DIR="${WEB_SERVICE_HOME}/.venv"' in source
    assert 'readonly WEB_UV_BOOTSTRAP_DIR="${WEB_SERVICE_HOME}/uv"' in source
    assert 'useradd --system --user-group --home-dir "${WEB_SERVICE_HOME}"' in (
        create_user
    )
    assert '--shell /usr/sbin/nologin "${WEB_SERVICE_USER}"' in create_user
    assert 'VIRTUAL_ENV="${WEB_VENV_DIR}"' in sync_runtime
    assert '"${WEB_UV_BOOTSTRAP_DIR}/bin/uv" sync' in sync_runtime
    assert "--active --frozen --no-dev" in sync_runtime
    assert 'runuser -u "${WEB_SERVICE_USER}"' in sync_runtime
    assert 'safe.directory "${INSTALL_DIR}"' in git_metadata
    assert "safe.directory=*" not in git_metadata
    assert 'usermod -a -G "${SERVICE_USER}"' not in source
    assert 'chown -R "${WEB_SERVICE_USER}" "${INSTALL_DIR}"' not in source


def test_web_database_grants_are_explicit_and_least_privilege() -> None:
    source = installer_source()
    apply_grants = installer_function(source, "apply_web_database_grants")
    grants = WEB_GRANTS.read_text(encoding="utf-8")

    assert "REVOKE CONNECT, TEMPORARY ON DATABASE discord_stats_prod FROM PUBLIC" in (
        grants
    )
    assert "GRANT CONNECT ON DATABASE discord_stats_prod" in grants
    assert "REVOKE CREATE ON SCHEMA public FROM PUBLIC" in grants
    assert "GRANT USAGE ON SCHEMA public" in grants
    assert "GRANT TEMPORARY" not in grants
    assert "GRANT INSERT, UPDATE, DELETE ON TABLE rulesets" in grants
    assert "GRANT INSERT ON TABLE audit_events" in grants
    assert "rulesets_id_seq, audit_events_id_seq" in grants
    assert "operational_health_observations" in grants
    assert "GRANT INSERT ON TABLE operational_health_observations" not in grants
    assert "GRANT UPDATE ON TABLE operational_health_observations" not in grants
    assert "GRANT DELETE ON TABLE operational_health_observations" not in grants
    for forbidden in (
        "GRANT ALL",
        "ALTER DEFAULT PRIVILEGES",
        "ALTER DATABASE",
        "ALTER SCHEMA",
        "OWNER TO",
        "GRANT CREATE ON SCHEMA",
        "TRUNCATE",
        "UPDATE ON TABLE guilds",
    ):
        assert forbidden not in grants
    assert 'local grants_source="${INSTALL_DIR}/${WEB_GRANTS_SOURCE_RELATIVE}"' in (
        apply_grants
    )
    assert "-f ${grants_source}" in apply_grants
    assert "-r ${grants_source}" in apply_grants
    assert "! -L ${grants_source}" in apply_grants
    assert '--file="${grants_source}" "${DB_NAME}"' in apply_grants
    assert source.index('log "Applying Alembic migrations"') < source.index(
        'log "Applying least-privilege Web Admin PostgreSQL grants"'
    )
    role_creation = source[source.index('printf "CREATE ROLE %s LOGIN NOSUPERUSER') :]
    for restriction in (
        "NOSUPERUSER",
        "NOCREATEDB",
        "NOCREATEROLE",
        "NOINHERIT",
        "NOREPLICATION",
        "NOBYPASSRLS",
    ):
        assert restriction in role_creation


def test_web_admin_is_optional_and_never_auto_started() -> None:
    source = installer_source()

    choice = source.index("read_web_admin_choice")
    confirmation = source.index("if ! confirm_installation; then")
    create_user = source.index("create_web_service_user", confirmation)
    create_database = source.index('log "Creating PostgreSQL role and database"')
    assert choice < confirmation < create_user
    assert confirmation < create_database
    assert 'if [[ ${configure_web_admin} == "true" ]]; then' in source
    for forbidden in (
        "systemctl enable kanami-web-admin",
        "systemctl start kanami-web-admin",
        "systemctl enable --now kanami-web-admin",
        "kanami web-start",
        "web-admin-setup.sh",
    ):
        assert forbidden not in source
    assert "Then run: sudo kanami web-setup" in source
