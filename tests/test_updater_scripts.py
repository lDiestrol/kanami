import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
UPDATER = REPOSITORY_ROOT / "scripts/update.sh"
type BashRunner = Callable[[str], subprocess.CompletedProcess[str]]


def updater_source() -> str:
    return UPDATER.read_text(encoding="utf-8")


def manager_refresh_function(source: str) -> str:
    start = source.index("refresh_manager() {")
    end = source.index("\n}\n", start) + 3
    return source[start:end]


def updater_function(source: str, name: str) -> str:
    start = source.index(f"{name}() {{")
    end = source.index("\n}\n", start) + 3
    return source[start:end]


@pytest.fixture
def run_bash() -> BashRunner:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is required for isolated updater helper tests")

    def invoke(script: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [bash, "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )

    return invoke


def test_updater_refreshes_manager_from_installed_checkout_with_safe_metadata() -> None:
    source = updater_source()
    function = manager_refresh_function(source)

    assert 'readonly MANAGER_FILE="/usr/local/bin/kanami"' in source
    assert 'local manager_source="${INSTALL_DIR}/scripts/manager.sh"' in function
    assert (
        'install -m 0755 -o root -g root "${manager_source}" "${MANAGER_FILE}"'
        in function
    )


def test_manager_refresh_is_copy_based_and_rejects_invalid_source() -> None:
    function = manager_refresh_function(updater_source())

    assert "-f ${manager_source}" in function
    assert "-r ${manager_source}" in function
    assert "! -L ${manager_source}" in function
    failure = 'fail "installed checkout manager source is not a regular readable file"'
    assert failure in function
    assert "ln -s" not in function
    assert "ln -sf" not in function
    assert "readlink" not in function
    assert "eval" not in function


def test_manager_refresh_runs_after_pull_and_before_remaining_update_steps() -> None:
    source = updater_source()
    pull = 'git -c safe.directory="${INSTALL_DIR}" -C "${INSTALL_DIR}" pull --ff-only'
    refresh = 'log "Refreshing Kanami Manager command"'
    dependency_sync = 'log "Synchronizing locked dependencies"'
    migrations = 'log "Applying Alembic migrations"'
    restart = 'log "Updating systemd unit and restarting Kanami"'

    assert source.index(pull) < source.index(refresh)
    assert source.index(refresh) < source.index(dependency_sync)
    assert source.index(refresh) < source.index(migrations)
    assert source.index(refresh) < source.index(restart)


def test_manager_refresh_does_not_access_config_or_secrets() -> None:
    function = manager_refresh_function(updater_source())

    for secret_marker in (
        "CONFIG_FILE",
        "kanami.env",
        "DISCORD_TOKEN",
        "DATABASE_URL",
        "database_url",
    ):
        assert secret_marker not in function


def test_updater_runs_production_git_operations_as_root() -> None:
    source = updater_source()
    root_git = 'git -c safe.directory="${INSTALL_DIR}" -C "${INSTALL_DIR}"'

    assert f"{root_git} \\\n    status --porcelain" in source
    assert f"{root_git} \\\n    rev-parse --short HEAD" in source
    assert f"{root_git} pull --ff-only" in source
    assert source.index("umask 022") < source.index(f"{root_git} pull --ff-only")
    assert (
        'runuser -u "${SERVICE_USER}" -- env HOME="${SERVICE_HOME}" \\\n    git'
        not in source
    )
    assert 'fail "Git working tree has local changes; update aborted"' in source


def test_updater_validates_root_owned_checkout_with_venv_exception() -> None:
    function = updater_function(updater_source(), "validate_checkout_ownership")

    assert "! -L ${INSTALL_DIR}" in function
    assert '-path "${INSTALL_DIR}/.venv" -prune' in function
    assert "! -uid 0 -o ! -gid 0" in function
    assert "! -type l -perm /022" in function
    assert 'find -P "${INSTALL_DIR}/.venv"' in function
    assert '! -user "${SERVICE_USER}" -o ! -group "${SERVICE_USER}"' in function
    assert 'test -w "${INSTALL_DIR}/.venv"' in function
    assert "production checkout source must be root-owned" in function
    assert "project environment must be owned by" in function


def test_updater_does_not_repair_legacy_checkout_ownership() -> None:
    source = updater_source()

    assert "chown -R" not in source
    assert 'chown root:root "${INSTALL_DIR}"' not in source
    assert "validate_checkout_ownership" in source


def test_updater_uses_regular_root_owned_privileged_sources() -> None:
    source = updater_source()
    manager_function = manager_refresh_function(source)
    service_function = updater_function(source, "install_service_unit")

    for function in (manager_function, service_function):
        assert "-f ${" in function
        assert "-r ${" in function
        assert "! -L ${" in function
        assert "-o root -g root" in function
    assert 'local manager_source="${INSTALL_DIR}/scripts/manager.sh"' in (
        manager_function
    )
    assert 'local service_source="${INSTALL_DIR}/deploy/kanami.service"' in (
        service_function
    )


def test_updater_preserves_conservative_git_policy() -> None:
    source = updater_source()

    assert "pull --ff-only" in source
    for forbidden in (
        "reset --hard",
        "git clean",
        "rebase",
        "checkout --force",
        "checkout -f",
    ):
        assert forbidden not in source


def test_updater_preserves_dependency_migration_restart_order() -> None:
    source = updater_source()
    pull = source.index("pull --ff-only")
    refresh = source.index('log "Refreshing Kanami Manager command"')
    sync = source.index('log "Synchronizing locked dependencies"')
    migrations = source.index('log "Applying Alembic migrations"')
    unit_restart = source.index('log "Updating systemd unit and restarting Kanami"')

    assert pull < refresh < sync < migrations < unit_restart
    assert '"${UV_BOOTSTRAP_DIR}/bin/uv" sync --frozen --no-dev' in source
    assert '"${INSTALL_DIR}/.venv/bin/discord-stats-bot"' in source


def test_core_only_update_skips_optional_web_runtime() -> None:
    function = updater_function(updater_source(), "preflight_optional_web_installation")

    assert "if ((marker_count == 0)); then" in function
    assert "Optional Web Admin installation not present; skipping" in function
    assert "return 0" in function


def test_installed_web_runtime_refreshes_locked_dependencies_as_web_user() -> None:
    source = updater_source()
    function = updater_function(source, "refresh_optional_web_runtime")

    assert 'readonly WEB_SERVICE_USER="kanami-web"' in source
    assert 'readonly WEB_VENV_DIR="${WEB_SERVICE_HOME}/.venv"' in source
    assert 'readonly WEB_UV_BOOTSTRAP_DIR="${WEB_SERVICE_HOME}/uv"' in source
    assert 'runuser -u "${WEB_SERVICE_USER}" -- env HOME="${WEB_SERVICE_HOME}"' in (
        function
    )
    assert 'VIRTUAL_ENV="${WEB_VENV_DIR}" UV_CACHE_DIR="${WEB_UV_CACHE_DIR}"' in (
        function
    )
    assert '"${WEB_UV_BOOTSTRAP_DIR}/bin/uv" sync' in function
    assert "--active --frozen --no-dev" in function
    assert '"${WEB_VENV_DIR}/bin/kanami-web-admin"' in function
    assert "sudo" not in function


def test_partial_web_runtime_fails_closed_without_ownership_repair() -> None:
    function = updater_function(updater_source(), "preflight_optional_web_installation")

    assert "partial Web Admin installation detected" in function
    for marker in (
        "WEB_SERVICE_HOME",
        "WEB_VENV_DIR",
        "WEB_UV_BOOTSTRAP_DIR",
        "WEB_UV_CACHE_DIR",
        "WEB_CONFIG_FILE",
        "WEB_SERVICE_FILE",
    ):
        assert marker in function
    assert "chown" not in function
    assert "chmod" not in function


def test_complete_web_runtime_requires_exact_metadata_and_web_user_access() -> None:
    source = updater_source()
    function = updater_function(source, "preflight_optional_web_installation")

    assert 'web_uid="$(id -u' in function
    assert 'web_gid="$(id -g' in function
    assert 'web_primary_group="$(id -gn' in function
    assert 'web_primary_group} == "${WEB_SERVICE_USER}"' in function
    assert '"${web_uid}:${web_gid}:750"' in function
    assert '"0:${web_gid}:640"' in function
    assert '"0:0:644"' in function
    assert 'runuser -u "${WEB_SERVICE_USER}" -- test -w' in function
    assert 'runuser -u "${WEB_SERVICE_USER}" -- test -x' in function
    assert "chown" not in function
    assert "chmod" not in function


def test_active_web_admin_is_rejected_before_git_pull(run_bash: BashRunner) -> None:
    source = updater_source()
    fail_function = updater_function(source, "fail")
    state_function = updater_function(source, "validate_web_systemd_state")
    result = run_bash(
        f"{fail_function}\n{state_function}\n"
        'validate_web_systemd_state "loaded" "active"'
    )

    assert result.returncode != 0
    assert "Web Admin is active" in result.stderr
    assert "does not manage Web Admin lifecycle automatically" in result.stderr
    preflight_call = source.index("\npreflight_optional_web_installation\n")
    assert preflight_call < source.index("pull --ff-only")


@pytest.mark.parametrize(
    ("load_state", "active_state"),
    [("not-found", "inactive"), ("loaded", "activating"), ("loaded", "unknown")],
)
def test_ambiguous_web_admin_systemd_state_fails_closed(
    load_state: str,
    active_state: str,
    run_bash: BashRunner,
) -> None:
    source = updater_source()
    result = run_bash(
        f"{updater_function(source, 'fail')}\n"
        f"{updater_function(source, 'validate_web_systemd_state')}\n"
        f'validate_web_systemd_state "{load_state}" "{active_state}"'
    )

    assert result.returncode != 0
    assert "update aborted" in result.stderr


def test_inactive_web_admin_allows_preflight_state(run_bash: BashRunner) -> None:
    source = updater_source()
    result = run_bash(
        f"{updater_function(source, 'fail')}\n"
        f"{updater_function(source, 'validate_web_systemd_state')}\n"
        'validate_web_systemd_state "loaded" "inactive"'
    )

    assert result.returncode == 0


def test_web_refresh_occurs_after_core_sync_without_web_restart() -> None:
    source = updater_source()
    core_sync = source.index('log "Synchronizing locked dependencies"')
    web_refresh = source.index("refresh_optional_web_runtime", core_sync)
    migrations = source.index('log "Applying Alembic migrations"')

    assert core_sync < web_refresh < migrations
    assert "systemctl restart kanami-web-admin" not in source
    assert "systemctl start kanami-web-admin" not in source
    assert "systemctl enable kanami-web-admin" not in source


def test_updater_refreshes_canonical_web_unit_and_reapplies_grants() -> None:
    source = updater_source()
    unit = updater_function(source, "install_web_service_unit")
    grants = updater_function(source, "apply_web_database_grants")

    assert 'local service_source="${INSTALL_DIR}/deploy/systemd/' in unit
    assert "kanami-web-admin.service" in unit
    assert "-f ${service_source}" in unit
    assert "-r ${service_source}" in unit
    assert "! -L ${service_source}" in unit
    assert 'install -m 0644 -o root -g root "${service_source}"' in unit
    assert 'local grants_source="${INSTALL_DIR}/${WEB_GRANTS_SOURCE_RELATIVE}"' in (
        grants
    )
    assert "! -L ${grants_source}" in grants
    migrations = source.index('log "Applying Alembic migrations"')
    reapply = source.index(
        'log "Reapplying least-privilege Web Admin PostgreSQL grants"'
    )
    unit_refresh = source.index(
        'log "Refreshing inactive Web Admin systemd unit without starting it"'
    )
    daemon_reload = source.index("systemctl daemon-reload")
    assert migrations < reapply < unit_refresh < daemon_reload
