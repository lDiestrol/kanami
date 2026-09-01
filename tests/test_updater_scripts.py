from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
UPDATER = REPOSITORY_ROOT / "scripts/update.sh"


def updater_source() -> str:
    return UPDATER.read_text(encoding="utf-8")


def manager_refresh_function(source: str) -> str:
    start = source.index("refresh_manager() {")
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
    pull = 'git -C "${INSTALL_DIR}" pull --ff-only'
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
