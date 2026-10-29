"""Network-isolated, read-only repository archive staging for DevPilot.

This module deliberately does *not* execute repository code.  It receives an
already-downloaded GitHub archive and unpacks it using a fixed Python program
inside a tightly constrained Docker container.  The result is a named Docker
volume; no host directory or Docker socket is mounted into the container.
"""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


class IsolatedCheckoutError(RuntimeError):
    """Raised when a repository archive cannot be staged safely."""


MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_IMAGE_ID = re.compile(r"^sha256:[a-f0-9]{64}$")
_TASK_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass(frozen=True)
class IsolatedCheckout:
    """A staged source snapshot; the named volume is managed by DevPilot."""

    task_id: str
    volume_name: str
    image_id: str


@dataclass(frozen=True)
class ValidationResult:
    """Bounded result from the fixed pytest validation command."""

    task_id: str
    exit_code: int
    succeeded: bool


_EXTRACT_ARCHIVE_PROGRAM = r'''
import pathlib
import stat
import zipfile

archive = pathlib.Path("/repository.zip")
workspace = pathlib.Path("/workspace")
extraction_root = workspace / "extracted"
destination = workspace / "source"
max_members = 10_000
max_uncompressed = 512 * 1024 * 1024
total = 0

with zipfile.ZipFile(archive) as zf:
    members = zf.infolist()
    if not 1 <= len(members) <= max_members:
        raise RuntimeError("archive member count exceeds checkout limit")
    for item in members:
        member = pathlib.PurePosixPath(item.filename)
        if member.is_absolute() or ".." in member.parts or not item.filename:
            raise RuntimeError("archive contains an unsafe path")
        if stat.S_ISLNK(item.external_attr >> 16):
            raise RuntimeError("archive contains a symbolic link")
        total += item.file_size
        if total > max_uncompressed:
            raise RuntimeError("archive exceeds checkout extraction limit")
    extraction_root.mkdir(parents=True, exist_ok=False)
    for item in members:
        target = extraction_root.joinpath(*pathlib.PurePosixPath(item.filename).parts)
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(item) as source, target.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
    roots = list(extraction_root.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise RuntimeError("archive must contain one repository root directory")
    roots[0].rename(destination)
    extraction_root.rmdir()
'''


_VALIDATE_PYTEST_PROGRAM = r'''
import os
import pathlib
import shutil
import subprocess
import sys

source = pathlib.Path("/input/source")
worktree = pathlib.Path("/work/repository")
if not source.is_dir():
    raise RuntimeError("isolated source checkout is unavailable")
shutil.copytree(source, worktree, symlinks=False)
os.chdir(worktree)
result = subprocess.run(
    [sys.executable, "-m", "pytest", "-q", "--disable-warnings"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.STDOUT,
    check=False,
)
raise SystemExit(result.returncode)
'''


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False, timeout=60)


def _run_validation(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False, timeout=180)


def resolve_immutable_worker_image(image_reference: str, *, runner: CommandRunner = _run) -> str:
    """Resolve a local image reference to its exact Docker content ID.

    A production caller should supply a locally preloaded, reviewed worker
    image.  Image lookup does not pull from a registry, preventing an
    unexpected image download during task execution.
    """

    if not isinstance(image_reference, str) or not image_reference.strip():
        raise IsolatedCheckoutError("A local worker image reference is required.")
    result = runner(("docker", "image", "inspect", image_reference.strip(), "--format", "{{.Id}}"))
    if result.returncode != 0:
        raise IsolatedCheckoutError("Configured worker image is not available locally.")
    image_id = result.stdout.strip().lower()
    if not _IMAGE_ID.fullmatch(image_id):
        raise IsolatedCheckoutError("Docker did not return a valid immutable worker image ID.")
    return image_id


def stage_archive_in_isolated_volume(
    archive_path: Path,
    *,
    task_id: str,
    image_reference: str,
    runner: CommandRunner = _run,
) -> IsolatedCheckout:
    """Unpack a bounded archive in a no-network Docker loader.

    The caller must obtain the archive with a scoped, short-lived token and
    must delete its host staging file after this function returns.  This
    function never accepts a token, repository URL, shell string, or command
    supplied by a repository.
    """

    _validate_inputs(archive_path, task_id)
    image_id = resolve_immutable_worker_image(image_reference, runner=runner)
    safe_task_id = task_id.replace("-", "")
    volume_name = f"devpilot-task-{safe_task_id}"
    container_name = f"devpilot-loader-{safe_task_id}"
    volume_created = False
    container_created = False
    try:
        _require_success(runner(("docker", "volume", "create", volume_name)), "Could not create isolated task volume.")
        volume_created = True
        _require_success(
            runner((
                "docker", "run", "--rm", "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--cap-add", "CHOWN", "--user", "0:0",
                "--mount", f"type=volume,source={volume_name},target=/workspace,volume-nocopy",
                image_id, "python3", "-Ic",
                "import os; os.chown('/workspace', 10001, 10001)",
            )),
            "Could not initialize permissions for isolated task volume.",
        )
        create_command = (
            "docker", "create", "--name", container_name,
            "--network", "none", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges=true", "--pids-limit", "64",
            "--memory", "512m", "--cpus", "1.0", "--user", "10001:10001",
            "--mount", f"type=volume,source={volume_name},target=/workspace,volume-nocopy",
            image_id, "python3", "-Ic", _EXTRACT_ARCHIVE_PROGRAM,
        )
        _require_success(runner(create_command), "Could not create isolated checkout loader.")
        container_created = True
        _require_success(
            runner(("docker", "cp", str(archive_path), f"{container_name}:/repository.zip")),
            "Could not transfer source archive to isolated checkout loader.",
        )
        _require_success(
            runner(("docker", "start", "--attach", container_name)),
            "Could not safely unpack source archive in isolated checkout loader.",
        )
        return IsolatedCheckout(task_id=task_id, volume_name=volume_name, image_id=image_id)
    except Exception:
        if volume_created:
            runner(("docker", "volume", "rm", volume_name))
        raise
    finally:
        if container_created:
            runner(("docker", "rm", "--force", container_name))


def run_pytest_validation(
    checkout: IsolatedCheckout,
    *,
    image_reference: str,
    runner: CommandRunner = _run_validation,
) -> ValidationResult:
    """Run the fixed pytest command against an immutable isolated checkout."""

    if not isinstance(checkout, IsolatedCheckout) or not _TASK_ID.fullmatch(checkout.task_id):
        raise IsolatedCheckoutError("A valid isolated checkout is required for validation.")
    expected_volume = f"devpilot-task-{checkout.task_id.replace('-', '')}"
    if checkout.volume_name != expected_volume or not _IMAGE_ID.fullmatch(checkout.image_id):
        raise IsolatedCheckoutError("Isolated checkout identity is invalid.")
    image_id = resolve_immutable_worker_image(image_reference, runner=runner)
    if image_id != checkout.image_id:
        raise IsolatedCheckoutError("Worker image changed since the repository snapshot was staged.")
    container_name = f"devpilot-validator-{checkout.task_id.replace('-', '')}"
    command = (
        "docker", "run", "--rm", "--name", container_name,
        "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges=true", "--pids-limit", "128",
        "--memory", "1024m", "--cpus", "1.0", "--ulimit", "nofile=256:256",
        "--user", "10001:10001",
        "--mount", f"type=volume,source={checkout.volume_name},target=/input,readonly",
        "--tmpfs", "/work:rw,nosuid,nodev,size=768m",
        checkout.image_id, "python3", "-Ic", _VALIDATE_PYTEST_PROGRAM,
    )
    try:
        result = runner(command)
    except subprocess.TimeoutExpired as exc:
        runner(("docker", "rm", "--force", container_name))
        raise IsolatedCheckoutError("Isolated validation exceeded its time limit.") from exc
    return ValidationResult(task_id=checkout.task_id, exit_code=result.returncode, succeeded=result.returncode == 0)


def _validate_inputs(archive_path: Path, task_id: str) -> None:
    if not isinstance(archive_path, Path) or not archive_path.is_file():
        raise IsolatedCheckoutError("A downloaded repository archive file is required.")
    if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise IsolatedCheckoutError("Repository archive exceeds the 64 MiB checkout limit.")
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise IsolatedCheckoutError("Task ID must be a canonical UUID.")


def _require_success(result: subprocess.CompletedProcess[str], message: str) -> None:
    if result.returncode != 0:
        raise IsolatedCheckoutError(message)


