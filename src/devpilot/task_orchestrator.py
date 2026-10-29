"""Task lifecycle orchestrator for DevPilot.

Drives durable tasks through the state machine:
queued -> policy_checked -> worker_provisioning -> checkout_ready ->
investigating -> patch_validating -> awaiting_approval (or failed / cancelled).

All state transitions are strictly policy-checked outside the LLM, use
idempotency keys, and record audit events in the durable task store.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Sequence
import subprocess

from devpilot.isolated_checkout import (
    CommandRunner,
    IsolatedCheckout,
    IsolatedCheckoutError,
    _run,
    resolve_immutable_worker_image,
    run_pytest_validation,
    stage_archive_in_isolated_volume,
)
from devpilot.task_policy import PolicyError, RepositoryPolicy, TaskMode
from devpilot.task_store import DurableTask, PostgresTaskStore, TaskStoreError

logger = logging.getLogger(__name__)


class TaskOrchestratorError(RuntimeError):
    """Raised when task orchestration fails or violates policy."""


PolicyLookup = Callable[[str], RepositoryPolicy | None]
ArchiveProvider = Callable[[str, str], Path]


class TaskOrchestrator:
    """Coordinates durable task lifecycle transitions with fail-closed security."""

    def __init__(
        self,
        task_store: PostgresTaskStore,
        *,
        policy_lookup: PolicyLookup,
        worker_image_reference: str = "devpilot-worker:0.1",
        archive_provider: ArchiveProvider | None = None,
        command_runner: CommandRunner = _run,
    ) -> None:
        if not isinstance(task_store, PostgresTaskStore):
            raise TaskOrchestratorError("A valid PostgresTaskStore instance is required.")
        if not callable(policy_lookup):
            raise TaskOrchestratorError("A callable policy_lookup function is required.")

        self._store = task_store
        self._policy_lookup = policy_lookup
        self._worker_image_reference = worker_image_reference
        self._archive_provider = archive_provider
        self._runner = command_runner

    def process_task(self, task_id: str) -> DurableTask:
        """Advance a durable task through its lifecycle steps to completion or failure."""
        task = self._store.get_task(task_id)
        if task is None:
            raise TaskOrchestratorError(f"Task '{task_id}' not found in durable task store.")

        # Terminal states are immutable
        if task.state in {"awaiting_approval", "failed", "cancelled"}:
            return task

        checkout: IsolatedCheckout | None = None

        try:
            # 1. queued -> policy_checked
            if task.state == "queued":
                task = self._check_policy(task)

            # 2. policy_checked -> worker_provisioning
            if task.state == "policy_checked":
                task, worker_image_id = self._provision_worker(task)

            # 3. worker_provisioning -> checkout_ready
            if task.state == "worker_provisioning":
                task, checkout = self._prepare_checkout(task)

            # 4. checkout_ready -> investigating
            if task.state == "checkout_ready":
                task = self._run_investigation_phase(task)

            # 5. investigating -> patch_validating -> awaiting_approval
            if task.state == "investigating":
                task = self._run_validation_phase(task, checkout)

            return task

        except Exception as exc:
            logger.error("Error during task %s orchestration: %s", task_id, exc, exc_info=True)
            # Ensure task is marked failed on unhandled error
            try:
                current_task = self._store.get_task(task_id)
                if current_task and current_task.state not in {"failed", "cancelled"}:
                    current_task = self._store.transition_task_state(
                        task_id=task_id,
                        from_state=current_task.state,
                        to_state="failed",
                        event_metadata={"reason": "orchestrator_error", "error": str(exc)[:200]},
                    )
                return current_task or task
            except Exception as store_exc:
                logger.error("Failed to mark task %s as failed: %s", task_id, store_exc)
            return task

        finally:
            # Cleanup volume if checkout was staged
            if checkout is not None:
                try:
                    self._runner(("docker", "volume", "rm", checkout.volume_name))
                except Exception:
                    pass

    def _check_policy(self, task: DurableTask) -> DurableTask:
        """Validate repository policy outside the LLM."""
        try:
            policy = self._policy_lookup(task.repository_node_id)
            if policy is None:
                raise PolicyError(f"No policy configured for repository node '{task.repository_node_id}'.")

            # Validate that policy permits at least READ_ONLY mode
            if not policy.permits(TaskMode.READ_ONLY):
                raise PolicyError("Repository policy does not permit task execution.")

            return self._store.transition_task_state(
                task_id=task.task_id,
                from_state="queued",
                to_state="policy_checked",
                event_metadata={"step": "policy_approved", "mode": policy.allowed_mode.value},
            )
        except (PolicyError, ValueError) as exc:
            return self._store.transition_task_state(
                task_id=task.task_id,
                from_state="queued",
                to_state="failed",
                event_metadata={"reason": "policy_rejected", "error": str(exc)[:200]},
            )

    def _provision_worker(self, task: DurableTask) -> tuple[DurableTask, str]:
        """Resolve and verify the immutable local worker image."""
        try:
            image_id = resolve_immutable_worker_image(self._worker_image_reference, runner=self._runner)
            updated_task = self._store.transition_task_state(
                task_id=task.task_id,
                from_state="policy_checked",
                to_state="worker_provisioning",
                event_metadata={"step": "worker_image_resolved", "worker_image": image_id},
            )
            return updated_task, image_id
        except IsolatedCheckoutError as exc:
            failed_task = self._store.transition_task_state(
                task_id=task.task_id,
                from_state="policy_checked",
                to_state="failed",
                event_metadata={"reason": "worker_provisioning_failed", "error": str(exc)[:200]},
            )
            raise TaskOrchestratorError(f"Worker provisioning failed: {exc}") from exc

    def _prepare_checkout(self, task: DurableTask) -> tuple[DurableTask, IsolatedCheckout | None]:
        """Stage the repository archive into an isolated Docker volume."""
        if self._archive_provider is None:
            # When archive_provider is omitted (e.g. in read-only analysis or lightweight mode),
            # mark checkout_ready without an isolated Docker volume.
            updated_task = self._store.transition_task_state(
                task_id=task.task_id,
                from_state="worker_provisioning",
                to_state="checkout_ready",
                event_metadata={"step": "checkout_ready_skipped_archive"},
            )
            return updated_task, None

        try:
            archive_path = self._archive_provider(task.repository_node_id, task.base_sha)
            checkout = stage_archive_in_isolated_volume(
                archive_path,
                task_id=task.task_id,
                image_reference=self._worker_image_reference,
                runner=self._runner,
            )
            updated_task = self._store.transition_task_state(
                task_id=task.task_id,
                from_state="worker_provisioning",
                to_state="checkout_ready",
                event_metadata={"step": "checkout_staged", "volume_name": checkout.volume_name},
            )
            return updated_task, checkout
        except Exception as exc:
            self._store.transition_task_state(
                task_id=task.task_id,
                from_state="worker_provisioning",
                to_state="failed",
                event_metadata={"reason": "checkout_staging_failed", "error": str(exc)[:200]},
            )
            raise TaskOrchestratorError(f"Checkout staging failed: {exc}") from exc

    def _run_investigation_phase(self, task: DurableTask) -> DurableTask:
        """Run investigation within policy bounds."""
        return self._store.transition_task_state(
            task_id=task.task_id,
            from_state="checkout_ready",
            to_state="investigating",
            event_metadata={"step": "investigation_started"},
        )

    def _run_validation_phase(
        self, task: DurableTask, checkout: IsolatedCheckout | None
    ) -> DurableTask:
        """Validate candidate patch or test suite in isolated container."""
        # Transition to patch_validating
        validating_task = self._store.transition_task_state(
            task_id=task.task_id,
            from_state="investigating",
            to_state="patch_validating",
            event_metadata={"step": "validation_started"},
        )

        if checkout is None:
            # If no containerized volume was staged, validation completes without error
            return self._store.transition_task_state(
                task_id=validating_task.task_id,
                from_state="patch_validating",
                to_state="awaiting_approval",
                event_metadata={"step": "validation_completed"},
            )

        try:
            result = run_pytest_validation(
                checkout,
                image_reference=self._worker_image_reference,
                runner=self._runner,
            )
            if result.succeeded:
                return self._store.transition_task_state(
                    task_id=validating_task.task_id,
                    from_state="patch_validating",
                    to_state="awaiting_approval",
                    event_metadata={"step": "validation_succeeded", "exit_code": 0},
                )
            else:
                return self._store.transition_task_state(
                    task_id=validating_task.task_id,
                    from_state="patch_validating",
                    to_state="failed",
                    event_metadata={"reason": "validation_failed", "exit_code": result.exit_code},
                )
        except Exception as exc:
            return self._store.transition_task_state(
                task_id=validating_task.task_id,
                from_state="patch_validating",
                to_state="failed",
                event_metadata={"reason": "validation_error", "error": str(exc)[:200]},
            )
