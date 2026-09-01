import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MANAGER_SCRIPT = REPOSITORY_ROOT / "scripts/manager.sh"
BASH = shutil.which("bash")
RUN_WINDOWS_BASH_TESTS = os.environ.get("KANAMI_TEST_WINDOWS_BASH") == "1"
SECRET_TOKEN = "doctor-must-not-print-this-token"
SECRET_DATABASE_URL = "postgresql+asyncpg://secret-user:secret-password@db/kanami"

pytestmark = pytest.mark.skipif(
    (sys.platform == "win32" and not RUN_WINDOWS_BASH_TESTS) or BASH is None,
    reason="Kanami Manager requires Bash on a Unix-like platform",
)


def run_manager(
    *arguments: str,
    environment: dict[str, str] | None = None,
    input_text: str | None = None,
    script: Path = MANAGER_SCRIPT,
) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    process_environment = os.environ.copy()
    if environment is not None:
        process_environment.update(environment)
    return subprocess.run(
        [BASH, str(script), *arguments],
        cwd=REPOSITORY_ROOT,
        env=process_environment,
        check=False,
        capture_output=True,
        input=input_text,
        text=True,
        timeout=10,
    )


def run_git(checkout: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def write_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def shell_function_source(name: str) -> str:
    source = MANAGER_SCRIPT.read_text(encoding="utf-8")
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{\n(?P<body>.*?)^\}}$",
        source,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None
    return match.group(0)


def create_logs_test_manager(
    tmp_path: Path,
    *,
    journalctl_exit_code: int = 0,
) -> tuple[Path, dict[str, str], Path]:
    fake_journalctl = tmp_path / "bin/journalctl"
    arguments_file = tmp_path / "journalctl-arguments.txt"
    fake_journalctl.parent.mkdir(parents=True)
    fake_journalctl.write_text(
        """#!/usr/bin/env bash
set -u
printf '%s\n' "$@" > "${KANAMI_TEST_JOURNAL_ARGUMENTS_FILE:?}"
printf 'fake journal line\n'
exit "${KANAMI_TEST_JOURNAL_EXIT_CODE:-0}"
""",
        encoding="utf-8",
    )
    fake_journalctl.chmod(0o755)

    source = MANAGER_SCRIPT.read_text(encoding="utf-8")
    production_boundary = 'readonly JOURNALCTL="/usr/bin/journalctl"'
    assert source.count(production_boundary) == 1
    test_manager = tmp_path / "kanami"
    test_manager.write_text(
        source.replace(
            production_boundary,
            f'readonly JOURNALCTL="{fake_journalctl.as_posix()}"',
        ),
        encoding="utf-8",
    )
    test_manager.chmod(0o755)
    environment = {
        "KANAMI_TEST_JOURNAL_ARGUMENTS_FILE": arguments_file.as_posix(),
        "KANAMI_TEST_JOURNAL_EXIT_CODE": str(journalctl_exit_code),
    }
    return test_manager, environment, arguments_file


def create_lifecycle_test_manager(
    tmp_path: Path,
    *,
    mode: str,
    load_state: str = "loaded",
    show_exit_code: int = 0,
    initial_active_state: str = "inactive",
    post_start_state: str = "active",
    start_exit_code: int = 0,
    post_stop_state: str = "inactive",
    stop_exit_code: int = 0,
) -> tuple[Path, dict[str, str], Path]:
    fake_systemctl = tmp_path / "bin/systemctl"
    command_log = tmp_path / "systemctl-commands.txt"
    action_marker = tmp_path / "systemctl-action-completed"
    fake_systemctl.parent.mkdir(parents=True)
    fake_systemctl.write_text(
        """#!/usr/bin/env bash
set -u
printf '%s\n' "$*" >> "${KANAMI_TEST_SYSTEMCTL_COMMAND_LOG:?}"

case "${1:-}" in
    show)
        if ((KANAMI_TEST_SHOW_EXIT_CODE != 0)); then
            exit "${KANAMI_TEST_SHOW_EXIT_CODE}"
        fi
        printf '%s\n' "${KANAMI_TEST_LOAD_STATE}"
        ;;
    is-active)
        quiet=false
        if [[ ${2:-} == "--quiet" ]]; then
            quiet=true
        fi
        if [[ ${KANAMI_TEST_LIFECYCLE_MODE} == "start" ]]; then
            if [[ -f ${KANAMI_TEST_ACTION_MARKER} ]]; then
                state="${KANAMI_TEST_POST_START_STATE}"
            else
                state="${KANAMI_TEST_INITIAL_ACTIVE_STATE}"
            fi
        else
            state="${KANAMI_TEST_POST_STOP_STATE}"
        fi
        if [[ ${quiet} == "false" ]]; then
            printf '%s\n' "${state}"
        fi
        [[ ${state} == "active" ]]
        ;;
    start)
        touch "${KANAMI_TEST_ACTION_MARKER}"
        exit "${KANAMI_TEST_START_EXIT_CODE}"
        ;;
    stop)
        touch "${KANAMI_TEST_ACTION_MARKER}"
        exit "${KANAMI_TEST_STOP_EXIT_CODE}"
        ;;
    *)
        exit 64
        ;;
esac
""",
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o755)

    source = MANAGER_SCRIPT.read_text(encoding="utf-8")
    systemctl_boundary = 'readonly MUTATING_SYSTEMCTL="/usr/bin/systemctl"'
    root_boundary = "if [[ ${EUID} -ne 0 ]]; then"
    assert source.count(systemctl_boundary) == 1
    assert source.count(root_boundary) == 1
    test_manager = tmp_path / "kanami"
    test_manager.write_text(
        source.replace(
            systemctl_boundary,
            f'readonly MUTATING_SYSTEMCTL="{fake_systemctl.as_posix()}"',
        ).replace(root_boundary, "if false; then"),
        encoding="utf-8",
    )
    test_manager.chmod(0o755)
    environment = {
        "KANAMI_TEST_SYSTEMCTL_COMMAND_LOG": command_log.as_posix(),
        "KANAMI_TEST_ACTION_MARKER": action_marker.as_posix(),
        "KANAMI_TEST_LIFECYCLE_MODE": mode,
        "KANAMI_TEST_LOAD_STATE": load_state,
        "KANAMI_TEST_SHOW_EXIT_CODE": str(show_exit_code),
        "KANAMI_TEST_INITIAL_ACTIVE_STATE": initial_active_state,
        "KANAMI_TEST_POST_START_STATE": post_start_state,
        "KANAMI_TEST_START_EXIT_CODE": str(start_exit_code),
        "KANAMI_TEST_POST_STOP_STATE": post_stop_state,
        "KANAMI_TEST_STOP_EXIT_CODE": str(stop_exit_code),
    }
    return test_manager, environment, command_log


def create_update_test_manager(
    tmp_path: Path,
    *,
    updater_exit_code: int = 0,
) -> tuple[Path, dict[str, str], Path, Path, Path]:
    checkout = tmp_path / "opt/kanami"
    scripts_dir = checkout / "scripts"
    updater = scripts_dir / "update.sh"
    scripts_dir.mkdir(parents=True)
    updater.write_text("# trusted updater fixture\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bash = fake_bin / "bash"
    fake_stat = fake_bin / "stat"
    invocation_log = tmp_path / "update-invocation.txt"
    stat_log = tmp_path / "update-stat.txt"
    fake_bin.mkdir()
    fake_bash.write_text(
        """#!/usr/bin/env bash
set -u
printf '%s\n' "$@" > "${KANAMI_TEST_UPDATE_INVOCATION_LOG:?}"
printf 'fake updater stdout\n'
printf 'fake updater stderr\n' >&2
exit "${KANAMI_TEST_UPDATE_EXIT_CODE:?}"
""",
        encoding="utf-8",
    )
    fake_stat.write_text(
        """#!/usr/bin/env bash
set -u
path="${!#}"
printf '%s\n' "${path}" >> "${KANAMI_TEST_UPDATE_STAT_LOG:?}"
case "${path}" in
    "${KANAMI_TEST_UPDATE_CHECKOUT:?}")
        printf '%s %s %s\n' \
            "${KANAMI_TEST_CHECKOUT_UID:-0}" \
            "${KANAMI_TEST_CHECKOUT_GID:-0}" \
            "${KANAMI_TEST_CHECKOUT_MODE:-755}"
        ;;
    "${KANAMI_TEST_UPDATE_SCRIPTS_DIR:?}")
        printf '%s %s %s\n' \
            "${KANAMI_TEST_SCRIPTS_UID:-0}" \
            "${KANAMI_TEST_SCRIPTS_GID:-0}" \
            "${KANAMI_TEST_SCRIPTS_MODE:-755}"
        ;;
    "${KANAMI_TEST_UPDATE_SCRIPT:?}")
        printf '%s %s %s\n' \
            "${KANAMI_TEST_UPDATER_UID:-0}" \
            "${KANAMI_TEST_UPDATER_GID:-0}" \
            "${KANAMI_TEST_UPDATER_MODE:-644}"
        ;;
    *)
        exit 64
        ;;
esac
""",
        encoding="utf-8",
    )
    fake_bash.chmod(0o755)
    fake_stat.chmod(0o755)

    source = MANAGER_SCRIPT.read_text(encoding="utf-8")
    replacements = {
        'readonly UPDATE_CHECKOUT="/opt/kanami"': (
            f'readonly UPDATE_CHECKOUT="{checkout.as_posix()}"'
        ),
        'readonly UPDATE_SCRIPTS_DIR="/opt/kanami/scripts"': (
            f'readonly UPDATE_SCRIPTS_DIR="{scripts_dir.as_posix()}"'
        ),
        'readonly UPDATE_SCRIPT="/opt/kanami/scripts/update.sh"': (
            f'readonly UPDATE_SCRIPT="{updater.as_posix()}"'
        ),
        'readonly UPDATE_BASH="/usr/bin/bash"': (
            f'readonly UPDATE_BASH="{fake_bash.as_posix()}"'
        ),
        'readonly UPDATE_STAT="/usr/bin/stat"': (
            f'readonly UPDATE_STAT="{fake_stat.as_posix()}"'
        ),
        "if (( EUID != 0 )); then": "if false; then",
    }
    for production_value, test_value in replacements.items():
        assert source.count(production_value) == 1
        source = source.replace(production_value, test_value)

    test_manager = tmp_path / "kanami"
    test_manager.write_text(source, encoding="utf-8")
    test_manager.chmod(0o755)
    environment = {
        "KANAMI_TEST_UPDATE_INVOCATION_LOG": invocation_log.as_posix(),
        "KANAMI_TEST_UPDATE_STAT_LOG": stat_log.as_posix(),
        "KANAMI_TEST_UPDATE_EXIT_CODE": str(updater_exit_code),
        "KANAMI_TEST_UPDATE_CHECKOUT": checkout.as_posix(),
        "KANAMI_TEST_UPDATE_SCRIPTS_DIR": scripts_dir.as_posix(),
        "KANAMI_TEST_UPDATE_SCRIPT": updater.as_posix(),
    }
    return test_manager, environment, invocation_log, checkout, updater


def create_checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "kanami-test"\nversion = "0"\n',
        encoding="utf-8",
    )
    (checkout / "kanami.env").write_text(
        f"DISCORD_TOKEN={SECRET_TOKEN}\nDATABASE_URL={SECRET_DATABASE_URL}\n",
        encoding="utf-8",
    )
    write_executable(checkout / ".venv/bin/discord-stats-bot")

    run_git(checkout, "init", "--initial-branch=main")
    run_git(checkout, "config", "user.name", "Kanami Tests")
    run_git(checkout, "config", "user.email", "kanami-tests@example.invalid")
    run_git(checkout, "add", ".")
    run_git(checkout, "commit", "-m", "test fixture")
    run_git(
        checkout,
        "remote",
        "add",
        "origin",
        "https://example.invalid/kanami.git",
    )
    return checkout


def create_fake_systemctl(tmp_path: Path) -> Path:
    fake_systemctl = tmp_path / "bin/systemctl"
    fake_systemctl.parent.mkdir(parents=True, exist_ok=True)
    fake_systemctl.write_text(
        """#!/usr/bin/env bash
set -u

case "${1:-}:${2:-}" in
    show:kanami.service)
        printf '%s\n' "${KANAMI_TEST_BOT_LOAD_STATE:-loaded}"
        ;;
    show:kanami-web-admin.service)
        printf '%s\n' "${KANAMI_TEST_WEB_LOAD_STATE:-not-found}"
        ;;
    is-active:kanami.service)
        state="${KANAMI_TEST_BOT_ACTIVE_STATE:-active}"
        printf '%s\n' "${state}"
        [[ ${state} == active ]]
        ;;
    is-active:kanami-web-admin.service)
        state="${KANAMI_TEST_WEB_ACTIVE_STATE:-inactive}"
        printf '%s\n' "${state}"
        [[ ${state} == active ]]
        ;;
    *)
        exit 1
        ;;
esac
""",
        encoding="utf-8",
    )
    fake_systemctl.chmod(0o755)
    return fake_systemctl


def create_manager_environment(
    tmp_path: Path,
    checkout: Path,
    *,
    bot_load_state: str = "loaded",
    bot_active_state: str = "active",
    web_load_state: str = "not-found",
    web_active_state: str = "inactive",
) -> dict[str, str]:
    uv_bootstrap = tmp_path / "kanami-uv"
    uv_cache = tmp_path / "uv-cache"
    write_executable(uv_bootstrap / "bin/uv")
    uv_cache.mkdir()
    fake_systemctl = create_fake_systemctl(tmp_path)
    return {
        "PATH": str(fake_systemctl.parent) + os.pathsep + os.environ.get("PATH", ""),
        "KANAMI_MANAGER_INSTALL_DIR": str(checkout),
        "KANAMI_MANAGER_UV_BOOTSTRAP_DIR": str(uv_bootstrap),
        "KANAMI_MANAGER_UV_CACHE_DIR": str(uv_cache),
        "KANAMI_TEST_BOT_LOAD_STATE": bot_load_state,
        "KANAMI_TEST_BOT_ACTIVE_STATE": bot_active_state,
        "KANAMI_TEST_WEB_LOAD_STATE": web_load_state,
        "KANAMI_TEST_WEB_ACTIVE_STATE": web_active_state,
        "DISCORD_TOKEN": SECRET_TOKEN,
        "DATABASE_URL": SECRET_DATABASE_URL,
    }


@pytest.fixture
def healthy_installation(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    checkout = create_checkout(tmp_path)
    environment = create_manager_environment(tmp_path, checkout)
    return checkout, environment


def test_no_arguments_in_non_tty_shows_help_without_blocking() -> None:
    result = run_manager(input_text="")

    assert result.returncode == 0
    assert "Kanami Manager" in result.stdout
    assert "Usage: kanami [command]" in result.stdout
    assert "Select an option" not in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize("argument", ["help", "--help", "-h"])
def test_help_aliases_show_the_same_help(argument: str) -> None:
    result = run_manager(argument)
    default_result = run_manager()

    assert result.returncode == 0
    assert result.stdout == default_result.stdout
    assert "status" in result.stdout
    assert "doctor" in result.stdout
    assert "logs       Show recent Kanami bot logs" in result.stdout
    assert "restart" in result.stdout
    assert "start      Start the main Kanami bot service" in result.stdout
    assert "stop       Stop the main Kanami bot service" in result.stdout
    assert "update     Run the trusted production updater" in result.stdout
    assert "menu" in result.stdout
    assert result.stderr == ""


def test_version_shows_manager_name_and_optional_git_commit() -> None:
    result = run_manager("version")

    assert result.returncode == 0
    assert result.stderr == ""
    assert re.fullmatch(
        r"Kanami Manager\n(?:Git commit: [0-9a-f]+\n)?",
        result.stdout,
    )


def test_menu_preserves_existing_numbering_adds_update_and_exits() -> None:
    result = run_manager("menu", input_text="0\n")

    assert result.returncode == 0
    assert "1. Status" in result.stdout
    assert "2. Doctor" in result.stdout
    assert "3. Version" in result.stdout
    assert "4. Help" in result.stdout
    assert "5. Restart bot" in result.stdout
    assert "6. Logs" in result.stdout
    assert "7. Start bot" in result.stdout
    assert "8. Stop bot" in result.stdout
    assert "9. Update" in result.stdout
    assert "0. Exit" in result.stdout
    assert "Select an option [0-9]:" in result.stdout
    assert "Install" not in result.stdout
    assert "Backup" not in result.stdout
    assert "Restore" not in result.stdout
    assert "Goodbye." in result.stdout


def test_menu_status_uses_existing_status_logic(
    healthy_installation: tuple[Path, dict[str, str]],
) -> None:
    checkout, environment = healthy_installation

    result = run_manager("menu", environment=environment, input_text="1\n0\n")

    assert result.returncode == 0
    assert "Kanami Manager status" in result.stdout
    assert f"Checkout path: {checkout}" in result.stdout
    assert "kanami.service: active" in result.stdout
    assert "Goodbye." in result.stdout


def test_menu_doctor_failure_returns_to_menu_and_can_exit(
    healthy_installation: tuple[Path, dict[str, str]],
) -> None:
    _, environment = healthy_installation
    environment["KANAMI_TEST_BOT_ACTIVE_STATE"] = "inactive"

    result = run_manager("menu", environment=environment, input_text="2\n0\n")

    assert result.returncode == 0
    assert "Kanami Manager doctor" in result.stdout
    assert "Overall: UNHEALTHY" in result.stdout
    assert result.stdout.count("1. Status") == 2
    assert "Goodbye." in result.stdout


def test_menu_version_uses_existing_version_logic() -> None:
    result = run_manager("menu", input_text="3\n0\n")

    assert result.returncode == 0
    assert "Git commit:" in result.stdout
    assert result.stdout.count("Kanami Manager") == 3
    assert "Goodbye." in result.stdout


def test_menu_help_uses_existing_help_logic() -> None:
    result = run_manager("menu", input_text="4\n0\n")

    assert result.returncode == 0
    assert "Usage: kanami [command]" in result.stdout
    assert "restart    Restart the main Kanami bot service" in result.stdout
    assert "menu       Open the interactive menu" in result.stdout
    assert "Goodbye." in result.stdout


def test_menu_invalid_choice_returns_to_menu() -> None:
    result = run_manager("menu", input_text="invalid\n0\n")

    assert result.returncode == 0
    assert "Invalid choice: invalid. Select a number from 0 to 9." in result.stdout
    assert result.stdout.count("1. Status") == 2
    assert "Goodbye." in result.stdout


def test_menu_eof_exits_successfully() -> None:
    result = run_manager("menu", input_text="")

    assert result.returncode == 0
    assert "1. Status" in result.stdout
    assert "End of input; exiting." in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize("answer", ["", "n", "N", "invalid"])
def test_menu_restart_requires_positive_confirmation(answer: str) -> None:
    result = run_manager("menu", input_text=f"5\n{answer}\n0\n")

    assert result.returncode == 0
    assert "Restart kanami.service? [y/N]:" in result.stdout
    assert "Restart cancelled." in result.stdout
    assert result.stdout.count("1. Status") == 2
    assert "restarted successfully" not in result.stdout
    assert "Goodbye." in result.stdout


def test_menu_restart_confirmation_eof_is_safe() -> None:
    result = run_manager("menu", input_text="5\n")

    assert result.returncode == 0
    assert "Restart cancelled." in result.stdout
    assert result.stdout.count("1. Status") == 2
    assert "End of input; exiting." in result.stdout
    assert "restarted successfully" not in result.stdout


def test_menu_restart_failure_returns_to_menu() -> None:
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        pytest.skip("behavioral root-boundary check requires a non-root host")

    result = run_manager("menu", input_text="5\ny\n0\n")

    assert result.returncode == 0
    assert "restart requires root" in result.stderr
    assert "Restart failed." in result.stdout
    assert result.stdout.count("1. Status") == 2
    assert "Goodbye." in result.stdout


def test_direct_restart_rejects_non_root_without_mutation() -> None:
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        pytest.skip("behavioral root-boundary check requires a non-root host")

    result = run_manager("restart")

    assert result.returncode != 0
    assert result.stdout == ""
    assert "restart requires root" in result.stderr
    assert "sudo kanami restart" in result.stderr


def test_restart_uses_fixed_production_boundary_and_bot_only() -> None:
    source = MANAGER_SCRIPT.read_text(encoding="utf-8")
    restart_source = shell_function_source("restart_bot")

    assert 'readonly MUTATING_SYSTEMCTL="/usr/bin/systemctl"' in source
    assert 'readonly BOT_SERVICE="kanami.service"' in source
    assert "kanami-web-admin.service" not in restart_source
    assert "KANAMI_MANAGER_INSTALL_DIR" not in restart_source
    assert "KANAMI_MANAGER_UV_BOOTSTRAP_DIR" not in restart_source
    assert "KANAMI_MANAGER_UV_CACHE_DIR" not in restart_source
    assert "KANAMI_MANAGER_SYSTEMCTL" not in source
    assert 'MUTATING_SYSTEMCTL="${' not in source


def test_restart_enforces_root_without_privilege_escalation() -> None:
    restart_source = shell_function_source("restart_bot")

    assert "((EUID != 0))" in restart_source
    assert not re.search(r"^\s*(?:sudo|su)\b", restart_source, re.MULTILINE)


def test_menu_restart_only_runs_after_explicit_positive_confirmation() -> None:
    confirmation_source = shell_function_source("confirm_restart")
    menu_source = shell_function_source("show_menu")

    assert "y | Y | yes | YES)" in confirmation_source
    confirmation = menu_source.index("if confirm_restart; then")
    guarded_restart = menu_source.index("if ! restart_bot; then")
    assert confirmation < guarded_restart


def test_logs_uses_fixed_production_boundary_and_bot_only() -> None:
    source = MANAGER_SCRIPT.read_text(encoding="utf-8")
    logs_source = shell_function_source("show_logs")

    assert 'readonly JOURNALCTL="/usr/bin/journalctl"' in source
    assert "KANAMI_MANAGER_JOURNALCTL" not in source
    assert 'JOURNALCTL="${' not in source
    assert 'readonly BOT_SERVICE="kanami.service"' in source
    assert '"${JOURNALCTL}" -u "${BOT_SERVICE}"' in logs_source
    assert "kanami-web-admin.service" not in logs_source


def test_logs_defaults_to_100_lines_without_pager(tmp_path: Path) -> None:
    manager, environment, arguments_file = create_logs_test_manager(tmp_path)

    result = run_manager("logs", environment=environment, script=manager)

    assert result.returncode == 0
    assert result.stdout == "fake journal line\n"
    assert result.stderr == ""
    assert arguments_file.read_text(encoding="utf-8").splitlines() == [
        "-u",
        "kanami.service",
        "-n",
        "100",
        "--no-pager",
    ]


@pytest.mark.parametrize("lines", ["1", "50", "1000"])
def test_logs_accepts_valid_line_limits(tmp_path: Path, lines: str) -> None:
    manager, environment, arguments_file = create_logs_test_manager(tmp_path)

    result = run_manager(
        "logs",
        "--lines",
        lines,
        environment=environment,
        script=manager,
    )

    assert result.returncode == 0
    arguments = arguments_file.read_text(encoding="utf-8").splitlines()
    assert arguments == ["-u", "kanami.service", "-n", lines, "--no-pager"]


@pytest.mark.parametrize(
    "arguments",
    [
        ("--lines", "0"),
        ("--lines", "1001"),
        ("--lines", "999999999999999999999999"),
        ("--lines", "abc"),
        ("--lines",),
        ("unexpected",),
        ("some.service",),
        ("--lines", "50", "unexpected"),
        ("--follow",),
        ("-f",),
    ],
)
def test_logs_rejects_invalid_or_unsupported_arguments(
    arguments: tuple[str, ...],
) -> None:
    result = run_manager("logs", *arguments)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "Usage: kanami logs [--lines N]" in result.stderr


def test_logs_propagates_journalctl_exit_code(tmp_path: Path) -> None:
    manager, environment, _ = create_logs_test_manager(
        tmp_path,
        journalctl_exit_code=7,
    )

    result = run_manager("logs", environment=environment, script=manager)

    assert result.returncode == 7
    assert "fake journal line" in result.stdout


def test_menu_logs_failure_returns_to_menu(tmp_path: Path) -> None:
    manager, environment, arguments_file = create_logs_test_manager(
        tmp_path,
        journalctl_exit_code=7,
    )

    result = run_manager(
        "menu",
        environment=environment,
        input_text="6\n0\n",
        script=manager,
    )

    assert result.returncode == 0
    assert arguments_file.read_text(encoding="utf-8").splitlines() == [
        "-u",
        "kanami.service",
        "-n",
        "100",
        "--no-pager",
    ]
    assert "Unable to show logs." in result.stderr
    assert result.stdout.count("1. Status") == 2
    assert "Goodbye." in result.stdout


def test_logs_does_not_escalate_or_access_other_subsystems() -> None:
    logs_source = shell_function_source("show_logs")

    assert "eval" not in logs_source
    assert not re.search(r"^\s*(?:sudo|su)\b", logs_source, re.MULTILINE)
    for forbidden in (
        "CONFIG_FILE",
        "kanami.env",
        "DISCORD_TOKEN",
        "DATABASE_URL",
        "git ",
        "uv ",
        "alembic",
        "systemctl",
        "restart",
        "start",
        "stop",
    ):
        assert forbidden not in logs_source


@pytest.mark.parametrize("action", ["start", "stop"])
def test_start_and_stop_reject_positional_arguments(action: str) -> None:
    result = run_manager(action, "anything")

    assert result.returncode == 2
    assert result.stdout == ""
    assert f"Usage: sudo kanami {action}" in result.stderr


@pytest.mark.parametrize("action", ["start", "stop"])
def test_start_and_stop_reject_non_root_without_mutation(action: str) -> None:
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        pytest.skip("behavioral root-boundary check requires a non-root host")

    result = run_manager(action)

    assert result.returncode != 0
    assert result.stdout == ""
    assert f"{action} requires root" in result.stderr
    assert f"sudo kanami {action}" in result.stderr


def test_start_stop_use_fixed_bot_only_production_boundary() -> None:
    source = MANAGER_SCRIPT.read_text(encoding="utf-8")
    validation_source = shell_function_source("validate_bot_lifecycle_action")
    start_source = shell_function_source("start_bot")
    stop_source = shell_function_source("stop_bot")
    lifecycle_source = validation_source + start_source + stop_source

    assert 'readonly MUTATING_SYSTEMCTL="/usr/bin/systemctl"' in source
    assert 'readonly BOT_SERVICE="kanami.service"' in source
    assert "KANAMI_MANAGER_SYSTEMCTL" not in source
    assert 'MUTATING_SYSTEMCTL="${' not in source
    assert "kanami-web-admin.service" not in lifecycle_source
    assert "KANAMI_MANAGER_INSTALL_DIR" not in lifecycle_source
    assert "KANAMI_MANAGER_UV_BOOTSTRAP_DIR" not in lifecycle_source
    assert "KANAMI_MANAGER_UV_CACHE_DIR" not in lifecycle_source
    assert validation_source.index("EUID") < validation_source.index(
        '"${MUTATING_SYSTEMCTL}" show'
    )


@pytest.mark.parametrize(
    ("action", "load_state"),
    [
        ("start", "not-found"),
        ("start", "masked"),
        ("start", "error"),
        ("start", "bad-setting"),
        ("start", "unknown"),
        ("start", ""),
        ("stop", "not-found"),
        ("stop", "masked"),
        ("stop", "error"),
        ("stop", "bad-setting"),
        ("stop", "unknown"),
        ("stop", ""),
    ],
)
def test_start_stop_reject_abnormal_load_states(
    tmp_path: Path,
    action: str,
    load_state: str,
) -> None:
    manager, environment, command_log = create_lifecycle_test_manager(
        tmp_path,
        mode=action,
        load_state=load_state,
    )

    result = run_manager(action, environment=environment, script=manager)

    assert result.returncode != 0
    assert "successfully" not in result.stdout
    commands = command_log.read_text(encoding="utf-8").splitlines()
    assert commands == ["show kanami.service --property=LoadState --value"]


@pytest.mark.parametrize("action", ["start", "stop"])
def test_start_stop_fail_when_load_state_cannot_be_read(
    tmp_path: Path,
    action: str,
) -> None:
    manager, environment, command_log = create_lifecycle_test_manager(
        tmp_path,
        mode=action,
        show_exit_code=5,
    )

    result = run_manager(action, environment=environment, script=manager)

    assert result.returncode != 0
    assert "cannot verify kanami.service load state" in result.stderr
    assert command_log.read_text(encoding="utf-8").splitlines() == [
        "show kanami.service --property=LoadState --value"
    ]


def test_start_is_noop_when_service_is_already_active(tmp_path: Path) -> None:
    manager, environment, command_log = create_lifecycle_test_manager(
        tmp_path,
        mode="start",
        initial_active_state="active",
    )

    result = run_manager("start", environment=environment, script=manager)

    assert result.returncode == 0
    assert "kanami.service is already active." in result.stdout
    assert command_log.read_text(encoding="utf-8").splitlines() == [
        "show kanami.service --property=LoadState --value",
        "is-active --quiet kanami.service",
    ]


def test_start_inactive_service_and_verify_active_state(tmp_path: Path) -> None:
    manager, environment, command_log = create_lifecycle_test_manager(
        tmp_path,
        mode="start",
    )

    result = run_manager("start", environment=environment, script=manager)

    assert result.returncode == 0
    assert "kanami.service started successfully." in result.stdout
    assert command_log.read_text(encoding="utf-8").splitlines() == [
        "show kanami.service --property=LoadState --value",
        "is-active --quiet kanami.service",
        "start kanami.service",
        "is-active --quiet kanami.service",
    ]


def test_start_command_failure_is_not_reported_as_success(tmp_path: Path) -> None:
    manager, environment, command_log = create_lifecycle_test_manager(
        tmp_path,
        mode="start",
        start_exit_code=5,
    )

    result = run_manager("start", environment=environment, script=manager)

    assert result.returncode != 0
    assert "failed to start kanami.service" in result.stderr
    assert "successfully" not in result.stdout
    assert command_log.read_text(encoding="utf-8").splitlines()[-1] == (
        "start kanami.service"
    )


def test_start_post_check_failure_is_not_reported_as_success(
    tmp_path: Path,
) -> None:
    manager, environment, command_log = create_lifecycle_test_manager(
        tmp_path,
        mode="start",
        post_start_state="failed",
    )

    result = run_manager("start", environment=environment, script=manager)

    assert result.returncode != 0
    assert "not active after start" in result.stderr
    assert "successfully" not in result.stdout
    assert command_log.read_text(encoding="utf-8").splitlines()[-1] == (
        "is-active --quiet kanami.service"
    )


def test_stop_is_idempotent_and_confirms_inactive_state(tmp_path: Path) -> None:
    manager, environment, command_log = create_lifecycle_test_manager(
        tmp_path,
        mode="stop",
        post_stop_state="inactive",
    )

    result = run_manager("stop", environment=environment, script=manager)

    assert result.returncode == 0
    assert "kanami.service stopped successfully." in result.stdout
    assert "Stop kanami.service?" not in result.stdout
    assert command_log.read_text(encoding="utf-8").splitlines() == [
        "show kanami.service --property=LoadState --value",
        "stop kanami.service",
        "is-active kanami.service",
    ]


@pytest.mark.parametrize(
    "post_stop_state",
    ["failed", "active", "activating", "deactivating", "unknown", ""],
)
def test_stop_rejects_unconfirmed_final_state(
    tmp_path: Path,
    post_stop_state: str,
) -> None:
    manager, environment, command_log = create_lifecycle_test_manager(
        tmp_path,
        mode="stop",
        post_stop_state=post_stop_state,
    )

    result = run_manager("stop", environment=environment, script=manager)

    assert result.returncode != 0
    assert "expected inactive" in result.stderr
    assert "successfully" not in result.stdout
    assert command_log.read_text(encoding="utf-8").splitlines()[-1] == (
        "is-active kanami.service"
    )


def test_stop_command_failure_is_not_reported_as_success(tmp_path: Path) -> None:
    manager, environment, command_log = create_lifecycle_test_manager(
        tmp_path,
        mode="stop",
        stop_exit_code=5,
    )

    result = run_manager("stop", environment=environment, script=manager)

    assert result.returncode != 0
    assert "failed to stop kanami.service" in result.stderr
    assert "successfully" not in result.stdout
    assert command_log.read_text(encoding="utf-8").splitlines()[-1] == (
        "stop kanami.service"
    )


def test_menu_start_failure_returns_to_menu(tmp_path: Path) -> None:
    manager, environment, _ = create_lifecycle_test_manager(
        tmp_path,
        mode="start",
        start_exit_code=5,
    )

    result = run_manager(
        "menu",
        environment=environment,
        input_text="7\n0\n",
        script=manager,
    )

    assert result.returncode == 0
    assert "Start failed." in result.stdout
    assert result.stdout.count("1. Status") == 2
    assert "Goodbye." in result.stdout


@pytest.mark.parametrize("answer", ["", "n", "N", "invalid"])
def test_menu_stop_requires_positive_confirmation(answer: str) -> None:
    result = run_manager("menu", input_text=f"8\n{answer}\n0\n")

    assert result.returncode == 0
    assert "Stop kanami.service? [y/N]:" in result.stdout
    assert "Stop cancelled." in result.stdout
    assert result.stdout.count("1. Status") == 2
    assert "stopped successfully" not in result.stdout
    assert "Goodbye." in result.stdout


def test_menu_stop_confirmation_eof_is_safe() -> None:
    result = run_manager("menu", input_text="8\n")

    assert result.returncode == 0
    assert "Stop cancelled." in result.stdout
    assert result.stdout.count("1. Status") == 2
    assert "End of input; exiting." in result.stdout
    assert "stopped successfully" not in result.stdout


@pytest.mark.parametrize("answer", ["y", "Y", "yes", "YES"])
def test_menu_stop_accepts_positive_confirmation(
    tmp_path: Path,
    answer: str,
) -> None:
    manager, environment, command_log = create_lifecycle_test_manager(
        tmp_path,
        mode="stop",
    )

    result = run_manager(
        "menu",
        environment=environment,
        input_text=f"8\n{answer}\n0\n",
        script=manager,
    )

    assert result.returncode == 0
    assert "kanami.service stopped successfully." in result.stdout
    assert "stop kanami.service" in command_log.read_text(encoding="utf-8")
    assert result.stdout.count("1. Status") == 2
    assert "Goodbye." in result.stdout


def test_menu_stop_failure_returns_to_menu(tmp_path: Path) -> None:
    manager, environment, _ = create_lifecycle_test_manager(
        tmp_path,
        mode="stop",
        post_stop_state="failed",
    )

    result = run_manager(
        "menu",
        environment=environment,
        input_text="8\ny\n0\n",
        script=manager,
    )

    assert result.returncode == 0
    assert "Stop failed." in result.stdout
    assert result.stdout.count("1. Status") == 2
    assert "Goodbye." in result.stdout


def test_start_stop_do_not_escalate_or_access_other_subsystems() -> None:
    lifecycle_source = "\n".join(
        (
            shell_function_source("validate_bot_lifecycle_action"),
            shell_function_source("start_bot"),
            shell_function_source("stop_bot"),
        )
    )

    assert not re.search(r"^\s*(?:sudo|su)\b", lifecycle_source, re.MULTILINE)
    for forbidden in (
        "CONFIG_FILE",
        "kanami.env",
        "DISCORD_TOKEN",
        "DATABASE_URL",
        "git ",
        "uv ",
        "alembic",
        "journalctl",
        "daemon-reload",
        '"${MUTATING_SYSTEMCTL}" restart',
        '"${MUTATING_SYSTEMCTL}" enable',
        '"${MUTATING_SYSTEMCTL}" disable',
        '"${MUTATING_SYSTEMCTL}" mask',
        '"${MUTATING_SYSTEMCTL}" unmask',
        '"${MUTATING_SYSTEMCTL}" reset-failed',
    ):
        assert forbidden not in lifecycle_source


def test_update_rejects_positional_arguments_before_preflight() -> None:
    result = run_manager("update", "unexpected")

    assert result.returncode == 2
    assert result.stdout == ""
    assert "update does not accept arguments" in result.stderr
    assert "Usage: sudo kanami update" in result.stderr


def test_update_rejects_non_root_before_trust_checks() -> None:
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        pytest.skip("behavioral root-boundary check requires a non-root host")

    result = run_manager("update")

    assert result.returncode != 0
    assert result.stdout == ""
    assert "update requires root; run: sudo kanami update" in result.stderr
    assert "trust check" not in result.stderr


def test_update_uses_fixed_production_bootstrap_boundary() -> None:
    source = MANAGER_SCRIPT.read_text(encoding="utf-8")
    run_source = shell_function_source("run_update")
    trust_source = "\n".join(
        (
            shell_function_source("validate_update_path_metadata"),
            shell_function_source("validate_update_directory"),
            shell_function_source("validate_update_script"),
            shell_function_source("validate_update_bootstrap_trust"),
        )
    )

    assert 'readonly UPDATE_CHECKOUT="/opt/kanami"' in source
    assert 'readonly UPDATE_SCRIPTS_DIR="/opt/kanami/scripts"' in source
    assert 'readonly UPDATE_SCRIPT="/opt/kanami/scripts/update.sh"' in source
    assert 'readonly UPDATE_BASH="/usr/bin/bash"' in source
    assert 'readonly UPDATE_STAT="/usr/bin/stat"' in source
    assert "KANAMI_MANAGER_UPDATE" not in source
    assert 'UPDATE_SCRIPT="${' not in source
    assert 'UPDATE_BASH="${' not in source
    assert 'UPDATE_STAT="${' not in source
    for read_only_seam in (
        "KANAMI_MANAGER_INSTALL_DIR",
        "KANAMI_MANAGER_UV_BOOTSTRAP_DIR",
        "KANAMI_MANAGER_UV_CACHE_DIR",
    ):
        assert read_only_seam not in run_source + trust_source


def test_update_checks_root_and_bootstrap_trust_before_fixed_bash_invocation() -> None:
    run_source = shell_function_source("run_update")
    trust_source = shell_function_source("validate_update_bootstrap_trust")
    directory_source = shell_function_source("validate_update_directory")
    updater_source = shell_function_source("validate_update_script")
    metadata_source = shell_function_source("validate_update_path_metadata")

    root_check = run_source.index("EUID")
    bash_check = run_source.index("[[ ! -x ${UPDATE_BASH} ]]")
    stat_check = run_source.index("[[ ! -x ${UPDATE_STAT} ]]")
    trust_check = run_source.index("validate_update_bootstrap_trust")
    invocation = run_source.index('"${UPDATE_BASH}" "${UPDATE_SCRIPT}"')
    assert root_check < bash_check < stat_check < trust_check < invocation
    assert trust_source.index("UPDATE_CHECKOUT") < trust_source.index(
        "UPDATE_SCRIPTS_DIR"
    )
    assert trust_source.index("UPDATE_SCRIPTS_DIR") < trust_source.index(
        "validate_update_script"
    )
    assert "[[ -L ${path} ]]" in directory_source
    assert "[[ -L ${UPDATE_SCRIPT} ]]" in updater_source
    assert "[[ ! -f ${UPDATE_SCRIPT} || ! -r ${UPDATE_SCRIPT} ]]" in updater_source
    assert "-x ${UPDATE_SCRIPT}" not in updater_source
    assert "UID 0 and GID 0" in metadata_source
    assert "8#022" in metadata_source


@pytest.mark.parametrize("updater_exit_code", [0, 7])
def test_direct_update_streams_output_and_propagates_updater_exit_code(
    tmp_path: Path,
    updater_exit_code: int,
) -> None:
    manager, environment, invocation_log, _, updater = create_update_test_manager(
        tmp_path,
        updater_exit_code=updater_exit_code,
    )

    result = run_manager("update", environment=environment, script=manager)

    assert result.returncode == updater_exit_code
    assert "fake updater stdout" in result.stdout
    assert "fake updater stderr" in result.stderr
    assert invocation_log.read_text(encoding="utf-8").splitlines() == [
        updater.as_posix()
    ]


@pytest.mark.parametrize(
    ("metadata_name", "metadata_value", "expected_label"),
    [
        ("KANAMI_TEST_CHECKOUT_UID", "1000", "production checkout"),
        ("KANAMI_TEST_CHECKOUT_MODE", "775", "production checkout"),
        ("KANAMI_TEST_SCRIPTS_GID", "1000", "scripts directory"),
        ("KANAMI_TEST_SCRIPTS_MODE", "757", "scripts directory"),
        ("KANAMI_TEST_UPDATER_UID", "1000", "updater"),
        ("KANAMI_TEST_UPDATER_GID", "1000", "updater"),
        ("KANAMI_TEST_UPDATER_MODE", "664", "updater"),
    ],
)
def test_update_rejects_untrusted_owner_group_or_mode_before_invocation(
    tmp_path: Path,
    metadata_name: str,
    metadata_value: str,
    expected_label: str,
) -> None:
    manager, environment, invocation_log, _, _ = create_update_test_manager(tmp_path)
    environment[metadata_name] = metadata_value

    result = run_manager("update", environment=environment, script=manager)

    assert result.returncode != 0
    assert f"update trust check failed: {expected_label}" in result.stderr
    assert not invocation_log.exists()


def test_update_rejects_non_regular_updater_before_invocation(tmp_path: Path) -> None:
    manager, environment, invocation_log, _, updater = create_update_test_manager(
        tmp_path
    )
    updater.unlink()
    updater.mkdir()

    result = run_manager("update", environment=environment, script=manager)

    assert result.returncode != 0
    assert "updater is missing or is not a readable regular file" in result.stderr
    assert not invocation_log.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="requires Unix symlink semantics")
@pytest.mark.parametrize("component", ["checkout", "scripts", "updater"])
def test_update_rejects_symlinked_bootstrap_chain_before_invocation(
    tmp_path: Path,
    component: str,
) -> None:
    manager, environment, invocation_log, checkout, updater = (
        create_update_test_manager(tmp_path)
    )
    scripts_dir = checkout / "scripts"
    if component == "checkout":
        real_checkout = tmp_path / "real-checkout"
        checkout.rename(real_checkout)
        checkout.symlink_to(real_checkout, target_is_directory=True)
    elif component == "scripts":
        real_scripts = checkout / "real-scripts"
        scripts_dir.rename(real_scripts)
        scripts_dir.symlink_to(real_scripts.name, target_is_directory=True)
    else:
        real_updater = updater.with_name("real-update.sh")
        updater.rename(real_updater)
        updater.symlink_to(real_updater.name)

    result = run_manager("update", environment=environment, script=manager)

    assert result.returncode != 0
    assert "must not be a symlink" in result.stderr
    assert not invocation_log.exists()


@pytest.mark.parametrize("answer", ["", "n", "N", "invalid"])
def test_menu_update_cancellation_returns_to_menu(
    tmp_path: Path,
    answer: str,
) -> None:
    manager, environment, invocation_log, _, _ = create_update_test_manager(tmp_path)

    result = run_manager(
        "menu",
        environment=environment,
        input_text=f"9\n{answer}\n0\n",
        script=manager,
    )

    assert result.returncode == 0
    assert "Run Kanami update? [y/N]:" in result.stdout
    assert "Update cancelled." in result.stdout
    assert result.stdout.count("1. Status") == 2
    assert "Goodbye." in result.stdout
    assert not invocation_log.exists()


def test_menu_update_confirmation_eof_cancels_and_returns_to_menu(
    tmp_path: Path,
) -> None:
    manager, environment, invocation_log, _, _ = create_update_test_manager(tmp_path)

    result = run_manager(
        "menu", environment=environment, input_text="9\n", script=manager
    )

    assert result.returncode == 0
    assert "Update cancelled." in result.stdout
    assert result.stdout.count("1. Status") == 2
    assert "End of input; exiting." in result.stdout
    assert not invocation_log.exists()


@pytest.mark.parametrize("answer", ["y", "Y", "yes", "YES"])
def test_menu_update_positive_confirmation_invokes_updater_and_exits(
    tmp_path: Path,
    answer: str,
) -> None:
    manager, environment, invocation_log, _, _ = create_update_test_manager(tmp_path)

    result = run_manager(
        "menu",
        environment=environment,
        input_text=f"9\n{answer}\n0\n",
        script=manager,
    )

    assert result.returncode == 0
    assert invocation_log.exists()
    assert result.stdout.count("1. Status") == 1
    assert "Update completed." in result.stdout
    assert "Manager may have been refreshed" in result.stdout
    assert "Run 'kanami' again" in result.stdout
    assert "Goodbye." not in result.stdout


def test_menu_update_failure_propagates_exit_and_ends_session(tmp_path: Path) -> None:
    manager, environment, invocation_log, _, _ = create_update_test_manager(
        tmp_path,
        updater_exit_code=7,
    )

    result = run_manager(
        "menu",
        environment=environment,
        input_text="9\ny\n0\n",
        script=manager,
    )

    assert result.returncode == 7
    assert invocation_log.exists()
    assert result.stdout.count("1. Status") == 1
    assert "installation may be partially updated" in result.stderr
    assert "Manager may have been refreshed" in result.stdout
    assert "rollback" not in (result.stdout + result.stderr).lower()
    assert "Goodbye." not in result.stdout


def test_no_argument_tty_menu_does_not_mask_menu_failure_status() -> None:
    main_source = shell_function_source("main")

    assert "show_menu || return $?" in main_source


def test_update_wrapper_does_not_duplicate_workflow_or_escalate() -> None:
    update_source = "\n".join(
        (
            shell_function_source("validate_update_path_metadata"),
            shell_function_source("validate_update_directory"),
            shell_function_source("validate_update_script"),
            shell_function_source("validate_update_bootstrap_trust"),
            shell_function_source("run_update"),
        )
    )

    assert not re.search(
        r"^\s*(?:sudo|su|eval|source|\.)\b", update_source, re.MULTILINE
    )
    for forbidden in (
        "git ",
        "uv ",
        "alembic",
        "DATABASE_URL",
        "kanami.env",
        "systemctl",
        "daemon-reload",
        "restart",
        "chown",
        "chmod",
        "reset --hard",
        "git clean",
    ):
        assert forbidden not in update_source


def test_restart_validates_unit_and_post_restart_active_state() -> None:
    restart_source = shell_function_source("restart_bot")

    load_check = restart_source.index("--property=LoadState --value")
    restart_call = restart_source.index(
        '"${MUTATING_SYSTEMCTL}" restart "${BOT_SERVICE}"'
    )
    active_check = restart_source.index(
        '"${MUTATING_SYSTEMCTL}" is-active --quiet "${BOT_SERVICE}"'
    )
    success_message = restart_source.index("restarted successfully")

    assert "masked | error | bad-setting" in restart_source
    assert load_check < restart_call < active_check < success_message


def test_restart_does_not_access_secrets_or_other_lifecycle_tools() -> None:
    restart_source = shell_function_source("restart_bot")

    for forbidden in (
        "CONFIG_FILE",
        "kanami.env",
        "DISCORD_TOKEN",
        "DATABASE_URL",
        "git ",
        "uv ",
        "alembic",
        "daemon-reload",
    ):
        assert forbidden not in restart_source


def test_installed_manager_copy_falls_back_to_install_checkout(
    tmp_path: Path,
) -> None:
    checkout = create_checkout(tmp_path)
    environment = create_manager_environment(tmp_path, checkout)
    installed_manager = tmp_path / "usr-local-bin/kanami"
    installed_manager.parent.mkdir()
    shutil.copyfile(MANAGER_SCRIPT, installed_manager)
    installed_manager.chmod(0o755)

    result = run_manager(
        "status",
        environment=environment,
        script=installed_manager,
    )

    assert result.returncode == 0
    assert f"Checkout path: {checkout}" in result.stdout
    assert "Git branch: main" in result.stdout

    environment.pop("KANAMI_MANAGER_INSTALL_DIR")
    default_result = run_manager(
        "status",
        environment=environment,
        script=installed_manager,
    )

    assert default_result.returncode == 0
    assert "Checkout path: /opt/kanami" in default_result.stdout
    manager_source = MANAGER_SCRIPT.read_text(encoding="utf-8")
    assert "${KANAMI_MANAGER_INSTALL_DIR:-/opt/kanami}" in manager_source


def test_foreign_adjacent_pyproject_does_not_override_install_fallback(
    tmp_path: Path,
) -> None:
    prefix = tmp_path / "fake-prefix"
    installed_manager = prefix / "bin/kanami"
    installed_manager.parent.mkdir(parents=True)
    shutil.copyfile(MANAGER_SCRIPT, installed_manager)
    installed_manager.chmod(0o755)
    (prefix / "pyproject.toml").write_text(
        '[project]\nname = "not-kanami"\n',
        encoding="utf-8",
    )

    environment = create_manager_environment(tmp_path, tmp_path / "checkout")
    environment.pop("KANAMI_MANAGER_INSTALL_DIR")
    result = run_manager(
        "status",
        environment=environment,
        script=installed_manager,
    )

    assert result.returncode == 0
    assert f"Checkout path: {prefix}" not in result.stdout
    assert "Checkout path: /opt/kanami" in result.stdout


def test_status_for_detected_checkout(
    healthy_installation: tuple[Path, dict[str, str]],
) -> None:
    checkout, environment = healthy_installation
    environment.update(
        {
            "KANAMI_TEST_WEB_LOAD_STATE": "loaded",
            "KANAMI_TEST_WEB_ACTIVE_STATE": "inactive",
        }
    )

    result = run_manager("status", environment=environment)

    assert result.returncode == 0
    assert "Checkout: found" in result.stdout
    assert f"Checkout path: {checkout}" in result.stdout
    assert re.search(r"Git commit: [0-9a-f]+", result.stdout)
    assert "Git branch: main" in result.stdout
    assert "kanami.service: active" in result.stdout
    assert "kanami-web-admin.service: inactive" in result.stdout
    assert result.stderr == ""


def test_status_when_installation_is_missing(tmp_path: Path) -> None:
    missing_checkout = tmp_path / "missing-checkout"
    environment = create_manager_environment(
        tmp_path,
        missing_checkout,
        bot_load_state="not-found",
    )

    result = run_manager("status", environment=environment)

    assert result.returncode == 0
    assert "Checkout: not found" in result.stdout
    assert f"Checkout path: {missing_checkout} (not found)" in result.stdout
    assert "Git commit: unavailable" in result.stdout
    assert "Git branch: unavailable" in result.stdout
    assert "kanami.service: not installed" in result.stdout
    assert "kanami-web-admin.service: not installed (optional)" in result.stdout
    assert result.stderr == ""


def test_doctor_reports_healthy_foundation_with_optional_web_admin_missing(
    healthy_installation: tuple[Path, dict[str, str]],
) -> None:
    _, environment = healthy_installation

    result = run_manager("doctor", environment=environment)

    assert result.returncode == 0
    assert "[OK] Checkout:" in result.stdout
    assert "[OK] Git working tree: clean" in result.stdout
    assert "[OK] Git origin: configured" in result.stdout
    assert "[OK] Bot executable:" in result.stdout
    assert "[WARN] Web Admin executable: optional component missing" in result.stdout
    assert "[OK] uv bootstrap:" in result.stdout
    assert "[OK] uv cache:" in result.stdout
    assert "[OK] kanami.service active: active" in result.stdout
    assert "[WARN] kanami-web-admin.service unit: optional unit not installed" in (
        result.stdout
    )
    assert "[SKIP] kanami-web-admin.service active:" in result.stdout
    assert "Overall: HEALTHY" in result.stdout


def test_doctor_reports_missing_checkout_as_unhealthy(tmp_path: Path) -> None:
    missing_checkout = tmp_path / "missing-checkout"
    environment = create_manager_environment(tmp_path, missing_checkout)

    result = run_manager("doctor", environment=environment)

    assert result.returncode == 1
    assert "[FAIL] Checkout: not found or inaccessible" in result.stdout
    assert "[SKIP] Git repository: checkout unavailable" in result.stdout
    assert "Overall: UNHEALTHY" in result.stdout


def test_doctor_reports_non_git_checkout_as_unhealthy(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    environment = create_manager_environment(tmp_path, checkout)

    result = run_manager("doctor", environment=environment)

    assert result.returncode == 1
    assert "[FAIL] Git repository: not a readable Git repository" in result.stdout
    assert "[SKIP] Git working tree: Git repository unavailable" in result.stdout
    assert "Overall: UNHEALTHY" in result.stdout


def test_doctor_reports_missing_origin_as_unhealthy(
    healthy_installation: tuple[Path, dict[str, str]],
) -> None:
    checkout, environment = healthy_installation
    run_git(checkout, "remote", "remove", "origin")

    result = run_manager("doctor", environment=environment)

    assert result.returncode == 1
    assert "[FAIL] Git origin: missing or inaccessible" in result.stdout
    assert "Overall: UNHEALTHY" in result.stdout


def test_doctor_reports_dirty_git_tree_as_unhealthy(
    healthy_installation: tuple[Path, dict[str, str]],
) -> None:
    checkout, environment = healthy_installation
    (checkout / "pyproject.toml").write_text("dirty\n", encoding="utf-8")

    result = run_manager("doctor", environment=environment)

    assert result.returncode == 1
    assert "[FAIL] Git working tree: local changes detected" in result.stdout
    assert "Overall: UNHEALTHY" in result.stdout


def test_doctor_reports_missing_bot_executable_as_unhealthy(
    healthy_installation: tuple[Path, dict[str, str]],
) -> None:
    checkout, environment = healthy_installation
    (checkout / ".venv/bin/discord-stats-bot").unlink()

    result = run_manager("doctor", environment=environment)

    assert result.returncode == 1
    assert "[FAIL] Bot executable: missing or not executable" in result.stdout
    assert "Overall: UNHEALTHY" in result.stdout


@pytest.mark.parametrize(
    ("active_state", "expected_code", "expected_level", "overall"),
    [
        ("active", 0, "OK", "HEALTHY"),
        ("inactive", 1, "FAIL", "UNHEALTHY"),
    ],
)
def test_doctor_bot_service_active_state_controls_result(
    tmp_path: Path,
    active_state: str,
    expected_code: int,
    expected_level: str,
    overall: str,
) -> None:
    checkout = create_checkout(tmp_path)
    environment = create_manager_environment(
        tmp_path,
        checkout,
        bot_active_state=active_state,
    )

    result = run_manager("doctor", environment=environment)

    assert result.returncode == expected_code
    assert f"[{expected_level}] kanami.service active: {active_state}" in result.stdout
    assert f"Overall: {overall}" in result.stdout


@pytest.mark.parametrize("load_state", ["masked", "error", "bad-setting"])
def test_doctor_abnormal_bot_load_state_is_unhealthy(
    tmp_path: Path,
    load_state: str,
) -> None:
    checkout = create_checkout(tmp_path)
    environment = create_manager_environment(
        tmp_path,
        checkout,
        bot_load_state=load_state,
        bot_active_state="active",
    )

    result = run_manager("doctor", environment=environment)

    assert result.returncode == 1
    assert (
        f"[FAIL] kanami.service unit: abnormal load state: {load_state}"
        in result.stdout
    )
    assert f"[SKIP] kanami.service active: load state is {load_state}" in result.stdout
    assert "Overall: UNHEALTHY" in result.stdout


def test_doctor_masked_optional_web_admin_is_warning_only(tmp_path: Path) -> None:
    checkout = create_checkout(tmp_path)
    environment = create_manager_environment(
        tmp_path,
        checkout,
        web_load_state="masked",
        web_active_state="active",
    )

    result = run_manager("doctor", environment=environment)

    assert result.returncode == 0
    assert (
        "[WARN] kanami-web-admin.service unit: "
        "optional unit has abnormal load state: masked"
    ) in result.stdout
    assert (
        "[SKIP] kanami-web-admin.service active: load state is masked" in result.stdout
    )
    assert "Overall: HEALTHY" in result.stdout


def test_status_exposes_abnormal_unit_load_states(tmp_path: Path) -> None:
    checkout = create_checkout(tmp_path)
    environment = create_manager_environment(
        tmp_path,
        checkout,
        bot_load_state="masked",
        bot_active_state="active",
        web_load_state="bad-setting",
        web_active_state="active",
    )

    result = run_manager("status", environment=environment)

    assert result.returncode == 0
    assert "kanami.service: masked (abnormal load state)" in result.stdout
    assert (
        "kanami-web-admin.service: bad-setting (abnormal load state)" in result.stdout
    )


def test_doctor_inactive_optional_web_admin_is_not_fatal(tmp_path: Path) -> None:
    checkout = create_checkout(tmp_path)
    write_executable(checkout / ".venv/bin/kanami-web-admin")
    run_git(checkout, "add", "--force", ".venv/bin/kanami-web-admin")
    run_git(checkout, "commit", "-m", "add optional web executable")
    environment = create_manager_environment(
        tmp_path,
        checkout,
        web_load_state="loaded",
        web_active_state="inactive",
    )

    result = run_manager("doctor", environment=environment)

    assert result.returncode == 0
    assert "[WARN] kanami-web-admin.service active: optional service inactive" in (
        result.stdout
    )
    assert "Overall: HEALTHY" in result.stdout


def test_unavailable_systemctl_is_reported_without_shell_failure(
    tmp_path: Path,
    healthy_installation: tuple[Path, dict[str, str]],
) -> None:
    _, environment = healthy_installation
    git_path = shutil.which("git")
    assert git_path is not None
    if sys.platform == "win32":
        environment["PATH"] = str(Path(git_path).parent)
    else:
        isolated_bin = tmp_path / "isolated-bin"
        isolated_bin.mkdir()
        (isolated_bin / "git").symlink_to(git_path)
        environment["PATH"] = str(isolated_bin)

    status_result = run_manager("status", environment=environment)
    doctor_result = run_manager("doctor", environment=environment)

    assert status_result.returncode == 0
    assert "kanami.service: unknown (systemctl unavailable)" in status_result.stdout
    assert doctor_result.returncode == 0
    assert "[WARN] systemctl: command unavailable" in doctor_result.stdout
    assert "[WARN] kanami.service unit: required unit could not be verified" in (
        doctor_result.stdout
    )
    assert "[SKIP] kanami.service active: systemctl unavailable" in (
        doctor_result.stdout
    )
    assert "Overall: HEALTHY" in doctor_result.stdout


def test_status_and_doctor_never_print_environment_secrets(
    healthy_installation: tuple[Path, dict[str, str]],
) -> None:
    _, environment = healthy_installation

    results = (
        run_manager("status", environment=environment),
        run_manager("doctor", environment=environment),
    )

    for result in results:
        output = result.stdout + result.stderr
        assert SECRET_TOKEN not in output
        assert SECRET_DATABASE_URL not in output
        assert "secret-password" not in output


def test_unknown_command_fails_with_a_help_hint() -> None:
    result = run_manager("not-a-command")

    assert result.returncode == 2
    assert result.stdout == ""
    assert "unknown command: not-a-command" in result.stderr
    assert "kanami help" in result.stderr
