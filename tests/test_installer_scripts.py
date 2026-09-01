from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPOSITORY_ROOT / "scripts/install.sh"


def installer_source() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def manager_install_function(source: str) -> str:
    start = source.index("install_manager() {")
    end = source.index("\n}\n", start) + 3
    return source[start:end]


def installer_function(source: str, name: str) -> str:
    start = source.index(f"{name}() {{")
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
