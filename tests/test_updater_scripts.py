from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
UPDATER = REPOSITORY_ROOT / "scripts/update.sh"


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
