from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPOSITORY_ROOT / "scripts/install.sh"


def installer_source() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def manager_install_function(source: str) -> str:
    start = source.index("install_manager() {")
    end = source.index("\n}\n", start) + 3
    return source[start:end]


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
