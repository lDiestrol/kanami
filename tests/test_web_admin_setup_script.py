import os
import re
import shutil
import socketserver
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SETUP_SCRIPT = REPOSITORY_ROOT / "scripts/web-admin-setup.sh"
MANAGED_CADDY_TEMPLATE = REPOSITORY_ROOT / "deploy/caddy/Caddyfile.managed.template"
BASH = shutil.which("bash")
RUN_WINDOWS_BASH_TESTS = os.environ.get("KANAMI_TEST_WINDOWS_BASH") == "1"
RUN_BASH_TESTS = BASH is not None and (
    sys.platform != "win32" or RUN_WINDOWS_BASH_TESTS
)
TEST_SECRET = "ab" * 32


def setup_source() -> str:
    return SETUP_SCRIPT.read_text(encoding="utf-8")


def shell_function_source(name: str) -> str:
    source = setup_source()
    start_match = re.search(rf"^{re.escape(name)}\(\) \{{\n", source, re.MULTILINE)
    assert start_match is not None
    next_function = re.search(
        r"^[a-z_][a-z0-9_]*\(\) \{\n", source[start_match.end() :], re.MULTILINE
    )
    end = (
        len(source)
        if next_function is None
        else start_match.end() + next_function.start()
    )
    return source[start_match.start() : end].rstrip()


def run_bash(script: str) -> subprocess.CompletedProcess[str]:
    if not RUN_BASH_TESTS:
        pytest.skip("isolated setup helper tests require Bash on a Unix-like host")
    assert BASH is not None
    environment = os.environ.copy()
    environment["MSYS2_ARG_CONV_EXCL"] = "*"
    return subprocess.run(
        [BASH, "-c", script],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def shell_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def env_reader_prelude() -> str:
    return "\n".join(
        (
            "set -Eeuo pipefail",
            'STAT="stat"',
            "MAX_ENV_BYTES=1048576",
            "MAX_ENV_LINES=4096",
            shell_function_source("fail"),
            shell_function_source("read_env_key"),
        )
    )


def function_bundle(*names: str) -> str:
    return "\n".join(shell_function_source(name) for name in names)


def web_network_script(web_file: Path) -> str:
    return "\n".join(
        (
            env_reader_prelude(),
            f"WEB_CONFIG_FILE={shell_quote(web_file)}",
            shell_function_source("validate_web_network_invariants"),
            "validate_web_network_invariants",
        )
    )


def test_setup_is_root_only_fail_closed_and_disables_xtrace_before_secrets() -> None:
    source = setup_source()

    assert source.startswith("#!/usr/bin/env bash\nset -Eeuo pipefail\nset +x\n")
    assert '((EUID == 0)) || fail "root is required' in source
    assert source.index("set +x") < source.index('bot_control_secret=""')
    assert not re.search(r"^\s*(?:source|eval)\s", source, re.MULTILINE)
    assert "safe.directory=*" not in source
    assert "chown -R" not in source
    assert "chmod -R" not in source


def test_setup_uses_only_canonical_production_paths_for_mutation() -> None:
    source = setup_source()

    for required in (
        'readonly INSTALL_DIR="/opt/kanami"',
        'readonly CORE_CONFIG_FILE="${CONFIG_DIR}/kanami.env"',
        'readonly WEB_CONFIG_FILE="${CONFIG_DIR}/kanami-web-admin.env"',
        'readonly CORE_SERVICE_FILE="/etc/systemd/system/kanami.service"',
        'readonly WEB_SERVICE_FILE="/etc/systemd/system/kanami-web-admin.service"',
        'readonly CADDY_CONFIG_FILE="${CADDY_CONFIG_DIR}/Caddyfile"',
    ):
        assert required in source
    assert "KANAMI_MANAGER_INSTALL_DIR" not in source
    assert "KANAMI_WEB_SETUP" not in source
    assert "${PWD}" not in source


def test_complete_d212_preflight_checks_users_paths_metadata_and_units() -> None:
    function = shell_function_source("validate_d212_installation")
    identity = shell_function_source("validate_service_identity_and_groups")

    for required in (
        '"${ID}" "${WEB_SERVICE_USER}"',
        'web_primary_group} == "${WEB_SERVICE_USER}"',
        '"${WEB_SERVICE_HOME}" "${web_uid}:${web_gid}:750"',
        '"${WEB_VENV_DIR}"',
        '"${WEB_UV_BOOTSTRAP_DIR}"',
        '"${WEB_UV_CACHE_DIR}"',
        '"${CORE_CONFIG_FILE}" "0:${core_gid}:640"',
        '"${WEB_CONFIG_FILE}" "0:${web_gid}:640"',
        '"${CORE_SERVICE_FILE}" "0:0:644"',
        '"${WEB_SERVICE_FILE}" "0:0:644"',
        'validate_unit_loaded "${CORE_SERVICE}"',
        'validate_unit_loaded "${WEB_SERVICE}"',
    ):
        assert required in function + identity
    assert '"${ID}" -G "${SERVICE_USER}"' in identity
    assert '"${ID}" -G "${WEB_SERVICE_USER}"' in identity
    assert '"${CONFIG_DIR}" "0:${core_gid}:750"' in function
    assert "chown" not in function
    assert "chmod" not in function


@pytest.mark.parametrize("allow_private_bind", [None, "false"])
def test_exact_d212_web_network_values_are_accepted(
    tmp_path: Path, allow_private_bind: str | None
) -> None:
    env_file = tmp_path / "web.env"
    lines = [
        "WEB_ADMIN_HOST=127.0.0.1",
        "WEB_ADMIN_PORT=8000",
        "WEB_ADMIN_COOKIE_SECURE=true",
    ]
    if allow_private_bind is not None:
        lines.append(f"WEB_ADMIN_ALLOW_PRIVATE_BIND={allow_private_bind}")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = run_bash(web_network_script(env_file))

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("WEB_ADMIN_HOST", "10.0.0.8"),
        ("WEB_ADMIN_PORT", "8080"),
        ("WEB_ADMIN_COOKIE_SECURE", "false"),
        ("WEB_ADMIN_ALLOW_PRIVATE_BIND", "true"),
    ],
)
def test_web_network_drift_is_rejected(tmp_path: Path, key: str, value: str) -> None:
    env_file = tmp_path / "web.env"
    values = {
        "WEB_ADMIN_HOST": "127.0.0.1",
        "WEB_ADMIN_PORT": "8000",
        "WEB_ADMIN_COOKIE_SECURE": "true",
        "WEB_ADMIN_ALLOW_PRIVATE_BIND": "false",
    }
    values[key] = value
    env_file.write_text(
        "".join(f"{name}={item}\n" for name, item in values.items()),
        encoding="utf-8",
    )

    result = run_bash(web_network_script(env_file))

    assert result.returncode != 0
    assert "managed same-host Caddy requires" in result.stderr


@pytest.mark.parametrize(
    "key",
    [
        "WEB_ADMIN_HOST",
        "WEB_ADMIN_PORT",
        "WEB_ADMIN_COOKIE_SECURE",
        "WEB_ADMIN_ALLOW_PRIVATE_BIND",
    ],
)
def test_duplicate_web_network_key_is_rejected(tmp_path: Path, key: str) -> None:
    env_file = tmp_path / "web.env"
    values = {
        "WEB_ADMIN_HOST": "127.0.0.1",
        "WEB_ADMIN_PORT": "8000",
        "WEB_ADMIN_COOKIE_SECURE": "true",
        "WEB_ADMIN_ALLOW_PRIVATE_BIND": "false",
    }
    env_file.write_text(
        "".join(f"{name}={item}\n" for name, item in values.items())
        + f"{key}={values[key]}\n",
        encoding="utf-8",
    )

    result = run_bash(web_network_script(env_file))

    assert result.returncode != 0
    assert f"duplicate critical key {key}" in result.stderr


def test_env_parser_rejects_duplicate_exact_critical_key(tmp_path: Path) -> None:
    env_file = tmp_path / "protected.env"
    env_file.write_text(
        "KEEP=value\nWEB_ADMIN_DISCORD_REDIRECT_URI=https://one.example/admin/auth/discord/callback\n"
        "WEB_ADMIN_DISCORD_REDIRECT_URI=https://two.example/admin/auth/discord/callback\n",
        encoding="utf-8",
    )
    result = run_bash(
        env_reader_prelude()
        + "\nvalue=''\npresent=false\n"
        + f"read_env_key {shell_quote(env_file)} WEB_ADMIN_DISCORD_REDIRECT_URI value present\n"
    )

    assert result.returncode != 0
    assert "duplicate critical key WEB_ADMIN_DISCORD_REDIRECT_URI" in result.stderr
    assert "one.example" not in result.stdout + result.stderr
    assert "two.example" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("core_groups", "web_groups", "accepted"),
    [
        ("1001 27", "1002 33", True),
        ("1001 27", "1002 1001 33", False),
        ("1001 1002 27", "1002 33", False),
    ],
)
def test_service_group_isolation_uses_numeric_membership(
    tmp_path: Path, core_groups: str, web_groups: str, accepted: bool
) -> None:
    fake_id = tmp_path / "id"
    fake_id.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1:${2-}" in\n'
        "  -g:kanami) echo 1001 ;;\n"
        "  -g:kanami-web) echo 1002 ;;\n"
        "  -u:kanami-web) echo 2002 ;;\n"
        "  -gn:kanami) echo kanami ;;\n"
        "  -gn:kanami-web) echo kanami-web ;;\n"
        f"  -G:kanami) echo '{core_groups}' ;;\n"
        f"  -G:kanami-web) echo '{web_groups}' ;;\n"
        "  kanami:|kanami-web:) exit 0 ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_id.chmod(0o755)
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            f"ID={shell_quote(fake_id)}",
            'SERVICE_USER="kanami"',
            'WEB_SERVICE_USER="kanami-web"',
            'core_gid=""',
            'web_gid=""',
            'web_uid=""',
            shell_function_source("fail"),
            shell_function_source("validate_service_identity_and_groups"),
            "validate_service_identity_and_groups",
        )
    )

    result = run_bash(script)

    assert (result.returncode == 0) is accepted
    if not accepted:
        assert "must not be a member" in result.stderr


@pytest.mark.parametrize(
    ("metadata", "accepted"),
    [
        ("0:1001:750", True),
        ("1:1001:750", False),
        ("0:1002:750", False),
        ("0:1001:770", False),
        ("0:1001:751", False),
    ],
)
def test_config_parent_requires_exact_d212_metadata(
    tmp_path: Path, metadata: str, accepted: bool
) -> None:
    config_dir = tmp_path / "etc-kanami"
    config_dir.mkdir()
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            shell_function_source("fail"),
            f"path_metadata() {{ printf '%s\\n' {shell_quote(metadata)}; }}",
            shell_function_source("validate_regular_directory"),
            f"validate_regular_directory {shell_quote(config_dir)} 0:1001:750 'Kanami configuration directory'",
        )
    )

    result = run_bash(script)

    assert (result.returncode == 0) is accepted


def test_config_parent_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    link = tmp_path / "etc-kanami"
    target.mkdir()
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this Windows host")
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            shell_function_source("fail"),
            "path_metadata() { printf '0:1001:750\\n'; }",
            shell_function_source("validate_regular_directory"),
            f"validate_regular_directory {shell_quote(link)} 0:1001:750 'Kanami configuration directory'",
        )
    )

    result = run_bash(script)

    assert result.returncode != 0
    assert "non-symlink directory" in result.stderr


@pytest.mark.parametrize(
    ("redirect", "accepted"),
    [
        ("https://admin.example.com/admin/auth/discord/callback", True),
        ("https://admin.example.com:443/admin/auth/discord/callback", True),
        ("http://admin.example.com/admin/auth/discord/callback", False),
        ("https://127.0.0.1/admin/auth/discord/callback", False),
        ("https://localhost/admin/auth/discord/callback", False),
        ("https://*.example.com/admin/auth/discord/callback", False),
        ("https://user@admin.example.com/admin/auth/discord/callback", False),
        ("https://admin.example.com:8443/admin/auth/discord/callback", False),
        ("https://admin.example.com/admin/auth/discord/callback?q=1", False),
        ("https://admin.example.com/admin/auth/discord/callback#fragment", False),
        ("https://admin.example.com/admin/wrong", False),
    ],
)
def test_redirect_validation_derives_only_public_dns_hostname(
    tmp_path: Path, redirect: str, accepted: bool
) -> None:
    env_file = tmp_path / "web.env"
    env_file.write_text(
        f"WEB_ADMIN_DISCORD_REDIRECT_URI={redirect}\n", encoding="utf-8"
    )
    script = "\n".join(
        (
            env_reader_prelude(),
            f"PYTHON={shell_quote(Path(sys.executable))}",
            f"WEB_CONFIG_FILE={shell_quote(env_file)}",
            'OAUTH_CALLBACK_PATH="/admin/auth/discord/callback"',
            'oauth_callback=""',
            'public_hostname=""',
            shell_function_source("validate_redirect_and_hostname"),
            "validate_redirect_and_hostname",
            'printf "%s\\n" "${public_hostname}"',
        )
    )
    result = run_bash(script)

    assert (result.returncode == 0) is accepted
    if accepted:
        assert result.stdout == "admin.example.com\n"
    else:
        assert redirect not in result.stdout + result.stderr


def pairing_script(core_file: Path, web_file: Path) -> str:
    return "\n".join(
        (
            env_reader_prelude(),
            f"CORE_CONFIG_FILE={shell_quote(core_file)}",
            f"WEB_CONFIG_FILE={shell_quote(web_file)}",
            'BOT_CONTROL_URL="http://127.0.0.1:8765"',
            'bot_control_state=""',
            'bot_control_secret=""',
            shell_function_source("inspect_bot_control_pairing"),
            "inspect_bot_control_pairing",
            '[[ ${bot_control_secret} == "'
            + TEST_SECRET
            + '" ]] || [[ -z ${bot_control_secret} ]]',
            'printf "%s\\n" "${bot_control_state}"',
        )
    )


def test_existing_exact_bot_control_pairing_is_idempotent(tmp_path: Path) -> None:
    core = tmp_path / "core.env"
    web = tmp_path / "web.env"
    core.write_text(
        "KEEP_CORE=yes\n"
        "DISCORD_BOT_CONTROL_ENABLED=true\n"
        "DISCORD_BOT_CONTROL_HOST=127.0.0.1\n"
        "DISCORD_BOT_CONTROL_PORT=8765\n"
        f"DISCORD_BOT_CONTROL_SHARED_SECRET={TEST_SECRET}\n",
        encoding="utf-8",
    )
    web.write_text(
        "KEEP_WEB=yes\n"
        "WEB_ADMIN_BOT_CONTROL_URL=http://127.0.0.1:8765\n"
        f"WEB_ADMIN_BOT_CONTROL_SHARED_SECRET={TEST_SECRET}\n",
        encoding="utf-8",
    )
    before = (core.read_bytes(), web.read_bytes())

    result = run_bash(pairing_script(core, web))

    assert result.returncode == 0
    assert result.stdout == "already paired\n"
    assert TEST_SECRET not in result.stdout + result.stderr
    assert (core.read_bytes(), web.read_bytes()) == before


@pytest.mark.parametrize("partial_side", ["core", "web", "mismatch"])
def test_partial_or_mismatched_bot_control_pairing_fails_closed(
    tmp_path: Path, partial_side: str
) -> None:
    core = tmp_path / "core.env"
    web = tmp_path / "web.env"
    core_lines = ["KEEP_CORE=yes"]
    web_lines = ["KEEP_WEB=yes"]
    if partial_side in {"core", "mismatch"}:
        core_lines.extend(
            (
                "DISCORD_BOT_CONTROL_ENABLED=true",
                "DISCORD_BOT_CONTROL_HOST=127.0.0.1",
                "DISCORD_BOT_CONTROL_PORT=8765",
                f"DISCORD_BOT_CONTROL_SHARED_SECRET={TEST_SECRET}",
            )
        )
    if partial_side in {"web", "mismatch"}:
        web_lines.extend(
            (
                "WEB_ADMIN_BOT_CONTROL_URL=http://127.0.0.1:8765",
                "WEB_ADMIN_BOT_CONTROL_SHARED_SECRET="
                + (("cd" * 32) if partial_side == "mismatch" else TEST_SECRET),
            )
        )
    core.write_text("\n".join(core_lines) + "\n", encoding="utf-8")
    web.write_text("\n".join(web_lines) + "\n", encoding="utf-8")
    before = (core.read_bytes(), web.read_bytes())

    result = run_bash(pairing_script(core, web))

    assert result.returncode != 0
    assert "Bot Control" in result.stderr
    assert TEST_SECRET not in result.stdout + result.stderr
    assert (core.read_bytes(), web.read_bytes()) == before


def test_first_pairing_writes_one_secret_and_preserves_unrelated_keys(
    tmp_path: Path,
) -> None:
    core = tmp_path / "core.env"
    web = tmp_path / "web.env"
    core.write_text("DISCORD_TOKEN=keep-core-secret\nKEEP_CORE=yes\n", encoding="utf-8")
    web.write_text(
        "WEB_ADMIN_DISCORD_CLIENT_SECRET=keep-web-secret\nKEEP_WEB=yes\n",
        encoding="utf-8",
    )
    fake_tool = tmp_path / "noop"
    fake_tool.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_tool.chmod(0o755)
    fake_openssl = tmp_path / "openssl"
    fake_openssl.write_text(
        f"#!/usr/bin/env bash\n[[ $* == 'rand -hex 32' ]] || exit 2\nprintf '%s\\n' '{TEST_SECRET}'\n",
        encoding="utf-8",
    )
    fake_openssl.chmod(0o755)
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            f"CORE_CONFIG_FILE={shell_quote(core)}",
            f"WEB_CONFIG_FILE={shell_quote(web)}",
            f"CONFIG_DIR={shell_quote(tmp_path)}",
            'BOT_CONTROL_URL="http://127.0.0.1:8765"',
            f"OPENSSL={shell_quote(fake_openssl)}",
            'MKTEMP="mktemp"',
            f"CHOWN={shell_quote(fake_tool)}",
            f"CHMOD={shell_quote(fake_tool)}",
            'MV="mv"',
            "core_gid=1001",
            "web_gid=1002",
            'bot_control_state="new pairing required"',
            'bot_control_secret=""',
            "TEMP_FILES=()",
            shell_function_source("log"),
            shell_function_source("fail"),
            shell_function_source("register_temp"),
            shell_function_source("validate_regular_file"),
            "path_metadata() {\n"
            "  if [[ $1 == *web-setup.* ]]; then\n"
            '    if [[ $1 == *kanami-web-admin* ]]; then printf "0:1002:600\\n"; '
            'else printf "0:1001:600\\n"; fi\n'
            '  elif [[ $1 == "${WEB_CONFIG_FILE}" ]]; then\n'
            "    printf '0:1002:640\\n'\n"
            "  else\n"
            "    printf '0:1001:640\\n'\n"
            "  fi\n"
            "}",
            shell_function_source("stage_paired_env_files"),
            "stage_paired_env_files",
        )
    )

    result = run_bash(script)

    assert result.returncode == 0, result.stderr
    assert TEST_SECRET not in result.stdout + result.stderr
    core_text = core.read_text(encoding="utf-8")
    web_text = web.read_text(encoding="utf-8")
    assert "DISCORD_TOKEN=keep-core-secret" in core_text
    assert "KEEP_CORE=yes" in core_text
    assert "WEB_ADMIN_DISCORD_CLIENT_SECRET=keep-web-secret" in web_text
    assert "KEEP_WEB=yes" in web_text
    assert f"DISCORD_BOT_CONTROL_SHARED_SECRET={TEST_SECRET}" in core_text
    assert f"WEB_ADMIN_BOT_CONTROL_SHARED_SECRET={TEST_SECRET}" in web_text
    assert "DISCORD_TOKEN" not in web_text
    assert "WEB_ADMIN_DISCORD_CLIENT_SECRET" not in core_text


def test_secret_staging_keeps_random_names_root_only_until_rename() -> None:
    function = shell_function_source("stage_paired_env_files")

    core_chown = function.index('"${CHOWN}" "0:${core_gid}" "${core_temp}"')
    temp_core_600 = function.index('"0:${core_gid}:600"')
    first_move = function.index('"${MV}" -f -- "${core_temp}"')
    first_replaced = function.index('core_env_replaced="true"')
    first_chmod = function.index('"${CHMOD}" 0640 "${CORE_CONFIG_FILE}"')
    web_move = function.index('"${MV}" -f -- "${web_temp}"')
    web_replaced = function.index('web_env_replaced="true"')
    web_chmod = function.index('"${CHMOD}" 0640 "${WEB_CONFIG_FILE}"')
    assert core_chown < temp_core_600 < first_move < first_replaced < first_chmod
    assert first_chmod < web_move < web_replaced < web_chmod
    assert '"${CHMOD}" 0640 "${core_temp}"' not in function
    assert '"${CHMOD}" 0640 "${web_temp}"' not in function


def test_failure_after_first_env_move_reports_safe_partial_state(
    tmp_path: Path,
) -> None:
    core = tmp_path / "core.env"
    web = tmp_path / "web.env"
    core.write_text("KEEP_CORE=yes\n", encoding="utf-8")
    web.write_text("KEEP_WEB=yes\n", encoding="utf-8")
    fake_openssl = tmp_path / "openssl"
    fake_openssl.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' '{TEST_SECRET}'\n", encoding="utf-8"
    )
    fake_openssl.chmod(0o755)
    fake_chmod = tmp_path / "chmod"
    fake_chmod.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$CHMOD_LOG"\nexit 23\n',
        encoding="utf-8",
    )
    fake_chmod.chmod(0o755)
    chmod_log = tmp_path / "chmod.log"
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            f"CORE_CONFIG_FILE={shell_quote(core)}",
            f"WEB_CONFIG_FILE={shell_quote(web)}",
            f"CONFIG_DIR={shell_quote(tmp_path)}",
            'BOT_CONTROL_URL="http://127.0.0.1:8765"',
            f"OPENSSL={shell_quote(fake_openssl)}",
            'MKTEMP="mktemp"',
            'CHOWN="true"',
            f"CHMOD={shell_quote(fake_chmod)}",
            'MV="mv"',
            'RM="rm"',
            'SYSTEMCTL="true"',
            f"export CHMOD_LOG={shell_quote(chmod_log)}",
            "core_gid=1001",
            "web_gid=1002",
            'bot_control_state="new pairing required"',
            'bot_control_secret=""',
            'mutation_confirmed="true"',
            'core_env_replaced="false"',
            'web_env_replaced="false"',
            'caddy_mask_created="false"',
            'caddy_install_attempted="false"',
            'CADDY_UNIT_MASK="unused"',
            'CADDY_SERVICE="caddy.service"',
            "TEMP_FILES=()",
            function_bundle("log", "fail", "register_temp", "validate_regular_file"),
            "path_metadata() {\n"
            '  if [[ $1 == *kanami-web-admin* ]]; then printf "0:1002:600\\n"; '
            'else printf "0:1001:600\\n"; fi\n'
            "}",
            shell_function_source("cleanup"),
            "trap cleanup EXIT",
            shell_function_source("stage_paired_env_files"),
            "stage_paired_env_files",
        )
    )

    result = run_bash(script)

    assert result.returncode == 23
    assert core.exists()
    assert f"DISCORD_BOT_CONTROL_SHARED_SECRET={TEST_SECRET}" in core.read_text(
        encoding="utf-8"
    )
    assert web.read_text(encoding="utf-8") == "KEEP_WEB=yes\n"
    assert chmod_log.read_text(encoding="utf-8").splitlines() == [f"0640 {core}"]
    assert "only one protected env file was replaced" in result.stderr
    assert TEST_SECRET not in result.stdout + result.stderr


def env_move_failure_script(
    tmp_path: Path, *, fail_on_move: int
) -> tuple[str, Path, Path, Path, Path]:
    core = tmp_path / "core.env"
    web = tmp_path / "web.env"
    core.write_text("KEEP_CORE=old-core\n", encoding="utf-8")
    web.write_text("KEEP_WEB=old-web\n", encoding="utf-8")
    state_log = tmp_path / "replacement-state.log"
    rm_log = tmp_path / "rm.log"
    move_count = tmp_path / "move-count"
    fake_openssl = tmp_path / "openssl-mv"
    fake_openssl.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' '{TEST_SECRET}'\n", encoding="utf-8"
    )
    fake_openssl.chmod(0o755)
    fake_mv = tmp_path / "mv-failure"
    fake_mv.write_text(
        "#!/usr/bin/env bash\n"
        "count=0\n"
        '[[ ! -f "$MOVE_COUNT" ]] || read -r count < "$MOVE_COUNT"\n'
        "count=$((count + 1))\n"
        'printf "%s\\n" "$count" > "$MOVE_COUNT"\n'
        'if [[ $count == "$FAIL_ON_MOVE" ]]; then exit 31; fi\n'
        '/usr/bin/mv "$@"\n',
        encoding="utf-8",
    )
    fake_mv.chmod(0o755)
    fake_rm = tmp_path / "rm-env"
    fake_rm.write_text(
        '#!/usr/bin/env bash\nprintf "rm %s\\n" "$*" >> "$RM_LOG"\n/usr/bin/rm "$@"\n',
        encoding="utf-8",
    )
    fake_rm.chmod(0o755)
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            f"CORE_CONFIG_FILE={shell_quote(core)}",
            f"WEB_CONFIG_FILE={shell_quote(web)}",
            f"CONFIG_DIR={shell_quote(tmp_path)}",
            'BOT_CONTROL_URL="http://127.0.0.1:8765"',
            f"OPENSSL={shell_quote(fake_openssl)}",
            'MKTEMP="mktemp"',
            'CHOWN="true"',
            'CHMOD="true"',
            f"MV={shell_quote(fake_mv)}",
            f"RM={shell_quote(fake_rm)}",
            'SYSTEMCTL="true"',
            'READLINK="readlink"',
            f"export MOVE_COUNT={shell_quote(move_count)}",
            f"export FAIL_ON_MOVE={fail_on_move}",
            f"export RM_LOG={shell_quote(rm_log)}",
            "core_gid=1001",
            "web_gid=1002",
            'bot_control_state="new pairing required"',
            'bot_control_secret=""',
            'mutation_confirmed="true"',
            'core_env_replaced="false"',
            'web_env_replaced="false"',
            'caddy_mask_created="false"',
            'caddy_install_attempted="false"',
            'CADDY_UNIT_MASK="unused"',
            'CADDY_SERVICE="caddy.service"',
            "TEMP_FILES=()",
            function_bundle("log", "fail", "register_temp", "validate_regular_file"),
            "path_metadata() {\n"
            "  if [[ $1 == *web-setup.* ]]; then\n"
            '    if [[ $1 == *kanami-web-admin* ]]; then printf "0:1002:600\\n"; '
            'else printf "0:1001:600\\n"; fi\n'
            '  elif [[ $1 == "${WEB_CONFIG_FILE}" ]]; then printf "0:1002:640\\n"\n'
            '  else printf "0:1001:640\\n"\n'
            "  fi\n"
            "}",
            shell_function_source("cleanup"),
            "observe_replacement_state_and_cleanup() {\n"
            "  status=$?\n"
            f'  printf "%s:%s\\n" "${{core_env_replaced}}" "${{web_env_replaced}}" > {shell_quote(state_log)}\n'
            "  set +e\n"
            '  (exit "${status}")\n'
            "  cleanup\n"
            "}",
            "trap observe_replacement_state_and_cleanup EXIT",
            shell_function_source("stage_paired_env_files"),
            "stage_paired_env_files",
        )
    )
    return script, core, web, state_log, rm_log


def test_first_env_move_failure_preserves_both_canonical_files(
    tmp_path: Path,
) -> None:
    script, core, web, state_log, rm_log = env_move_failure_script(
        tmp_path, fail_on_move=1
    )

    result = run_bash(script)

    assert result.returncode == 31
    assert state_log.read_text(encoding="utf-8") == "false:false\n"
    assert core.read_text(encoding="utf-8") == "KEEP_CORE=old-core\n"
    assert web.read_text(encoding="utf-8") == "KEEP_WEB=old-web\n"
    assert not list(tmp_path.glob(".*.web-setup.*"))
    assert rm_log.read_text(encoding="utf-8").count("rm -f --") == 2
    assert TEST_SECRET not in result.stdout + result.stderr


def test_second_env_move_failure_preserves_real_partial_state(
    tmp_path: Path,
) -> None:
    script, core, web, state_log, rm_log = env_move_failure_script(
        tmp_path, fail_on_move=2
    )

    result = run_bash(script)

    assert result.returncode == 31
    assert state_log.read_text(encoding="utf-8") == "true:false\n"
    assert f"DISCORD_BOT_CONTROL_SHARED_SECRET={TEST_SECRET}" in core.read_text(
        encoding="utf-8"
    )
    assert web.read_text(encoding="utf-8") == "KEEP_WEB=old-web\n"
    assert "only one protected env file was replaced" in result.stderr
    assert not list(tmp_path.glob(".*.web-setup.*"))
    assert rm_log.read_text(encoding="utf-8").count("rm -f --") == 1
    assert TEST_SECRET not in result.stdout + result.stderr


def test_caddy_managed_template_exposes_only_admin_namespace() -> None:
    config = MANAGED_CADDY_TEMPLATE.read_text(encoding="utf-8")

    assert config.count("__KANAMI_PUBLIC_HOST__") == 1
    assert "@root path /\n\t\tredir @root /admin/ 302" in config
    assert "@web_admin path /admin/*" in config
    assert "reverse_proxy @web_admin 127.0.0.1:8000" in config
    assert "respond 404" in config
    assert "/control" not in config
    assert "127.0.0.1:8765" not in config
    assert "Strict-Transport-Security" not in config
    assert "tls internal" not in config


def test_existing_caddy_must_match_exact_managed_hostname(tmp_path: Path) -> None:
    template = tmp_path / "template"
    config = tmp_path / "Caddyfile"
    template.write_text(
        MANAGED_CADDY_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    exact = template.read_text(encoding="utf-8").replace(
        "__KANAMI_PUBLIC_HOST__", "admin.example.com"
    )
    config.write_text(exact, encoding="utf-8")
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            f"CADDY_TEMPLATE={shell_quote(template)}",
            f"CADDY_CONFIG_FILE={shell_quote(config)}",
            'CMP="cmp"',
            'public_hostname="admin.example.com"',
            shell_function_source("render_managed_caddy"),
            shell_function_source("existing_config_is_managed_exact"),
            "existing_config_is_managed_exact",
        )
    )
    exact_result = run_bash(script)
    config.write_text(exact + "\nforeign.example { respond 200 }\n", encoding="utf-8")
    foreign_result = run_bash(script)

    assert exact_result.returncode == 0
    assert foreign_result.returncode != 0


@pytest.mark.parametrize("metadata", ["0:0:775", "0:0:757", "1:0:755"])
def test_caddy_directory_rejects_untrusted_metadata(
    tmp_path: Path, metadata: str
) -> None:
    caddy_dir = tmp_path / "caddy"
    caddy_dir.mkdir()
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            shell_function_source("fail"),
            f"path_metadata() {{ printf '%s\\n' {shell_quote(metadata)}; }}",
            shell_function_source("validate_root_owned_non_writable_directory"),
            f"validate_root_owned_non_writable_directory {shell_quote(caddy_dir)} 'Caddy configuration directory'",
        )
    )

    result = run_bash(script)

    assert result.returncode != 0


def test_caddy_directory_accepts_root_owned_non_writable_mode(tmp_path: Path) -> None:
    caddy_dir = tmp_path / "caddy"
    caddy_dir.mkdir()
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            shell_function_source("fail"),
            "path_metadata() { printf '0:0:750\\n'; }",
            shell_function_source("validate_root_owned_non_writable_directory"),
            f"validate_root_owned_non_writable_directory {shell_quote(caddy_dir)} 'Caddy configuration directory'",
        )
    )

    result = run_bash(script)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("as_symlink", [False, True])
def test_preexisting_caddy_directory_without_package_is_rejected(
    tmp_path: Path, as_symlink: bool
) -> None:
    caddy_dir = tmp_path / "etc-caddy"
    if as_symlink:
        target = tmp_path / "target"
        target.mkdir()
        try:
            caddy_dir.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("directory symlinks are unavailable on this Windows host")
    else:
        caddy_dir.mkdir()
    fake_dpkg = tmp_path / "dpkg-query"
    fake_dpkg.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    fake_dpkg.chmod(0o755)
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            f"DPKG_QUERY={shell_quote(fake_dpkg)}",
            f"CADDY_CONFIG_DIR={shell_quote(caddy_dir)}",
            f"CADDY={shell_quote(tmp_path / 'missing-caddy')}",
            f"CADDY_UNIT_MASK={shell_quote(tmp_path / 'caddy.service')}",
            f"CADDY_UNIT_DROPIN_DIR={shell_quote(tmp_path / 'caddy.service.d')}",
            f"CADDY_API_UNIT_OVERRIDE={shell_quote(tmp_path / 'caddy-api.service')}",
            f"CADDY_API_UNIT_DROPIN_DIR={shell_quote(tmp_path / 'caddy-api.service.d')}",
            'CADDY_SERVICE="caddy.service"',
            'caddy_installed=""',
            shell_function_source("fail"),
            "unit_is_present() { return 1; }",
            "require_unit_absent() { return 0; }",
            "reject_local_caddy_overrides() { return 0; }",
            shell_function_source("preflight_caddy"),
            "preflight_caddy",
        )
    )

    result = run_bash(script)

    assert result.returncode != 0
    assert "already exists" in result.stderr


def installed_caddy_preflight_script(
    tmp_path: Path,
    load_state: str,
    *,
    state_metadata: str = "2003:1003:750",
    state_dir: Path | None = None,
) -> str:
    if state_dir is None:
        state_dir = tmp_path / "var-lib-caddy"
        state_dir.mkdir(exist_ok=True)
    fake_systemctl = tmp_path / "systemctl"
    fake_systemctl.write_text(
        "#!/usr/bin/env bash\n"
        f"if [[ $1 == show ]]; then printf '%s\\n' '{load_state}'; exit 0; fi\n"
        "if [[ $1 == is-active ]]; then exit 3; fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o755)
    return "\n".join(
        (
            "set -Eeuo pipefail",
            f"SYSTEMCTL={shell_quote(fake_systemctl)}",
            'CADDY_SERVICE="caddy.service"',
            f"CADDY_CONFIG_DIR={shell_quote(tmp_path / 'caddy')}",
            f"CADDY_CONFIG_FILE={shell_quote(tmp_path / 'caddy' / 'Caddyfile')}",
            f"CADDY_STATE_DIR={shell_quote(state_dir)}",
            'caddy_uid=""',
            'caddy_gid=""',
            'public_hostname="admin.example.com"',
            shell_function_source("fail"),
            f"path_metadata() {{ printf '%s\\n' {shell_quote(state_metadata)}; }}",
            "validate_root_owned_non_writable_directory() { return 0; }",
            "validate_regular_file() { return 0; }",
            "existing_config_is_managed_exact() { return 0; }",
            "validate_caddy_identity() { caddy_uid=2003; caddy_gid=1003; }",
            shell_function_source("validate_owned_non_writable_directory"),
            "reject_local_caddy_overrides() { return 0; }",
            f"READLINK={shell_quote(fake_systemctl)}",
            "validate_caddy_effective_unit() {\n"
            f"  [[ {shell_quote(load_state)} == loaded ]] || fail 'bad load state'\n"
            "}",
            "validate_caddy_api_boot_state() { return 0; }",
            shell_function_source("validate_installed_caddy_preflight"),
            "validate_installed_caddy_preflight",
        )
    )


def test_existing_exact_managed_caddy_package_is_accepted(tmp_path: Path) -> None:
    result = run_bash(installed_caddy_preflight_script(tmp_path, "loaded"))

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "state_metadata",
    [
        "0:1003:750",
        "2003:0:750",
        "2003:1003:770",
        "2003:1003:752",
    ],
)
def test_existing_managed_caddy_rejects_state_directory_metadata_drift(
    tmp_path: Path, state_metadata: str
) -> None:
    result = run_bash(
        installed_caddy_preflight_script(
            tmp_path, "loaded", state_metadata=state_metadata
        )
    )

    assert result.returncode != 0
    assert "Caddy state directory" in result.stderr


def test_existing_managed_caddy_rejects_symlink_state_directory(
    tmp_path: Path,
) -> None:
    target = tmp_path / "state-target"
    state_dir = tmp_path / "var-lib-caddy"
    target.mkdir()
    try:
        state_dir.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this Windows host")

    result = run_bash(
        installed_caddy_preflight_script(tmp_path, "loaded", state_dir=state_dir)
    )

    assert result.returncode != 0
    assert "non-symlink directory" in result.stderr


def test_installed_caddy_state_rejections_are_read_only_and_pre_confirmation() -> None:
    function = shell_function_source("validate_installed_caddy_preflight")
    main = shell_function_source("main")

    assert main.index("preflight_caddy") < main.index("confirm_activation")
    assert (
        function.index("validate_caddy_identity")
        < function.index("validate_owned_non_writable_directory")
        < function.index("validate_caddy_effective_unit")
    )
    for forbidden in (
        "chmod",
        "chown",
        '"${MV}"',
        "systemctl start",
        "systemctl enable",
    ):
        assert forbidden not in function


@pytest.mark.parametrize("load_state", ["masked", "error", "not-found"])
def test_bad_caddy_unit_is_rejected_before_mutation(
    tmp_path: Path, load_state: str
) -> None:
    result = run_bash(installed_caddy_preflight_script(tmp_path, load_state))

    assert result.returncode != 0
    assert "bad load state" in result.stderr


def test_foreign_caddy_dropin_is_rejected(tmp_path: Path) -> None:
    dropin = tmp_path / "caddy.service.d"
    dropin.mkdir()
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            f"CADDY_UNIT_MASK={shell_quote(tmp_path / 'caddy.service')}",
            f"CADDY_UNIT_DROPIN_DIR={shell_quote(dropin)}",
            f"CADDY_API_UNIT_OVERRIDE={shell_quote(tmp_path / 'caddy-api.service')}",
            f"CADDY_API_UNIT_DROPIN_DIR={shell_quote(tmp_path / 'caddy-api.service.d')}",
            shell_function_source("fail"),
            shell_function_source("reject_local_caddy_overrides"),
            "reject_local_caddy_overrides",
        )
    )

    result = run_bash(script)

    assert result.returncode != 0
    assert "drop-ins" in result.stderr


@pytest.mark.parametrize(
    ("groups", "accepted"),
    [
        ("1003 33", True),
        ("1003 1001 33", False),
        ("1003 1002 33", False),
    ],
)
def test_caddy_service_user_group_isolation(
    tmp_path: Path, groups: str, accepted: bool
) -> None:
    fake_id = tmp_path / "id"
    fake_id.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1:${2-}" in\n'
        "  caddy:) exit 0 ;;\n"
        "  -u:caddy) echo 2003 ;;\n"
        "  -g:caddy) echo 1003 ;;\n"
        "  -gn:caddy) echo caddy ;;\n"
        f"  -G:caddy) echo '{groups}' ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_id.chmod(0o755)
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            f"ID={shell_quote(fake_id)}",
            'SERVICE_USER="kanami"',
            'WEB_SERVICE_USER="kanami-web"',
            "core_gid=1001",
            "web_gid=1002",
            'caddy_uid=""',
            'caddy_gid=""',
            shell_function_source("fail"),
            shell_function_source("validate_caddy_identity"),
            "validate_caddy_identity",
        )
    )

    result = run_bash(script)

    assert (result.returncode == 0) is accepted
    if not accepted:
        assert "must not be a member" in result.stderr


def effective_caddy_unit_script(
    tmp_path: Path, fragment_path: str, drop_in_paths: str
) -> str:
    fake_systemctl = tmp_path / "systemctl"
    fake_systemctl.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        "  *LoadState*) echo loaded ;;\n"
        f"  *FragmentPath*) echo '{fragment_path}' ;;\n"
        f"  *DropInPaths*) echo '{drop_in_paths}' ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o755)
    fake_readlink = tmp_path / "readlink"
    fake_readlink.write_text(
        "#!/usr/bin/env bash\n"
        'case "${@: -1}" in\n'
        "  /usr/lib/systemd/system/caddy.service|/lib/systemd/system/caddy.service)\n"
        "    echo /usr/lib/systemd/system/caddy.service ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_readlink.chmod(0o755)
    return "\n".join(
        (
            "set -Eeuo pipefail",
            f"SYSTEMCTL={shell_quote(fake_systemctl)}",
            f"READLINK={shell_quote(fake_readlink)}",
            'CADDY_SERVICE="caddy.service"',
            'CADDY_VENDOR_UNIT="/usr/lib/systemd/system/caddy.service"',
            'CADDY_VENDOR_UNIT_LEGACY="/lib/systemd/system/caddy.service"',
            function_bundle(
                "fail", "validate_unit_loaded", "validate_caddy_effective_unit"
            ),
            "validate_caddy_effective_unit",
        )
    )


@pytest.mark.parametrize(
    "fragment_path",
    [
        "/usr/lib/systemd/system/caddy.service",
        "/lib/systemd/system/caddy.service",
    ],
)
def test_canonical_caddy_vendor_fragment_is_accepted(
    tmp_path: Path, fragment_path: str
) -> None:
    result = run_bash(effective_caddy_unit_script(tmp_path, fragment_path, ""))

    assert result.returncode == 0, result.stderr


def test_unexpected_effective_caddy_fragment_is_rejected(tmp_path: Path) -> None:
    result = run_bash(
        effective_caddy_unit_script(
            tmp_path, "/run/systemd/transient/caddy.service", ""
        )
    )

    assert result.returncode != 0
    assert "canonical Debian vendor unit" in result.stderr


def test_effective_caddy_dropins_are_rejected(tmp_path: Path) -> None:
    result = run_bash(
        effective_caddy_unit_script(
            tmp_path,
            "/usr/lib/systemd/system/caddy.service",
            "/run/systemd/system/caddy.service.d/override.conf",
        )
    )

    assert result.returncode != 0
    assert "unknown effective drop-ins" in result.stderr


@pytest.mark.parametrize(
    ("active_state", "enabled_state", "accepted"),
    [
        ("inactive", "disabled", True),
        ("active", "disabled", False),
        ("inactive", "enabled", False),
    ],
)
def test_caddy_api_must_be_inactive_and_disabled(
    tmp_path: Path, active_state: str, enabled_state: str, accepted: bool
) -> None:
    fake_systemctl = tmp_path / "systemctl"
    fake_systemctl.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ $1 == show ]]; then "
        f"echo '{active_state}'; exit 0; fi\n"
        "if [[ $1 == is-enabled ]]; then "
        f"echo '{enabled_state}'; "
        f"[[ '{enabled_state}' == enabled ]]; exit $?; fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o755)
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            f"SYSTEMCTL={shell_quote(fake_systemctl)}",
            shell_function_source("fail"),
            shell_function_source("validate_caddy_api_boot_state"),
            "validate_caddy_api_boot_state",
        )
    )

    result = run_bash(script)

    assert (result.returncode == 0) is accepted
    if not accepted:
        assert "caddy-api.service must be" in result.stderr


def test_caddy_first_install_suppresses_package_start_and_enablement() -> None:
    source = setup_source()
    install_function = shell_function_source("install_caddy_package_safely")
    cleanup_function = shell_function_source("cleanup")

    mask = install_function.index("create_temporary_caddy_mask")
    apt = install_function.index('"${APT_GET}" install')
    disable = install_function.index('"${SYSTEMCTL}" disable')
    active_check = install_function.index('"${SYSTEMCTL}" is-active')
    assert mask < apt < disable < active_check
    assert "deb-systemd-helper" not in source
    assert "Cloudsmith" not in source
    assert "external apt" not in source
    assert '"${SYSTEMCTL}" disable "${CADDY_SERVICE}"' in cleanup_function
    assert '"${READLINK}" -- "${CADDY_UNIT_MASK}"' in cleanup_function
    assert "Installing Caddy from configured APT sources." in install_function
    assert "configured Debian 13 repositories" not in source


def package_install_script(tmp_path: Path, *, caddy_active: bool) -> tuple[str, Path]:
    call_log = tmp_path / "calls.log"
    fake_apt = tmp_path / "apt-get"
    fake_apt.write_text(
        '#!/usr/bin/env bash\nprintf \'apt umask=%s\\n\' "$(umask)" >> "$CALL_LOG"\n',
        encoding="utf-8",
    )
    fake_apt.chmod(0o755)
    fake_dpkg = tmp_path / "dpkg-query"
    fake_dpkg.write_text("#!/usr/bin/env bash\necho installed\n", encoding="utf-8")
    fake_dpkg.chmod(0o755)
    fake_caddy = tmp_path / "caddy"
    fake_caddy.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_caddy.chmod(0o755)
    fake_systemctl = tmp_path / "systemctl"
    active_exit = "0" if caddy_active else "3"
    fake_systemctl.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'systemctl %s\\n\' "$*" >> "$CALL_LOG"\n'
        "if [[ $1 == show ]]; then echo inactive; exit 0; fi\n"
        "if [[ $1 == is-enabled ]]; then echo disabled; exit 1; fi\n"
        f"if [[ $1 == is-active ]]; then exit {active_exit}; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o755)
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            f"export CALL_LOG={shell_quote(call_log)}",
            f"APT_GET={shell_quote(fake_apt)}",
            f"DPKG_QUERY={shell_quote(fake_dpkg)}",
            f"CADDY={shell_quote(fake_caddy)}",
            f"SYSTEMCTL={shell_quote(fake_systemctl)}",
            f"CADDY_CONFIG_DIR={shell_quote(tmp_path / 'etc-caddy')}",
            f"CADDY_STATE_DIR={shell_quote(tmp_path / 'var-lib-caddy')}",
            'CADDY_SERVICE="caddy.service"',
            'SERVICE_USER="kanami"',
            'WEB_SERVICE_USER="kanami-web"',
            'caddy_installed="false"',
            'caddy_install_attempted="false"',
            'caddy_mask_created="false"',
            'caddy_uid=""',
            'caddy_gid=""',
            shell_function_source("log"),
            shell_function_source("fail"),
            "create_temporary_caddy_mask() {\n"
            '  printf "mask create\\n" >> "${CALL_LOG}"\n'
            '  caddy_mask_created="true"\n'
            "}",
            "validate_root_owned_non_writable_directory() { return 0; }",
            "validate_caddy_identity() { caddy_uid=2003; caddy_gid=1003; }",
            "validate_owned_non_writable_directory() {\n"
            '  printf "state-check %s %s %s\\n" "$1" "$2" "$3" >> "${CALL_LOG}"\n'
            "}",
            shell_function_source("validate_caddy_api_boot_state"),
            shell_function_source("install_caddy_package_safely"),
            "umask 077",
            "install_caddy_package_safely",
        )
    )
    return script, call_log


def real_cleanup_tools(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    call_log = tmp_path / "cleanup-calls.log"
    fake_systemctl = tmp_path / "systemctl-cleanup"
    fake_systemctl.write_text(
        "#!/usr/bin/env bash\n"
        'printf "systemctl %s\\n" "$*" >> "$CALL_LOG"\n'
        'case "$1" in\n'
        "  show) echo inactive; exit 0 ;;\n"
        "  is-enabled) echo disabled; exit 1 ;;\n"
        '  is-active) exit "${CADDY_ACTIVE_EXIT:-3}" ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o755)
    fake_readlink = tmp_path / "readlink-cleanup"
    fake_readlink.write_text(
        "#!/usr/bin/env bash\n"
        'printf "readlink %s\\n" "$*" >> "$CALL_LOG"\n'
        '/usr/bin/readlink "$@"\n',
        encoding="utf-8",
    )
    fake_readlink.chmod(0o755)
    fake_rm = tmp_path / "rm-cleanup"
    fake_rm.write_text(
        "#!/usr/bin/env bash\n"
        'printf "rm %s\\n" "$*" >> "$CALL_LOG"\n'
        '/usr/bin/rm "$@"\n',
        encoding="utf-8",
    )
    fake_rm.chmod(0o755)
    return call_log, fake_systemctl, fake_readlink, fake_rm


def real_cleanup_harness(
    tmp_path: Path, *, mask_target: str = "/dev/null"
) -> tuple[str, Path, Path]:
    call_log, fake_systemctl, fake_readlink, fake_rm = real_cleanup_tools(tmp_path)
    mask = tmp_path / "caddy.service"
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            f"export CALL_LOG={shell_quote(call_log)}",
            f"SYSTEMCTL={shell_quote(fake_systemctl)}",
            f"READLINK={shell_quote(fake_readlink)}",
            f"RM={shell_quote(fake_rm)}",
            f"CADDY_UNIT_MASK={shell_quote(mask)}",
            'CADDY_SERVICE="caddy.service"',
            "TEMP_FILES=()",
            'caddy_mask_created="true"',
            'caddy_install_attempted="true"',
            'mutation_confirmed="true"',
            'core_env_replaced="false"',
            'web_env_replaced="false"',
            f'bot_control_secret="{TEST_SECRET}"',
            shell_function_source("cleanup"),
            "trap cleanup EXIT",
            f"ln -s {shell_quote(mask_target)} {shell_quote(mask)}",
            "exit 47",
        )
    )
    return script, call_log, mask


def real_package_failure_script(
    tmp_path: Path, *, apt_exit: int, caddy_active: bool
) -> tuple[str, Path, Path]:
    call_log, fake_systemctl, fake_readlink, fake_rm = real_cleanup_tools(tmp_path)
    mask = tmp_path / "caddy.service"
    fake_apt = tmp_path / "apt-get-failure"
    fake_apt.write_text(
        "#!/usr/bin/env bash\n"
        'printf "apt-get %s\\n" "$*" >> "$CALL_LOG"\n'
        f"exit {apt_exit}\n",
        encoding="utf-8",
    )
    fake_apt.chmod(0o755)
    fake_dpkg = tmp_path / "dpkg-query-failure"
    fake_dpkg.write_text("#!/usr/bin/env bash\necho installed\n", encoding="utf-8")
    fake_dpkg.chmod(0o755)
    fake_caddy = tmp_path / "caddy-failure"
    fake_caddy.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_caddy.chmod(0o755)
    active_exit = "0" if caddy_active else "3"
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            f"export CALL_LOG={shell_quote(call_log)}",
            f"export CADDY_ACTIVE_EXIT={active_exit}",
            f"SYSTEMCTL={shell_quote(fake_systemctl)}",
            f"READLINK={shell_quote(fake_readlink)}",
            f"RM={shell_quote(fake_rm)}",
            'LN="ln"',
            f"APT_GET={shell_quote(fake_apt)}",
            f"DPKG_QUERY={shell_quote(fake_dpkg)}",
            f"CADDY={shell_quote(fake_caddy)}",
            f"CADDY_UNIT_MASK={shell_quote(mask)}",
            f"CADDY_CONFIG_DIR={shell_quote(tmp_path / 'etc-caddy')}",
            f"CADDY_STATE_DIR={shell_quote(tmp_path / 'var-lib-caddy')}",
            'CADDY_SERVICE="caddy.service"',
            'SERVICE_USER="kanami"',
            'WEB_SERVICE_USER="kanami-web"',
            "TEMP_FILES=()",
            'caddy_installed="false"',
            'caddy_mask_created="false"',
            'caddy_install_attempted="false"',
            'mutation_confirmed="true"',
            'core_env_replaced="false"',
            'web_env_replaced="false"',
            'caddy_uid=""',
            'caddy_gid=""',
            f'bot_control_secret="{TEST_SECRET}"',
            function_bundle("log", "warn", "fail", "cleanup"),
            "trap cleanup EXIT",
            shell_function_source("create_temporary_caddy_mask"),
            "validate_root_owned_non_writable_directory() { return 0; }",
            "validate_caddy_identity() { caddy_uid=2003; caddy_gid=1003; }",
            "validate_owned_non_writable_directory() { return 0; }",
            shell_function_source("validate_caddy_api_boot_state"),
            shell_function_source("install_caddy_package_safely"),
            "install_caddy_package_safely",
        )
    )
    return script, call_log, mask


def test_new_caddy_install_uses_controlled_umask_and_disables_api(
    tmp_path: Path,
) -> None:
    script, call_log = package_install_script(tmp_path, caddy_active=False)

    result = run_bash(script)

    assert result.returncode == 0, result.stderr
    calls = call_log.read_text(encoding="utf-8")
    assert "apt umask=0022" in calls
    assert "systemctl disable caddy-api.service" in calls
    assert "systemctl is-enabled caddy-api.service" in calls
    assert "state-check" in calls
    assert "2003 1003" in calls


@pytest.mark.parametrize("metadata", ["2003:1003:770", "0:1003:750", "2003:0:750"])
def test_unsafe_caddy_state_directory_is_rejected(
    tmp_path: Path, metadata: str
) -> None:
    state_dir = tmp_path / "var-lib-caddy"
    state_dir.mkdir()
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            shell_function_source("fail"),
            f"path_metadata() {{ printf '%s\\n' {shell_quote(metadata)}; }}",
            shell_function_source("validate_owned_non_writable_directory"),
            f"validate_owned_non_writable_directory {shell_quote(state_dir)} 2003 1003 'Caddy state directory'",
        )
    )

    result = run_bash(script)

    assert result.returncode != 0


def test_real_cleanup_stops_disables_and_removes_only_owned_mask(
    tmp_path: Path,
) -> None:
    script, call_log, mask = real_cleanup_harness(tmp_path)

    result = run_bash(script)

    assert result.returncode == 47
    calls = call_log.read_text(encoding="utf-8")
    ordered = (
        "systemctl stop caddy.service caddy-api.service",
        "systemctl disable caddy.service",
        "systemctl disable caddy-api.service",
        f"rm -f -- {mask}",
        "systemctl daemon-reload",
    )
    positions = [calls.index(call) for call in ordered]
    assert positions == sorted(positions)
    assert not mask.exists()
    assert TEST_SECRET not in result.stdout + result.stderr


def test_real_cleanup_never_removes_foreign_mask(tmp_path: Path) -> None:
    script, call_log, mask = real_cleanup_harness(tmp_path, mask_target="/dev/zero")

    result = run_bash(script)

    assert result.returncode == 47
    calls = call_log.read_text(encoding="utf-8")
    assert "readlink" in calls
    assert "rm -f" not in calls
    assert "systemctl daemon-reload" not in calls
    assert mask.exists()
    assert TEST_SECRET not in result.stdout + result.stderr


def test_apt_failure_uses_real_cleanup_and_removes_owned_mask(
    tmp_path: Path,
) -> None:
    script, call_log, mask = real_package_failure_script(
        tmp_path, apt_exit=41, caddy_active=False
    )

    result = run_bash(script)

    assert result.returncode == 41
    calls = call_log.read_text(encoding="utf-8")
    assert "apt-get install --no-install-recommends -y caddy" in calls
    assert "systemctl stop caddy.service caddy-api.service" in calls
    assert "systemctl disable caddy.service" in calls
    assert "systemctl disable caddy-api.service" in calls
    assert "rm -f --" in calls
    assert "systemctl daemon-reload" in calls
    assert not mask.exists()
    assert "production activation completed" not in result.stdout + result.stderr
    assert TEST_SECRET not in result.stdout + result.stderr


def test_unexpected_new_package_start_uses_real_cleanup(
    tmp_path: Path,
) -> None:
    script, call_log, mask = real_package_failure_script(
        tmp_path, apt_exit=0, caddy_active=True
    )

    result = run_bash(script)

    assert result.returncode != 0
    calls = call_log.read_text(encoding="utf-8")
    cleanup_stop = calls.index("systemctl stop caddy.service caddy-api.service")
    cleanup_disable = calls.rindex("systemctl disable caddy.service")
    cleanup_remove = calls.index("rm -f --")
    cleanup_reload = calls.rindex("systemctl daemon-reload")
    assert cleanup_stop < cleanup_disable < cleanup_remove < cleanup_reload
    assert not mask.exists()
    assert "became active" in result.stderr
    assert "production activation completed" not in result.stdout + result.stderr
    assert TEST_SECRET not in result.stdout + result.stderr


def test_owned_caddy_mask_has_success_and_failure_cleanup_only() -> None:
    create = shell_function_source("create_temporary_caddy_mask")
    remove = shell_function_source("remove_temporary_caddy_mask")
    cleanup = shell_function_source("cleanup")
    config = shell_function_source("install_and_validate_caddy_config")
    main = shell_function_source("main")

    assert create.index('"${LN}" -s /dev/null') < create.index(
        'caddy_mask_created="true"'
    )
    assert config.index('"${CADDY}" validate') < config.index(
        "remove_temporary_caddy_mask"
    )
    assert 'caddy_mask_created} == "true"' in cleanup
    assert '"${READLINK}" -- "${CADDY_UNIT_MASK}"' in cleanup
    assert '== "/dev/null"' in cleanup
    assert 'caddy_mask_created="false"' in remove
    assert main.index("preflight_caddy") < main.index("confirm_activation")


def test_absent_caddy_unit_check_fails_closed_on_inspection_error(
    tmp_path: Path,
) -> None:
    fake_systemctl = tmp_path / "systemctl"
    fake_systemctl.write_text("#!/usr/bin/env bash\nexit 5\n", encoding="utf-8")
    fake_systemctl.chmod(0o755)
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            f"SYSTEMCTL={shell_quote(fake_systemctl)}",
            shell_function_source("fail"),
            shell_function_source("require_unit_absent"),
            "require_unit_absent caddy.service",
        )
    )

    result = run_bash(script)

    assert result.returncode != 0
    assert "cannot inspect caddy.service load state" in result.stderr


def test_caddy_config_is_validated_as_debian_service_user() -> None:
    function = shell_function_source("install_and_validate_caddy_config")
    identity = shell_function_source("validate_caddy_identity")

    assert "validate_caddy_identity" in function
    assert '"${ID}" -g caddy' in identity
    assert 'caddy_primary_group} == "caddy"' in identity
    assert '"${CHOWN}" "0:${caddy_gid}" "${config_temp}"' in function
    assert '"${CHMOD}" 0640 "${config_temp}"' in function
    assert '"${RUNUSER}" -u caddy -- "${ENV}" HOME=/var/lib/caddy' in function
    assert '"${CADDY}" validate --config "${config_temp}"' in function
    assert '"${INSTALL}" -m 0644 -o root -g root' in function


@pytest.mark.parametrize(
    ("load_state", "active_state", "enabled_state", "accepted"),
    [
        ("loaded", "active", "enabled", True),
        ("loaded", "inactive", "enabled", False),
        ("loaded", "active", "disabled", False),
    ],
)
def test_core_must_already_be_active_and_enabled(
    tmp_path: Path,
    load_state: str,
    active_state: str,
    enabled_state: str,
    accepted: bool,
) -> None:
    fake_systemctl = tmp_path / "systemctl"
    fake_systemctl.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ $1 == show && $* == *LoadState* ]]; then "
        f"printf '%s\\n' '{load_state}'; exit 0; fi\n"
        "if [[ $1 == show && $* == *ActiveState* ]]; then "
        f"printf '%s\\n' '{active_state}'; exit 0; fi\n"
        "if [[ $1 == is-enabled ]]; then "
        f"printf '%s\\n' '{enabled_state}'; "
        f"[[ '{enabled_state}' == enabled ]]; exit $?; fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o755)
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            f"SYSTEMCTL={shell_quote(fake_systemctl)}",
            'CORE_SERVICE="kanami.service"',
            function_bundle("fail", "validate_unit_loaded", "validate_core_activation"),
            "validate_core_activation",
        )
    )

    result = run_bash(script)

    assert (result.returncode == 0) is accepted
    if not accepted:
        assert "finish Core activation" in result.stderr


def test_core_preflight_precedes_confirmation_and_all_enablement() -> None:
    main = shell_function_source("main")

    assert main.index("validate_core_activation") < main.index("confirm_activation")
    assert main.index("validate_core_activation") < main.index(
        "enable_services_after_smoke"
    )


def test_local_web_health_uses_direct_http_connection_despite_proxy_env() -> None:
    function = shell_function_source("start_web_and_smoke")
    match = re.search(r'"\$\{PYTHON\}" - <<\'PY\'\n(.*?)\nPY', function, re.DOTALL)
    assert match is not None
    probe = match.group(1)
    assert 'http.client.HTTPConnection("127.0.0.1", 8000, timeout=3)' in probe
    assert "urllib" not in probe
    assert "response.read(1025)" in probe

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            assert self.path == "/admin/health"
            body = b'{"status":"healthy"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    with socketserver.TCPServer(("127.0.0.1", 0), HealthHandler) as server:
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        environment = os.environ.copy()
        environment.update(
            {
                "HTTP_PROXY": "http://127.0.0.1:1",
                "HTTPS_PROXY": "http://127.0.0.1:1",
                "ALL_PROXY": "http://127.0.0.1:1",
                "NO_PROXY": "",
            }
        )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                probe.replace(
                    'http.client.HTTPConnection("127.0.0.1", 8000, timeout=3)',
                    f'http.client.HTTPConnection("127.0.0.1", {port}, timeout=3)',
                ),
            ],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        server.shutdown()
        thread.join(timeout=2)

    assert result.returncode == 0, result.stderr


def test_public_https_smoke_bypasses_proxy_and_requires_http_200() -> None:
    function = shell_function_source("public_https_smoke")

    assert "--noproxy '*'" in function
    assert "--output /dev/null" in function
    assert "--write-out '%{http_code}'" in function
    assert '[[ ${http_status} == "200" ]]' in function


@pytest.mark.parametrize(
    ("curl_exit", "http_status", "successful"),
    [
        (7, "", False),
        (0, "503", False),
        (0, "200", True),
    ],
)
def test_public_https_smoke_runtime_is_best_effort(
    tmp_path: Path, curl_exit: int, http_status: str, successful: bool
) -> None:
    call_log = tmp_path / "curl.log"
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$CURL_LOG"\n'
        f"printf '%s' '{http_status}'\n"
        f"exit {curl_exit}\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            f"export CURL_LOG={shell_quote(call_log)}",
            f"CURL={shell_quote(fake_curl)}",
            'public_hostname="admin.example.com"',
            function_bundle("log", "warn", "public_https_smoke"),
            "public_https_smoke",
        )
    )

    result = run_bash(script)

    assert result.returncode == 0
    call = call_log.read_text(encoding="utf-8")
    assert "--noproxy *" in call
    assert "--output /dev/null" in call
    assert "--write-out %{http_code}" in call
    if successful:
        assert "responded with HTTP 200" in result.stdout
        assert "WARNING" not in result.stderr
    else:
        assert "WARNING" in result.stderr
        assert "responded with HTTP 200" not in result.stdout


@pytest.mark.parametrize(
    ("failed_stage", "expected_calls"),
    [
        (
            "smoke_bot_control",
            [
                "stage_paired_env_files",
                "install_caddy_package_safely",
                "install_and_validate_caddy_config",
                "daemon-reload",
                "restart_core_and_require_active",
                "smoke_bot_control",
            ],
        ),
        (
            "start_web_and_smoke",
            [
                "stage_paired_env_files",
                "install_caddy_package_safely",
                "install_and_validate_caddy_config",
                "daemon-reload",
                "restart_core_and_require_active",
                "smoke_bot_control",
                "start_web_and_smoke",
            ],
        ),
        (
            "activate_caddy",
            [
                "stage_paired_env_files",
                "install_caddy_package_safely",
                "install_and_validate_caddy_config",
                "daemon-reload",
                "restart_core_and_require_active",
                "smoke_bot_control",
                "start_web_and_smoke",
                "activate_caddy",
            ],
        ),
        (
            "none",
            [
                "stage_paired_env_files",
                "install_caddy_package_safely",
                "install_and_validate_caddy_config",
                "daemon-reload",
                "restart_core_and_require_active",
                "smoke_bot_control",
                "start_web_and_smoke",
                "activate_caddy",
                "public_https_smoke",
                "enable_services_after_smoke",
            ],
        ),
    ],
)
def test_post_confirmation_orchestration_short_circuits_on_failure(
    tmp_path: Path, failed_stage: str, expected_calls: list[str]
) -> None:
    call_log = tmp_path / "orchestration.log"
    helper_names = (
        "stage_paired_env_files",
        "install_caddy_package_safely",
        "install_and_validate_caddy_config",
        "restart_core_and_require_active",
        "smoke_bot_control",
        "start_web_and_smoke",
        "activate_caddy",
        "public_https_smoke",
        "enable_services_after_smoke",
    )
    fake_helpers = []
    for name in helper_names:
        fake_helpers.append(
            f"{name}() {{ printf '%s\\n' {shell_quote(name)} >> \"${{CALL_LOG}}\"; "
            f"[[ $FAILED_STAGE != {shell_quote(name)} ]]; }}"
        )
    script = "\n".join(
        (
            "set -Eeuo pipefail",
            f"export CALL_LOG={shell_quote(call_log)}",
            f"FAILED_STAGE={shell_quote(failed_stage)}",
            shell_function_source("fail"),
            *fake_helpers,
            "SYSTEMCTL=systemctl",
            "systemctl() { printf 'daemon-reload\\n' >> \"${CALL_LOG}\"; }",
            "stage_paired_env_files",
            "install_caddy_package_safely",
            "install_and_validate_caddy_config",
            '"${SYSTEMCTL}" daemon-reload',
            "restart_core_and_require_active",
            'smoke_bot_control || fail "authenticated Bot Control readiness check did not succeed before its deadline"',
            'start_web_and_smoke || fail "Web Admin local health did not become healthy before its deadline"',
            "activate_caddy",
            "public_https_smoke",
            "enable_services_after_smoke",
        )
    )

    result = run_bash(script)

    assert (result.returncode == 0) is (failed_stage == "none")
    assert call_log.read_text(encoding="utf-8").splitlines() == expected_calls
    if failed_stage != "none":
        assert "enable_services_after_smoke" not in expected_calls


def test_confirmation_precedes_all_production_mutations_and_cancellation_returns() -> (
    None
):
    main = shell_function_source("main")
    summary = main.index("show_summary")
    confirmation = main.index("if ! confirm_activation; then")
    cancellation = main.index("return 0", confirmation)
    confirmed = main.index('mutation_confirmed="true"')
    mutation_helpers = (
        "stage_paired_env_files",
        "install_caddy_package_safely",
        "install_and_validate_caddy_config",
        "restart_core_and_require_active",
        "smoke_bot_control",
        "start_web_and_smoke",
        "activate_caddy",
        "enable_services_after_smoke",
    )
    assert summary < confirmation < cancellation < confirmed
    for helper in mutation_helpers:
        assert main.index(helper) > confirmed


def test_activation_order_and_secret_transport_are_safe() -> None:
    source = setup_source()
    main = shell_function_source("main")

    order = [
        main.index("stage_paired_env_files"),
        main.index("install_and_validate_caddy_config"),
        main.index("restart_core_and_require_active"),
        main.index("smoke_bot_control"),
        main.index("start_web_and_smoke"),
        main.index("activate_caddy"),
        main.index("public_https_smoke"),
        main.index("enable_services_after_smoke"),
    ]
    assert order == sorted(order)
    smoke = shell_function_source("smoke_bot_control")
    assert "Authorization" in smoke
    assert "DISCORD_BOT_CONTROL_SHARED_SECRET" in smoke
    assert "curl -H" not in source
    assert "--header" not in source
    assert "Bearer ${" not in source
    assert '"${bot_control_secret}"' not in smoke
    assert "response.read" not in smoke
    assert "sleep " not in main


def test_setup_documents_partial_failure_without_claiming_rollback() -> None:
    cleanup_function = shell_function_source("cleanup")

    assert "activation failed after confirmation and may be partial" in cleanup_function
    assert "only one protected env file was replaced" in cleanup_function
    assert "rollback" not in cleanup_function.lower()
