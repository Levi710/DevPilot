"""DevPilot sandboxed shadow-workspace execution engine.

Provides isolated shadow workspace creation, bounded repository mirroring,
subprocess execution with resource limits and timeouts, exception parsing,
and patch validation without modifying the target repository.
"""

import ast
import difflib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    from tools import EXCLUDED_DIRS, MAX_FILE_SIZE, SENSITIVE_NAMES, _is_excluded, _is_sensitive_filename
except ImportError:
    from devpilot.tools import EXCLUDED_DIRS, MAX_FILE_SIZE, SENSITIVE_NAMES, _is_excluded, _is_sensitive_filename


# Maximum duration for a reproduction test subprocess (seconds)
DEFAULT_EXECUTION_TIMEOUT = 5.0

# Maximum stdout/stderr capture size in bytes to prevent memory exhaustion
MAX_OUTPUT_BYTES = 50_000

# Windows Job Object definitions for resource limits.  Python audit hooks in
# sandbox_runner.py enforce the runtime policy; this job limits process scope.
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryLimit", ctypes.c_size_t),
            ("PeakJobMemoryLimit", ctypes.c_size_t),
        ]

    def _create_isolated_job() -> int:
        """Create a Windows Job Object with kill-on-close, memory limits, and process limits."""
        kernel32 = ctypes.windll.kernel32
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError("CreateJobObjectW failed")

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        # 0x2000 = KILL_ON_JOB_CLOSE, 0x0008 = ACTIVE_PROCESS_LIMIT, 0x0200 = JOB_MEMORY_LIMIT
        info.BasicLimitInformation.LimitFlags = 0x2000 | 0x0008 | 0x0200
        info.BasicLimitInformation.ActiveProcessLimit = 5
        info.JobMemoryLimit = 256 * 1024 * 1024  # 256 MB memory limit
        configured = kernel32.SetInformationJobObject(
            job,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not configured:
            kernel32.CloseHandle(job)
            raise OSError("SetInformationJobObject failed")
        return job

    def _assign_process_to_job(job: int, proc_handle: int) -> None:
        if not proc_handle or not ctypes.windll.kernel32.AssignProcessToJobObject(job, proc_handle):
            raise OSError("AssignProcessToJobObject failed")

    def _close_job(job: int | None) -> None:
        if job:
            try:
                ctypes.windll.kernel32.CloseHandle(job)
            except Exception:
                pass
else:
    def _create_isolated_job() -> None:
        return None

    def _assign_process_to_job(job, proc_handle) -> None:
        pass

    def _close_job(job) -> None:
        pass


@dataclass
class ExecutionResult:
    """Structured result of executing a script inside the sandbox."""

    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    exception_type: str | None = None
    exception_message: str | None = None
    traceback_snippet: str | None = None
    failure_classification: str = "unknown"
    # Classifications: "clean_pass", "target_exception", "import_error",
    # "syntax_error", "environment_error", "timeout", "unknown_failure"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _classify_failure(exit_code: int, timed_out: bool, stderr: str, stdout: str) -> tuple[str, str | None, str | None, str | None]:
    """Parse stderr/stdout to extract exception details and classify failure."""
    if timed_out:
        return "timeout", "TimeoutExpired", "Test exceeded maximum allowed time.", None

    if exit_code == 0:
        return "clean_pass", None, None, None

    combined = stderr + "\n" + stdout

    # Match standard Python traceback patterns: e.g. "ZeroDivisionError: division by zero" or "AssertionError"
    exc_match = re.search(r"^([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Warning|Interrupt|Exit))(?::\s*(.*))?$", combined, re.MULTILINE)
    exc_type = exc_match.group(1) if exc_match else None
    exc_msg = (exc_match.group(2) or "").strip() if exc_match else None

    # Extract traceback snippet
    tb_match = re.search(r"(Traceback \(most recent call last\):[\s\S]+)", combined)
    tb_snippet = tb_match.group(1).strip()[-2000:] if tb_match else None

    if "Sandbox violation:" in combined:
        classification = "security_violation"
        exc_type = exc_type or "SecurityViolation"
    elif exc_type in ("ImportError", "ModuleNotFoundError"):
        classification = "import_error"
    elif exc_type in ("SyntaxError", "IndentationError", "TabError"):
        classification = "syntax_error"
    elif exc_type in ("EnvironmentError", "IOError", "OSError", "PermissionError"):
        classification = "environment_error"
    elif exc_type:
        classification = "target_exception"
    else:
        classification = "unknown_failure"

    return classification, exc_type, exc_msg, tb_snippet


def screen_test_ast(code: str) -> tuple[bool, str | None]:
    """
    Static safety screening of reproduction script before execution.

    Rejects obvious host-destructive primitives (AST heuristic defense).
    The execution harness provides the runtime safety boundary.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as err:
        return False, f"Syntax error in test script: {err}"

    forbidden_modules = {"shutil", "socket", "http", "urllib", "ctypes"}
    forbidden_calls = {"system", "popen", "spawn", "rmtree", "unlink", "kill"}

    for node in ast.walk(tree):
        # Check imports: e.g. import socket, from subprocess import Popen
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod_root = alias.name.split(".")[0]
                if mod_root in forbidden_modules:
                    return False, f"Importing '{mod_root}' is not permitted in reproduction scripts."

        if isinstance(node, ast.ImportFrom):
            mod_root = (node.module or "").split(".")[0]
            if mod_root in forbidden_modules:
                return False, f"Importing from '{mod_root}' is not permitted in reproduction scripts."

        # Check dangerous attribute calls: e.g. os.system, os.kill
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr in forbidden_calls:
                return False, f"Calling '{node.func.attr}()' is not permitted in reproduction scripts."

    return True, None


class ShadowWorkspace:
    """
    Creates and manages an ephemeral shadow copy of the repository.

    The original repository is treated as strictly read-only. All writes,
    patches, and test files are isolated to this shadow directory.
    """

    def __init__(self, repo_root: str, python_executable: str | None = None):
        self.repo_root = Path(repo_root).resolve()
        if not self.repo_root.is_dir():
            raise ValueError(f"Repository root does not exist: {repo_root}")

        self.python_executable = Path(python_executable or sys.executable).resolve()
        if not self.python_executable.is_file():
            raise ValueError(f"Python runtime does not exist: {self.python_executable}")

        self._temp_dir: tempfile.TemporaryDirectory | None = None
        self.shadow_root: Path | None = None

    def __enter__(self) -> "ShadowWorkspace":
        self._temp_dir = tempfile.TemporaryDirectory(prefix="devpilot_sandbox_")
        self.shadow_root = Path(self._temp_dir.name).resolve()
        self._mirror_repository()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._temp_dir:
            try:
                self._temp_dir.cleanup()
            except Exception:
                pass
            self._temp_dir = None
            self.shadow_root = None

    def _mirror_repository(self) -> None:
        """Copy repository files to shadow root, respecting exclusions and bounds."""
        for item in self.repo_root.rglob("*"):
            try:
                resolved_item = item.resolve()
                # Never mirror a symlink that resolves outside of the registered
                # repository: it could disclose arbitrary host files.
                if not resolved_item.is_relative_to(self.repo_root):
                    continue

                rel_path = item.relative_to(self.repo_root)

                if _is_excluded(rel_path):
                    continue

                if _is_sensitive_filename(item.name):
                    continue

                dest = self.shadow_root / rel_path

                if item.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                elif item.is_file():
                    if resolved_item.stat().st_size <= MAX_FILE_SIZE:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(resolved_item, dest)
            except (OSError, ValueError):
                continue

    def write_test_file(self, filename: str, content: str) -> Path:
        """Write a reproduction test file into the shadow workspace."""
        if not self.shadow_root:
            raise RuntimeError("Shadow workspace is not active.")

        safe_filename = Path(filename).name
        target = self.shadow_root / safe_filename
        target.write_text(content, encoding="utf-8")
        return target

    def apply_patch(self, rel_path: str, new_content: str) -> None:
        """Overwrite a single file in the shadow workspace with patched content."""
        if not self.shadow_root:
            raise RuntimeError("Shadow workspace is not active.")

        target = (self.shadow_root / rel_path).resolve()
        if not target.is_relative_to(self.shadow_root):
            raise ValueError("Target file is outside the shadow workspace.")

        if not target.is_file():
            raise FileNotFoundError(f"File to patch does not exist in shadow: {rel_path}")

        target.write_text(new_content, encoding="utf-8")

    def read_shadow_file(self, rel_path: str) -> str:
        """Read a file from the shadow workspace."""
        if not self.shadow_root:
            raise RuntimeError("Shadow workspace is not active.")

        target = (self.shadow_root / rel_path).resolve()
        if not target.is_relative_to(self.shadow_root) or not target.is_file():
            raise FileNotFoundError(f"File not found in shadow: {rel_path}")

        return target.read_text(encoding="utf-8", errors="replace")

    def _sandbox_environment(self) -> dict[str, str]:
        """Build a minimal, deterministic environment for executed code.

        This allow-list prevents unrelated credentials, package configuration,
        and user paths from leaking into a validation run.
        """
        if not self.shadow_root:
            raise RuntimeError("Shadow workspace is not active.")

        sandbox_temp = self.shadow_root / ".devpilot-tmp"
        sandbox_temp.mkdir(exist_ok=True)
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        py_dir = str(self.python_executable.parent)
        return {
            "ALL_PROXY": "http://127.0.0.1:9",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
            "PATH": f"{py_dir};{system_root}\\System32;{system_root}",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(self.shadow_root),
            "PYTHONUNBUFFERED": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "SystemRoot": system_root,
            "TEMP": str(sandbox_temp),
            "TMP": str(sandbox_temp),
            "WINDIR": os.environ.get("WINDIR", system_root),
        }

    def execute_script(
        self,
        script_rel_path: str,
        timeout: float = DEFAULT_EXECUTION_TIMEOUT,
        bypass_ast_screen: bool = False,
    ) -> ExecutionResult:
        """
        Execute a script inside the shadow workspace in a time-limited subprocess.

        Enforces Windows Job Object resource limits and an audit-hook runtime
        policy that blocks network access and host filesystem access.
        """
        if not self.shadow_root:
            raise RuntimeError("Shadow workspace is not active.")

        script_target = (self.shadow_root / script_rel_path).resolve()
        if not script_target.is_file():
            return ExecutionResult(
                exit_code=1,
                stdout="",
                stderr=f"Error: Script not found: {script_rel_path}",
                duration_seconds=0.0,
                failure_classification="environment_error",
                exception_type="FileNotFoundError",
                exception_message=f"Script not found: {script_rel_path}",
            )

        # Static AST screening supplements the runtime boundary.
        if not bypass_ast_screen:
            script_code = script_target.read_text(encoding="utf-8", errors="replace")
            safe, reason = screen_test_ast(script_code)
            if not safe:
                return ExecutionResult(
                    exit_code=1,
                    stdout="",
                    stderr=f"Security rejection: {reason}",
                    duration_seconds=0.0,
                    failure_classification="environment_error",
                    exception_type="SecurityError",
                    exception_message=reason,
                )

        env = self._sandbox_environment()

        start_time = time.perf_counter()
        timed_out = False
        stdout_text = ""
        stderr_text = ""
        exit_code = 0

        # On Windows, fail closed if resource-limited process isolation cannot
        # be set up.  A script must never be marked verified without it.
        try:
            job = _create_isolated_job()
        except OSError as exc:
            return ExecutionResult(
                exit_code=1,
                stdout="",
                stderr=f"Sandbox setup failed: {exc}",
                duration_seconds=round(time.perf_counter() - start_time, 3),
                failure_classification="environment_error",
                exception_type="SandboxSetupError",
                exception_message=str(exc),
            )
        runner_script = Path(__file__).resolve().parent / "sandbox_runner.py"
        proc: subprocess.Popen[str] | None = None
        try:
            cmd = [
                str(self.python_executable),
                "-I",
                str(runner_script),
                str(self.shadow_root),
                str(script_target),
            ]
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.shadow_root),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if sys.platform == "win32":
                _assign_process_to_job(job, getattr(proc, "_handle", 0))

            try:
                stdout_raw, stderr_raw = proc.communicate(timeout=timeout)
                exit_code = proc.returncode
                stdout_text = (stdout_raw or "")[:MAX_OUTPUT_BYTES]
                stderr_text = (stderr_raw or "")[:MAX_OUTPUT_BYTES]
            except subprocess.TimeoutExpired:
                timed_out = True
                exit_code = 124
                proc.kill()
                stdout_raw, stderr_raw = proc.communicate()
                stdout_text = (stdout_raw or "")[:MAX_OUTPUT_BYTES]
                stderr_text = (stderr_raw or "")[:MAX_OUTPUT_BYTES]
                stderr_text += f"\nProcess timed out after {timeout} seconds."
        except Exception as exc:
            if proc and proc.poll() is None:
                proc.kill()
                proc.communicate()
            exit_code = 1
            stderr_text = f"Execution failed: {exc}"
        finally:
            _close_job(job)

        duration = round(time.perf_counter() - start_time, 3)

        classification, exc_type, exc_msg, tb_snippet = _classify_failure(
            exit_code=exit_code,
            timed_out=timed_out,
            stderr=stderr_text,
            stdout=stdout_text,
        )

        return ExecutionResult(
            exit_code=exit_code,
            stdout=stdout_text,
            stderr=stderr_text,
            duration_seconds=duration,
            timed_out=timed_out,
            exception_type=exc_type,
            exception_message=exc_msg,
            traceback_snippet=tb_snippet,
            failure_classification=classification,
        )

    def run_existing_regression_suite(
        self,
        exclude_pattern: str = "reproduce_*",
        timeout: float = 15.0,
    ) -> tuple[bool | None, str]:
        """
        Run existing regression tests in the shadow workspace if available (Rule 2).

        Distinguishes reproduction resolution from full regression suite pass.
        Returns (passed, output). If no tests exist in the repo, returns (None, "...").
        """
        if not self.shadow_root:
            raise RuntimeError("Shadow workspace is not active.")

        test_files: list[str] = []
        tests_dir = self.shadow_root / "tests"

        if tests_dir.is_dir():
            for p in tests_dir.rglob("test_*.py"):
                if not p.name.startswith("reproduce_"):
                    test_files.append(str(p.relative_to(self.shadow_root)))
        else:
            for p in self.shadow_root.rglob("test_*.py"):
                rel = p.relative_to(self.shadow_root)
                if not p.name.startswith("reproduce_") and not _is_excluded(rel):
                    test_files.append(str(rel))

        if not test_files:
            return None, "No existing regression test suite found in repository."

        # Use the same protected runner as the reproduction test.  A passing
        # subset is not a passing suite, so all discovered tests are listed.
        runner_name = ".devpilot_regression_runner.py"
        quoted_files = ", ".join(repr(path) for path in sorted(test_files))
        self.write_test_file(
            runner_name,
            "import pytest\n"
            f"raise SystemExit(pytest.main(['-q', {quoted_files}]))\n",
        )
        result = self.execute_script(runner_name, timeout=timeout)
        output = (result.stdout + "\n" + result.stderr)[:MAX_OUTPUT_BYTES]

        if result.timed_out:
            return False, f"Regression tests timed out after {timeout} seconds.\n{output}"
        if result.failure_classification in {"security_violation", "environment_error", "import_error"}:
            return False, f"Regression suite did not complete safely.\n{output}"
        return result.exit_code == 0, output

def run_reproduction_test(
    repo_root: str,
    test_code: str,
    expected_exception: str | None = None,
    timeout: float = DEFAULT_EXECUTION_TIMEOUT,
    python_executable: str | None = None,
) -> dict[str, Any]:
    """
    Execute a reproduction test inside an isolated shadow workspace (Phase 2).

    Requires evidence that the test fails for the expected reason, not merely that it exits nonzero (Rule 1).
    Captures the exception, traceback, exit code, and test source.
    """
    root = Path(repo_root).resolve()
    if not root.is_dir():
        return {
            "reproduced": False,
            "status": "environment_error",
            "exit_code": 1,
            "exception_type": "FileNotFoundError",
            "exception_message": f"Repository root does not exist: {repo_root}",
            "traceback": None,
            "stdout": "",
            "stderr": f"Repository root does not exist: {repo_root}",
            "duration_seconds": 0.0,
            "test_code": test_code,
        }

    try:
        workspace = ShadowWorkspace(str(root), python_executable=python_executable)
    except ValueError as exc:
        return {
            "reproduced": False,
            "status": "environment_error",
            "exit_code": 1,
            "exception_type": "RuntimeConfigurationError",
            "exception_message": str(exc),
            "traceback": None,
            "stdout": "",
            "stderr": str(exc),
            "duration_seconds": 0.0,
            "test_code": test_code,
        }

    with workspace as shadow:
        shadow.write_test_file("reproduce_bug.py", test_code)
        result = shadow.execute_script("reproduce_bug.py", timeout=timeout)

    reproduced = False
    status = result.failure_classification

    if result.failure_classification == "target_exception":
        if expected_exception:
            exp_lower = expected_exception.strip().lower()
            actual_type = (result.exception_type or "").lower()
            actual_msg = (result.exception_message or "").lower()
            if exp_lower in actual_type or exp_lower in actual_msg:
                reproduced = True
                status = "reproduced_expected"
            else:
                reproduced = False
                status = f"unexpected_exception: expected {expected_exception}, got {result.exception_type}"
        else:
            reproduced = True
            status = "reproduced_exception"
    elif result.failure_classification == "clean_pass":
        reproduced = False
        status = "test_passed_unexpectedly"

    return {
        "reproduced": reproduced,
        "status": status,
        "exit_code": result.exit_code,
        "exception_type": result.exception_type,
        "exception_message": result.exception_message,
        "traceback": result.traceback_snippet,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_seconds": result.duration_seconds,
        "timed_out": result.timed_out,
        "failure_classification": result.failure_classification,
        "test_code": test_code,
    }


def validate_patch(
    repo_root: str,
    patch_file: str,
    patched_content: str,
    test_code: str,
    expected_exception: str | None = None,
    timeout: float = DEFAULT_EXECUTION_TIMEOUT,
    run_regression_tests: bool = True,
    regression_test_code: str | None = None,
    python_executable: str | None = None,
) -> dict[str, Any]:
    """
    Apply a narrowly scoped patch only to a shadow copy and validate it against the reproduction test (Phase 3).

    1. Runs reproduction test against the unpatched repository (pre-fix baseline).
    2. Applies patch exclusively in an ephemeral shadow workspace.
    3. Reruns the exact reproduction test to verify it resolves cleanly.
    4. Runs existing repository regression tests or custom regression test code if provided (Rule 2).
    5. Calculates and captures the exact unified diff.
    """
    root = Path(repo_root).resolve()
    if not root.is_dir():
        return {
            "valid": False,
            "reason": f"Repository directory not found: {repo_root}",
        }

    rel_patch_file = Path(patch_file)
    if rel_patch_file.is_absolute():
        try:
            rel_patch_file = rel_patch_file.relative_to(root)
        except ValueError:
            return {
                "valid": False,
                "reason": "Patch file path must be repository-relative.",
            }

    original_file_path = root / rel_patch_file
    if not original_file_path.is_file():
        return {
            "valid": False,
            "reason": f"File to patch does not exist in repository: {rel_patch_file}",
        }

    original_content = original_file_path.read_text(encoding="utf-8", errors="replace")

    # Generate unified diff
    diff_lines = list(
        difflib.unified_diff(
            original_content.splitlines(keepends=True),
            patched_content.splitlines(keepends=True),
            fromfile=f"a/{rel_patch_file.as_posix()}",
            tofile=f"b/{rel_patch_file.as_posix()}",
        )
    )
    unified_diff = "".join(diff_lines)

    # 1. Pre-fix baseline test
    pre_fix = run_reproduction_test(
        repo_root=str(root),
        test_code=test_code,
        expected_exception=expected_exception,
        timeout=timeout,
        python_executable=python_executable,
    )

    if not pre_fix["reproduced"]:
        return {
            "valid": False,
            "verification_status": "reproduction_failed",
            "analysis_passed": True,
            "reproduction_confirmed": False,
            "fix_verified": False,
            "reproduction_resolved": False,
            "regression_suite_passed": None,
            "reason": (
                f"Reproduction test failed baseline check: status={pre_fix['status']}. "
                f"Exception: {pre_fix.get('exception_type') or 'None'}. "
                "The test must reproduce the targeted bug before a patch can be validated."
            ),
            "patched_file": str(rel_patch_file.as_posix()),
            "diff": unified_diff,
            "pre_fix": pre_fix,
            "post_fix": None,
            "test_code": test_code,
            "regression_output": "",
        }

    # 2. Patch and validate in shadow workspace
    try:
        workspace = ShadowWorkspace(str(root), python_executable=python_executable)
    except ValueError as exc:
        return {
            "valid": False,
            "verification_status": "environment_error",
            "analysis_passed": True,
            "reproduction_confirmed": bool(pre_fix.get("reproduced")),
            "fix_verified": False,
            "reproduction_resolved": False,
            "regression_suite_passed": None,
            "reason": str(exc),
            "patched_file": str(rel_patch_file.as_posix()),
            "diff": unified_diff,
            "pre_fix": pre_fix,
            "post_fix": None,
            "test_code": test_code,
            "regression_output": "",
        }

    with workspace as shadow:
        shadow.apply_patch(str(rel_patch_file), patched_content)
        shadow.write_test_file("reproduce_bug.py", test_code)
        post_fix_res = shadow.execute_script("reproduce_bug.py", timeout=timeout)

        # 3. Check reproduction resolution
        reproduction_resolved = (
            post_fix_res.exit_code == 0
            and post_fix_res.failure_classification == "clean_pass"
        )

        # 4. Run existing regression test suite or custom regression script if requested (Rule 2)
        regression_passed = None
        regression_output = ""
        if regression_test_code:
            shadow.write_test_file("regression_check.py", regression_test_code)
            reg_res = shadow.execute_script("regression_check.py", timeout=timeout)
            regression_passed = (reg_res.exit_code == 0 and reg_res.failure_classification == "clean_pass")
            regression_output = (reg_res.stdout + "\n" + reg_res.stderr).strip()
        elif run_regression_tests:
            regression_passed, regression_output = shadow.run_existing_regression_suite(
                exclude_pattern="reproduce_*"
            )

    reproduction_confirmed = bool(pre_fix.get("reproduced"))
    # reproduction_resolved indicates that the targeted reproduction test passed post-patch
    # fix_verified strictly requires:
    # 1. reproduction was confirmed on baseline
    # 2. reproduction was resolved by the patch
    # 3. the complete existing regression test suite passed
    fix_verified = bool(
        reproduction_confirmed
        and reproduction_resolved
        and regression_passed is True
    )

    # Calculate unambiguous verification state (Requirement 1)
    if not reproduction_confirmed:
        verification_status = "reproduction_failed"
    elif regression_passed is False:
        verification_status = "regression_failed"
    elif regression_passed is None and reproduction_resolved:
        verification_status = "regression_suite_missing"
    elif fix_verified:
        verification_status = "fully_verified"
    else:
        verification_status = "fix_failed"

    is_valid = bool(reproduction_confirmed and fix_verified)

    return {
        "valid": is_valid,
        "verification_status": verification_status,
        "analysis_passed": True,
        "reproduction_confirmed": reproduction_confirmed,
        "fix_verified": fix_verified,
        "reproduction_resolved": reproduction_resolved,
        "regression_suite_passed": regression_passed,
        "patched_file": str(rel_patch_file.as_posix()),
        "diff": unified_diff,
        "pre_fix": pre_fix,
        "post_fix": post_fix_res.to_dict(),
        "test_code": test_code,
        "regression_output": regression_output,
    }
