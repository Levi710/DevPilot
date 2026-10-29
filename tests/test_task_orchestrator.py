"""Unit tests for the Task Lifecycle Orchestrator."""

from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import uuid
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from devpilot.isolated_checkout import IsolatedCheckout, ValidationResult
from devpilot.task_orchestrator import TaskOrchestrator, TaskOrchestratorError
from devpilot.task_policy import RepositoryPolicy, TaskMode
from devpilot.task_store import DurableTask, PostgresTaskStore, TaskStoreError

TASK_ID = "b16305f2-0ce0-42a7-919d-d019b196e724"
REPO_NODE_ID = "R_kgDODevPilot"
IMAGE_ID = "sha256:" + "a" * 64


class MockTaskStore(PostgresTaskStore):
    """In-memory mock store for testing orchestrator lifecycle state transitions."""

    def __init__(self, initial_task: DurableTask | None = None):
        self._task = initial_task
        self.transitions: list[tuple[str, str, dict]] = []

    def get_task(self, task_id: str) -> DurableTask | None:
        if self._task and self._task.task_id == task_id:
            return self._task
        return None

    def transition_task_state(
        self,
        *,
        task_id: str,
        from_state: str,
        to_state: str,
        idempotency_key: str | None = None,
        event_metadata: dict | None = None,
    ) -> DurableTask:
        if not self._task or self._task.task_id != task_id:
            raise TaskStoreError(f"Task {task_id} not found")
        assert self._task.state == from_state, f"Expected state {from_state}, found {self._task.state}"
        self.transitions.append((from_state, to_state, event_metadata or {}))
        self._task = DurableTask(
            task_id=self._task.task_id,
            source=self._task.source,
            repository_node_id=self._task.repository_node_id,
            base_sha=self._task.base_sha,
            state=to_state,
            created_at=self._task.created_at,
        )
        return self._task


def _make_task(state: str = "queued") -> DurableTask:
    return DurableTask(
        task_id=TASK_ID,
        source="webhook",
        repository_node_id=REPO_NODE_ID,
        base_sha="f" * 40,
        state=state,
        created_at=datetime.now(timezone.utc),
    )


def _make_policy() -> RepositoryPolicy:
    return RepositoryPolicy(
        repository_node_id=REPO_NODE_ID,
        allowed_paths=("src", "tests"),
        test_command=("pytest", "-q"),
        dependency_recipe="requirements.txt",
        allowed_mode=TaskMode.VALIDATE_PATCH,
    )


def test_orchestrator_initialization_requires_valid_store_and_policy():
    with pytest.raises(TaskOrchestratorError, match="valid PostgresTaskStore"):
        TaskOrchestrator(None, policy_lookup=lambda _: None)

    store = MockTaskStore(_make_task())
    with pytest.raises(TaskOrchestratorError, match="callable policy_lookup"):
        TaskOrchestrator(store, policy_lookup=None)


def test_orchestrator_raises_on_nonexistent_task():
    store = MockTaskStore(None)
    orchestrator = TaskOrchestrator(store, policy_lookup=lambda _: _make_policy())
    with pytest.raises(TaskOrchestratorError, match="not found"):
        orchestrator.process_task(str(uuid.uuid4()))


def test_orchestrator_ignores_terminal_states():
    for terminal in ("awaiting_approval", "failed", "cancelled"):
        store = MockTaskStore(_make_task(state=terminal))
        orchestrator = TaskOrchestrator(store, policy_lookup=lambda _: _make_policy())
        result = orchestrator.process_task(TASK_ID)
        assert result.state == terminal
        assert store.transitions == []


def test_orchestrator_fails_unapproved_repository_policy():
    store = MockTaskStore(_make_task("queued"))
    orchestrator = TaskOrchestrator(store, policy_lookup=lambda _: None)
    result = orchestrator.process_task(TASK_ID)
    assert result.state == "failed"
    assert len(store.transitions) == 1
    assert store.transitions[0][0] == "queued"
    assert store.transitions[0][1] == "failed"
    assert store.transitions[0][2]["reason"] == "policy_rejected"


def test_orchestrator_fails_when_worker_image_resolution_fails():
    store = MockTaskStore(_make_task("queued"))
    calls = []

    def failing_runner(cmd):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="Image not found")

    orchestrator = TaskOrchestrator(
        store,
        policy_lookup=lambda _: _make_policy(),
        command_runner=failing_runner,
    )
    result = orchestrator.process_task(TASK_ID)
    assert result.state == "failed"
    states = [t[1] for t in store.transitions]
    assert "policy_checked" in states
    assert states[-1] == "failed"


def test_orchestrator_drives_full_lifecycle_to_awaiting_approval(tmp_path):
    archive = tmp_path / "repo.zip"
    archive.write_bytes(b"PK\x05\x06" + b"\x00" * 18)  # minimal empty zip header

    store = MockTaskStore(_make_task("queued"))
    calls = []

    def mock_runner(cmd):
        calls.append(cmd)
        if cmd[:3] == ("docker", "image", "inspect"):
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=IMAGE_ID + "\n", stderr="")
        if cmd[:3] == ("docker", "volume", "create"):
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="vol\n", stderr="")
        if cmd[:2] == ("docker", "run") and cmd[-1] == "python3 -Ic _VALIDATE_PYTEST_PROGRAM":
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    orchestrator = TaskOrchestrator(
        store,
        policy_lookup=lambda _: _make_policy(),
        archive_provider=lambda _repo, _sha: archive,
        command_runner=mock_runner,
    )

    # Monkeypatch stage_archive_in_isolated_volume and run_pytest_validation for unit test
    from devpilot import task_orchestrator

    orig_stage = task_orchestrator.stage_archive_in_isolated_volume
    orig_validate = task_orchestrator.run_pytest_validation

    try:
        task_orchestrator.stage_archive_in_isolated_volume = lambda *args, **kwargs: IsolatedCheckout(
            task_id=TASK_ID,
            volume_name=f"devpilot-task-{TASK_ID.replace('-', '')}",
            image_id=IMAGE_ID,
        )
        task_orchestrator.run_pytest_validation = lambda *args, **kwargs: ValidationResult(
            task_id=TASK_ID,
            exit_code=0,
            succeeded=True,
        )

        result = orchestrator.process_task(TASK_ID)
        assert result.state == "awaiting_approval"

        states = [t[1] for t in store.transitions]
        assert states == [
            "policy_checked",
            "worker_provisioning",
            "checkout_ready",
            "investigating",
            "patch_validating",
            "awaiting_approval",
        ]
    finally:
        task_orchestrator.stage_archive_in_isolated_volume = orig_stage
        task_orchestrator.run_pytest_validation = orig_validate


def test_orchestrator_marks_task_failed_when_validation_fails(tmp_path):
    archive = tmp_path / "repo.zip"
    archive.write_bytes(b"PK")

    store = MockTaskStore(_make_task("queued"))

    def mock_runner(cmd):
        if cmd[:3] == ("docker", "image", "inspect"):
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=IMAGE_ID + "\n", stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    orchestrator = TaskOrchestrator(
        store,
        policy_lookup=lambda _: _make_policy(),
        archive_provider=lambda _repo, _sha: archive,
        command_runner=mock_runner,
    )

    from devpilot import task_orchestrator

    orig_stage = task_orchestrator.stage_archive_in_isolated_volume
    orig_validate = task_orchestrator.run_pytest_validation

    try:
        task_orchestrator.stage_archive_in_isolated_volume = lambda *args, **kwargs: IsolatedCheckout(
            task_id=TASK_ID,
            volume_name=f"devpilot-task-{TASK_ID.replace('-', '')}",
            image_id=IMAGE_ID,
        )
        task_orchestrator.run_pytest_validation = lambda *args, **kwargs: ValidationResult(
            task_id=TASK_ID,
            exit_code=1,
            succeeded=False,
        )

        result = orchestrator.process_task(TASK_ID)
        assert result.state == "failed"
        assert store.transitions[-1][1] == "failed"
        assert store.transitions[-1][2]["reason"] == "validation_failed"
    finally:
        task_orchestrator.stage_archive_in_isolated_volume = orig_stage
        task_orchestrator.run_pytest_validation = orig_validate
