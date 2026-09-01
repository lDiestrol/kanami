import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MANAGER_SCRIPT = REPOSITORY_ROOT / "scripts/manager.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or BASH is None,
    reason="Kanami Manager requires Bash on a Unix-like platform",
)


def run_manager(*arguments: str) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, str(MANAGER_SCRIPT), *arguments],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


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
    assert result.stderr == ""


def test_version_shows_manager_name_and_optional_git_commit() -> None:
    result = run_manager("version")

    assert result.returncode == 0
    assert result.stderr == ""
    assert re.fullmatch(
        r"Kanami Manager\n(?:Git commit: [0-9a-f]+\n)?",
        result.stdout,
    )


def test_unknown_command_fails_with_a_help_hint() -> None:
    result = run_manager("not-a-command")

    assert result.returncode == 2
    assert result.stdout == ""
    assert "unknown command: not-a-command" in result.stderr
    assert "kanami help" in result.stderr
