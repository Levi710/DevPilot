import os
import sys
import tempfile
from pathlib import Path
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
DEV_PILOT_DIR = SRC_DIR / "devpilot"

sys.path.insert(0, str(DEV_PILOT_DIR))
sys.path.insert(0, str(SRC_DIR))

from sandbox import (
    DEFAULT_EXECUTION_TIMEOUT,
    ExecutionResult,
    ShadowWorkspace,
    _classify_failure,
    run_reproduction_test,
    screen_test_ast,
    validate_patch,
)


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """Create a temporary mock repository."""
    repo = tmp_path / "mock_repo"
    repo.mkdir()

    src = repo / "src"
    src.mkdir()
    calc = src / "calculator.py"
    calc.write_text(
        "def calculate_average(items):\n    return sum(items) / len(items)\n",
        encoding="utf-8",
    )

    # Add sensitive file and git dir that should be ignored
    env_file = repo / ".env"
    env_file.write_text("SECRET_KEY=12345\n", encoding="utf-8")

    git_dir = repo / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("dummy", encoding="utf-8")

    return repo


def test_shadow_workspace_mirroring_and_isolation(sample_repo: Path):
    """Verify shadow workspace mirrors files, excludes sensitive files, and leaves host untouched."""
    original_calc_content = (sample_repo / "src" / "calculator.py").read_text(encoding="utf-8")

    with ShadowWorkspace(str(sample_repo)) as shadow:
        assert shadow.shadow_root is not None
        assert shadow.shadow_root.exists()

        # Mirrored files exist
        shadow_calc = shadow.shadow_root / "src" / "calculator.py"
        assert shadow_calc.exists()
        assert shadow.read_shadow_file("src/calculator.py") == original_calc_content

        # Excluded and sensitive files are NOT mirrored
        assert not (shadow.shadow_root / ".env").exists()
        assert not (shadow.shadow_root / ".git").exists()

        # Write test file in shadow
        test_file = shadow.write_test_file("test_calc.py", "print('hello from shadow')")
        assert test_file.exists()
        assert not (sample_repo / "test_calc.py").exists()

        # Patch a file in shadow
        shadow.apply_patch("src/calculator.py", "# patched content")
        assert shadow.read_shadow_file("src/calculator.py") == "# patched content"

        # Verify host original was NOT touched
        assert (sample_repo / "src" / "calculator.py").read_text(encoding="utf-8") == original_calc_content

    # Once exited, shadow dir must be cleaned up
    assert not shadow.shadow_root or not shadow.shadow_root.exists()


def test_screen_test_ast():
    """Verify static AST screening permits safe tests and rejects hazardous operations."""
    # Safe code
    safe_code = """
import unittest
from src.calculator import calculate_average

def test_empty():
    calculate_average([])
"""
    is_safe, reason = screen_test_ast(safe_code)
    assert is_safe is True
    assert reason is None

    # Forbidden import
    unsafe_import = "import socket\ns = socket.socket()"
    is_safe, reason = screen_test_ast(unsafe_import)
    assert is_safe is False
    assert "socket" in reason

    # Forbidden call
    unsafe_call = "import os\nos.system('dir')"
    is_safe, reason = screen_test_ast(unsafe_call)
    assert is_safe is False
    assert "system" in reason

    # Syntax error
    invalid_syntax = "def broken(:"
    is_safe, reason = screen_test_ast(invalid_syntax)
    assert is_safe is False
    assert "Syntax error" in reason


def test_execute_script_clean_pass(sample_repo: Path):
    """Test running a script that passes cleanly."""
    with ShadowWorkspace(str(sample_repo)) as shadow:
        shadow.write_test_file(
            "run_pass.py",
            "from src.calculator import calculate_average\n"
            "assert calculate_average([10, 20]) == 15\n"
            "print('PASS')",
        )
        res = shadow.execute_script("run_pass.py")
        assert res.exit_code == 0
        assert "PASS" in res.stdout
        assert res.failure_classification == "clean_pass"
        assert res.exception_type is None


def test_execute_script_supports_src_layout_imports(sample_repo: Path):
    """Projects using src/package layout must import the shadow source tree."""
    with ShadowWorkspace(str(sample_repo)) as shadow:
        shadow.write_test_file(
            "src_layout_import.py",
            "from calculator import calculate_average\n"
            "assert calculate_average([10, 20]) == 15\n",
        )
        res = shadow.execute_script("src_layout_import.py")
        assert res.exit_code == 0
        assert res.failure_classification == "clean_pass"


def test_execute_script_target_exception(sample_repo: Path):
    """Test running a reproduction test that triggers a ZeroDivisionError."""
    with ShadowWorkspace(str(sample_repo)) as shadow:
        shadow.write_test_file(
            "reproduce.py",
            "from src.calculator import calculate_average\n"
            "calculate_average([])\n",
        )
        res = shadow.execute_script("reproduce.py")
        assert res.exit_code != 0
        assert res.failure_classification == "target_exception"
        assert res.exception_type == "ZeroDivisionError"
        assert "division by zero" in (res.exception_message or "")
        assert res.traceback_snippet is not None


def test_execute_script_import_error(sample_repo: Path):
    """Test distinguishing import errors from targeted bug exceptions."""
    with ShadowWorkspace(str(sample_repo)) as shadow:
        shadow.write_test_file(
            "test_missing_dep.py",
            "import non_existent_library_12345\n",
        )
        res = shadow.execute_script("test_missing_dep.py")
        assert res.exit_code != 0
        assert res.failure_classification == "import_error"
        assert res.exception_type in ("ModuleNotFoundError", "ImportError")


def test_execute_script_timeout(sample_repo: Path):
    """Test enforcing timeout and classifying as timeout."""
    with ShadowWorkspace(str(sample_repo)) as shadow:
        shadow.write_test_file(
            "hang.py",
            "import time\ntime.sleep(5)\n",
        )
        res = shadow.execute_script("hang.py", timeout=0.5)
        assert res.timed_out is True
        assert res.failure_classification == "timeout"
        assert res.exception_type == "TimeoutExpired"


def test_execute_script_environment_sanitization(sample_repo: Path, monkeypatch):
    """Verify sensitive environment variables like NVIDIA_API_KEY are stripped."""
    monkeypatch.setenv("NVIDIA_API_KEY", "secret-test-key-999")
    with ShadowWorkspace(str(sample_repo)) as shadow:
        shadow.write_test_file(
            "check_env.py",
            "import os\n"
            "print('KEY:' + str(os.environ.get('NVIDIA_API_KEY')))\n",
        )
        res = shadow.execute_script("check_env.py")
        assert res.exit_code == 0
        assert "KEY:None" in res.stdout


def test_execute_script_does_not_inherit_unrelated_environment(sample_repo: Path, monkeypatch):
    """A clean sandbox environment must not leak arbitrary parent variables."""
    monkeypatch.setenv("DEVPILOT_UNRELATED_SECRET", "do-not-leak")
    with ShadowWorkspace(str(sample_repo)) as shadow:
        shadow.write_test_file(
            "check_unrelated_env.py",
            "import os\n"
            "print(os.environ.get('DEVPILOT_UNRELATED_SECRET'))\n",
        )
        res = shadow.execute_script("check_unrelated_env.py")
        assert res.exit_code == 0
        assert res.stdout.strip() == "None"


def test_execute_script_blocks_host_file_read(sample_repo: Path):
    """Executed code must not be able to read the original repository."""
    host_target = sample_repo / "host_secret.txt"
    host_target.write_text("host-only-value", encoding="utf-8")
    with ShadowWorkspace(str(sample_repo)) as shadow:
        escaped_path = str(host_target).replace("\\", "\\\\")
        shadow.write_test_file(
            "attack_read.py",
            f"print(open(r'{escaped_path}', encoding='utf-8').read())\n",
        )
        res = shadow.execute_script("attack_read.py", bypass_ast_screen=True)
        assert res.exit_code != 0
        assert res.failure_classification == "security_violation"
        assert "read access outside allowed runtime roots is forbidden" in res.stderr
        assert "host-only-value" not in res.stdout


def test_shadow_workspace_skips_symlink_escaping_repository(sample_repo: Path, tmp_path: Path):
    """Mirroring must not follow a link out of the registered repository."""
    outside = tmp_path / "outside.txt"
    outside.write_text("not part of repository", encoding="utf-8")
    link = sample_repo / "escaped-link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symbolic links are not available for this test user.")

    with ShadowWorkspace(str(sample_repo)) as shadow:
        assert not (shadow.shadow_root / "escaped-link.txt").exists()


def test_run_reproduction_test_matches_expected_exception(sample_repo: Path):
    """Phase 2 / Rule 1: Confirm reproduction test succeeds when expected exception matches."""
    test_code = (
        "from src.calculator import calculate_average\n"
        "calculate_average([])\n"
    )
    result = run_reproduction_test(
        repo_root=str(sample_repo),
        test_code=test_code,
        expected_exception="ZeroDivisionError",
    )
    assert result["reproduced"] is True
    assert result["status"] == "reproduced_expected"
    assert result["exception_type"] == "ZeroDivisionError"
    assert result["exit_code"] != 0
    assert result["traceback"] is not None


def test_run_reproduction_test_rejects_wrong_exception(sample_repo: Path):
    """Phase 2 / Rule 1: A failure with an unexpected exception must NOT be classified as reproduced."""
    test_code = (
        "from src.calculator import calculate_average\n"
        "calculate_average([])\n"
    )
    result = run_reproduction_test(
        repo_root=str(sample_repo),
        test_code=test_code,
        expected_exception="ValueError",
    )
    assert result["reproduced"] is False
    assert "unexpected_exception" in result["status"]
    assert result["exception_type"] == "ZeroDivisionError"


def test_run_reproduction_test_rejects_import_error(sample_repo: Path):
    """Phase 2 / Rule 1: Missing dependencies or import errors are classified separately, not as bugs."""
    test_code = "import nonexistent_package_abc\n"
    result = run_reproduction_test(
        repo_root=str(sample_repo),
        test_code=test_code,
        expected_exception="ZeroDivisionError",
    )
    assert result["reproduced"] is False
    assert result["status"] == "import_error"


def test_validate_patch_successful_end_to_end(sample_repo: Path):
    """Phase 3: Successful patch validation in shadow workspace leaves host repo untouched."""
    original_calc = (sample_repo / "src" / "calculator.py").read_text(encoding="utf-8")
    test_code = (
        "from src.calculator import calculate_average\n"
        "assert calculate_average([]) == 0\n"
    )
    patched_code = (
        "def calculate_average(items):\n"
        "    if not items:\n"
        "        return 0\n"
        "    return sum(items) / len(items)\n"
    )

    result = validate_patch(
        repo_root=str(sample_repo),
        patch_file="src/calculator.py",
        patched_content=patched_code,
        test_code=test_code,
        expected_exception="ZeroDivisionError",
    )

    assert result["valid"] is False
    assert result["analysis_passed"] is True
    assert result["reproduction_confirmed"] is True
    assert result["fix_verified"] is False
    assert result["reproduction_resolved"] is True
    assert result["verification_status"] == "regression_suite_missing"
    assert result["pre_fix"]["reproduced"] is True
    assert result["pre_fix"]["exception_type"] == "ZeroDivisionError"
    assert result["post_fix"]["exit_code"] == 0
    assert "--- a/src/calculator.py" in result["diff"]
    assert "+++ b/src/calculator.py" in result["diff"]
    assert "+        return 0" in result["diff"]

    # Crucial guarantee: Host repository remains completely untouched!
    assert (sample_repo / "src" / "calculator.py").read_text(encoding="utf-8") == original_calc


def test_validate_patch_rejects_flawed_fix_breaking_regression(sample_repo: Path):
    """Phase 3 / Rule 2: Distinguish reproduction resolution from regression suite pass."""
    # Add an existing regression test to the mock repository
    tests_dir = sample_repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_calculator.py").write_text(
        "from src.calculator import calculate_average\n\n"
        "def test_normal_average():\n"
        "    assert calculate_average([10, 20]) == 15\n",
        encoding="utf-8",
    )

    # Flawed patch: hardcodes return 0, which satisfies [] but breaks [10, 20]
    flawed_patch = (
        "def calculate_average(items):\n"
        "    return 0\n"
    )
    reproduction_test = (
        "from src.calculator import calculate_average\n"
        "assert calculate_average([]) == 0\n"
    )

    result = validate_patch(
        repo_root=str(sample_repo),
        patch_file="src/calculator.py",
        patched_content=flawed_patch,
        test_code=reproduction_test,
        expected_exception="ZeroDivisionError",
    )

    # Reproduction resolved on empty list, BUT regression suite fails on normal average!
    assert result["reproduction_resolved"] is True
    assert result["reproduction_confirmed"] is True
    assert result["fix_verified"] is False
    assert result["regression_suite_passed"] is False
    assert result["verification_status"] == "regression_failed"
    assert result["valid"] is False
    assert "FAILED tests" in result["regression_output"]


def test_regression_suite_blocks_host_writes(sample_repo: Path):
    """A suite that escapes the shadow cannot produce a verified patch."""
    host_target = sample_repo / "regression_host_breach.txt"
    tests_dir = sample_repo / "tests"
    tests_dir.mkdir()
    escaped_path = str(host_target).replace("\\", "\\\\")
    (tests_dir / "test_host_escape.py").write_text(
        "from pathlib import Path\n\n"
        "def test_cannot_write_host_file():\n"
        f"    Path(r'{escaped_path}').write_text('breach', encoding='utf-8')\n",
        encoding="utf-8",
    )

    result = validate_patch(
        repo_root=str(sample_repo),
        patch_file="src/calculator.py",
        patched_content=(
            "def calculate_average(items):\n"
            "    if not items:\n"
            "        return 0\n"
            "    return sum(items) / len(items)\n"
        ),
        test_code=(
            "from src.calculator import calculate_average\n"
            "assert calculate_average([]) == 0\n"
        ),
        expected_exception="ZeroDivisionError",
    )

    assert result["reproduction_resolved"] is True
    assert result["regression_suite_passed"] is False
    assert result["fix_verified"] is False
    assert not host_target.exists()
    assert "Sandbox violation: write access outside shadow workspace" in result["regression_output"]


def test_regression_suite_runs_every_discovered_test(sample_repo: Path):
    """A failure after the historical 20-file cap must invalidate verification."""
    tests_dir = sample_repo / "tests"
    tests_dir.mkdir()
    for number in range(20):
        (tests_dir / f"test_{number:02d}_pass.py").write_text(
            f"def test_{number:02d}_pass():\n    assert True\n",
            encoding="utf-8",
        )
    (tests_dir / "test_99_late_failure.py").write_text(
        "def test_late_failure():\n    assert False, 'late regression failure'\n",
        encoding="utf-8",
    )

    result = validate_patch(
        repo_root=str(sample_repo),
        patch_file="src/calculator.py",
        patched_content=(
            "def calculate_average(items):\n"
            "    if not items:\n"
            "        return 0\n"
            "    return sum(items) / len(items)\n"
        ),
        test_code=(
            "from src.calculator import calculate_average\n"
            "assert calculate_average([]) == 0\n"
        ),
        expected_exception="ZeroDivisionError",
    )

    assert result["regression_suite_passed"] is False
    assert result["fix_verified"] is False
    assert "late regression failure" in result["regression_output"]


def test_validate_patch_rejects_unreproduced_baseline(sample_repo: Path):
    """Requirement 1: A patch must be rejected if the baseline test does not reproduce the bug."""
    test_code = (
        "from src.calculator import calculate_average\n"
        "assert calculate_average([10, 20]) == 15\n"
    )
    patched_code = (
        "def calculate_average(items):\n"
        "    return 15\n"
    )
    result = validate_patch(
        repo_root=str(sample_repo),
        patch_file="src/calculator.py",
        patched_content=patched_code,
        test_code=test_code,
        expected_exception="ZeroDivisionError",
    )
    assert result["valid"] is False
    assert result["reproduction_confirmed"] is False
    assert result["fix_verified"] is False
    assert result["verification_status"] == "reproduction_failed"


def test_adversarial_network_access_blocked_by_os_boundary(sample_repo: Path):
    """Adversarial Test 1: Verify direct socket operations are blocked at OS/C runtime level."""
    with ShadowWorkspace(str(sample_repo)) as shadow:
        # Script attempts raw socket connection without relying on environment proxy
        shadow.write_test_file(
            "attack_network.py",
            "import socket\n"
            "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "s.connect(('127.0.0.1', 80))\n"
        )
        res = shadow.execute_script("attack_network.py", bypass_ast_screen=True)
        assert res.exit_code != 0
        assert res.failure_classification == "security_violation"
        assert "Sandbox violation: direct network access is forbidden" in res.stderr


def test_adversarial_filesystem_write_outside_shadow_blocked(sample_repo: Path):
    """Adversarial Test 2: Verify file writes outside shadow workspace are blocked by OS boundary."""
    host_target = sample_repo / "host_breach.txt"
    with ShadowWorkspace(str(sample_repo)) as shadow:
        # Script attempts to write to the host repository
        escaped_path = str(host_target).replace("\\", "\\\\")
        shadow.write_test_file(
            "attack_fs.py",
            f"with open(r'{escaped_path}', 'w') as f:\n"
            f"    f.write('malicious payload')\n"
        )
        res = shadow.execute_script("attack_fs.py", bypass_ast_screen=True)
        assert res.exit_code != 0
        assert res.failure_classification == "security_violation"
        assert "Sandbox violation: write access outside shadow workspace is forbidden" in res.stderr
        # Verify host file was NEVER created
        assert not host_target.exists()


def test_adversarial_process_spawn_blocked(sample_repo: Path):
    """Adversarial Test 3: Verify subprocess creation is blocked by OS boundary."""
    with ShadowWorkspace(str(sample_repo)) as shadow:
        shadow.write_test_file(
            "attack_proc.py",
            "import subprocess\n"
            "subprocess.Popen(['cmd.exe'])\n"
        )
        res = shadow.execute_script("attack_proc.py", bypass_ast_screen=True)
        assert res.exit_code != 0
        assert res.failure_classification == "security_violation"
        assert "Sandbox violation: child process execution is forbidden" in res.stderr
