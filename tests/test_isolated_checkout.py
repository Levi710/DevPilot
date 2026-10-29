from pathlib import Path
import subprocess
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from devpilot.isolated_checkout import (
    IsolatedCheckout,
    IsolatedCheckoutError,
    resolve_immutable_worker_image,
    run_pytest_validation,
    stage_archive_in_isolated_volume,
)


TASK_ID = "b16305f2-0ce0-42a7-919d-d019b196e724"
IMAGE_ID = "sha256:" + "a" * 64


def _result(command, returncode=0, stdout=""):
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")


def test_resolve_worker_image_uses_docker_content_id_not_mutable_tag():
    calls = []
    def runner(command):
        calls.append(command)
        return _result(command, stdout=IMAGE_ID + "\n")

    assert resolve_immutable_worker_image("reviewed-worker:1", runner=runner) == IMAGE_ID
    assert calls == [("docker", "image", "inspect", "reviewed-worker:1", "--format", "{{.Id}}")]


def test_stage_uses_no_network_no_host_mounts_and_fixed_extractor(tmp_path):
    archive = tmp_path / "source.zip"
    archive.write_bytes(b"zip")
    calls = []
    def runner(command):
        calls.append(command)
        if command[:3] == ("docker", "image", "inspect"):
            return _result(command, stdout=IMAGE_ID + "\n")
        return _result(command)

    checkout = stage_archive_in_isolated_volume(
        archive, task_id=TASK_ID, image_reference="reviewed-worker:1", runner=runner
    )
    create = next(command for command in calls if command[:2] == ("docker", "create"))
    assert checkout.image_id == IMAGE_ID
    assert "--network" in create and create[create.index("--network") + 1] == "none"
    assert "--cap-drop" in create and create[create.index("--cap-drop") + 1] == "ALL"
    assert "--user" in create and create[create.index("--user") + 1] == "10001:10001"
    assert "--mount" in create
    assert "type=volume" in create[create.index("--mount") + 1]
    assert all("type=bind" not in part for part in create)
    assert create[-3:] == ("python3", "-Ic", create[-1])
    assert "zipfile.ZipFile" in create[-1]
    assert "archive must contain one repository root directory" in create[-1]
    assert calls[-1][:3] == ("docker", "rm", "--force")
    initializer = next(command for command in calls if command[:3] == ("docker", "run", "--rm"))
    assert initializer[initializer.index("--network") + 1] == "none"
    assert initializer[initializer.index("--cap-add") + 1] == "CHOWN"


def test_stage_rejects_non_uuid_and_never_invokes_docker(tmp_path):
    archive = tmp_path / "source.zip"
    archive.write_bytes(b"zip")
    with pytest.raises(IsolatedCheckoutError, match="canonical UUID"):
        stage_archive_in_isolated_volume(archive, task_id="not-a-task", image_reference="image", runner=lambda _c: None)


def test_stage_removes_volume_after_failed_extraction(tmp_path):
    archive = tmp_path / "source.zip"
    archive.write_bytes(b"zip")
    calls = []
    def runner(command):
        calls.append(command)
        if command[:3] == ("docker", "image", "inspect"):
            return _result(command, stdout=IMAGE_ID + "\n")
        if command[:3] == ("docker", "start", "--attach"):
            return _result(command, returncode=1)
        return _result(command)
    with pytest.raises(IsolatedCheckoutError, match="safely unpack"):
        stage_archive_in_isolated_volume(archive, task_id=TASK_ID, image_reference="image", runner=runner)
    assert any(command[:3] == ("docker", "volume", "rm") for command in calls)


def test_validation_uses_fixed_networkless_readonly_pytest_command():
    calls = []
    checkout = IsolatedCheckout(TASK_ID, "devpilot-task-b16305f20ce042a7919dd019b196e724", IMAGE_ID)
    def runner(command):
        calls.append(command)
        if command[:3] == ("docker", "image", "inspect"):
            return _result(command, stdout=IMAGE_ID + "\n")
        return _result(command)

    result = run_pytest_validation(checkout, image_reference="reviewed-worker:1", runner=runner)
    command = calls[-1]
    assert result.succeeded is True
    assert command[0:2] == ("docker", "run")
    assert command[command.index("--network") + 1] == "none"
    assert "--read-only" in command
    assert "type=volume" in command[command.index("--mount") + 1]
    assert "readonly" in command[command.index("--mount") + 1]
    assert "type=bind" not in command
    assert command[-3:] == ("python3", "-Ic", command[-1])
    assert "pytest" in command[-1]
    assert "DEVNULL" in command[-1]


def test_validation_rejects_image_change_since_checkout():
    checkout = IsolatedCheckout(TASK_ID, "devpilot-task-b16305f20ce042a7919dd019b196e724", IMAGE_ID)
    changed = "sha256:" + "b" * 64
    def runner(command): return _result(command, stdout=changed + "\n")
    with pytest.raises(IsolatedCheckoutError, match="changed"):
        run_pytest_validation(checkout, image_reference="reviewed-worker:1", runner=runner)
