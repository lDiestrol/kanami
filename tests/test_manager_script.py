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
) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    process_environment = os.environ.copy()
    if environment is not None:
        process_environment.update(environment)
    return subprocess.run(
        [BASH, str(MANAGER_SCRIPT), *arguments],
        cwd=REPOSITORY_ROOT,
        env=process_environment,
        check=False,
        capture_output=True,
        text=True,
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


def test_no_arguments_shows_help() -> None:
    result = run_manager()

    assert result.returncode == 0
    assert "Kanami Manager" in result.stdout
    assert "Usage: kanami [command]" in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize("argument", ["help", "--help", "-h"])
def test_help_aliases_show_the_same_help(argument: str) -> None:
    result = run_manager(argument)
    default_result = run_manager()

    assert result.returncode == 0
    assert result.stdout == default_result.stdout
    assert "status" in result.stdout
    assert "doctor" in result.stdout
    assert result.stderr == ""


def test_version_shows_manager_name_and_optional_git_commit() -> None:
    result = run_manager("version")

    assert result.returncode == 0
    assert result.stderr == ""
    assert re.fullmatch(
        r"Kanami Manager\n(?:Git commit: [0-9a-f]+\n)?",
        result.stdout,
    )


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
