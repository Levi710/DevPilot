"""Policy contracts for future durable DevPilot tasks.

Policies are evaluated independently from the LLM.  Repository and issue text
may inform evidence, but cannot grant capabilities through this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath


class TaskMode(StrEnum):
    READ_ONLY = "read_only"
    VALIDATE_PATCH = "validate_patch"
    OPEN_PULL_REQUEST = "open_pull_request"
    MERGE = "merge"
    DEPLOY = "deploy"


class PolicyError(ValueError):
    """Raised when an administrator-authored repository policy is invalid."""


@dataclass(frozen=True)
class RepositoryPolicy:
    """Administrator-authored limits for one repository.

    The initial production policy intentionally stops at validation.  Future
    write capabilities must be added only by a separately reviewed migration.
    """

    repository_node_id: str
    allowed_paths: tuple[str, ...]
    test_command: tuple[str, ...]
    dependency_recipe: str
    max_task_seconds: int = 900
    max_model_iterations: int = 10
    max_changed_files: int = 10
    allowed_mode: TaskMode = TaskMode.VALIDATE_PATCH

    def __post_init__(self) -> None:
        if not self.repository_node_id.strip():
            raise PolicyError("repository_node_id is required.")
        if not self.allowed_paths:
            raise PolicyError("At least one allowed path is required.")
        for path in self.allowed_paths:
            _validate_relative_path(path)
        if not self.test_command or any(not item.strip() for item in self.test_command):
            raise PolicyError("test_command must be a non-empty argument list.")
        if not self.dependency_recipe.strip():
            raise PolicyError("dependency_recipe is required.")
        if not 1 <= self.max_task_seconds <= 3600:
            raise PolicyError("max_task_seconds must be between 1 and 3600.")
        if not 1 <= self.max_model_iterations <= 20:
            raise PolicyError("max_model_iterations must be between 1 and 20.")
        if not 1 <= self.max_changed_files <= 100:
            raise PolicyError("max_changed_files must be between 1 and 100.")
        if self.allowed_mode not in {TaskMode.READ_ONLY, TaskMode.VALIDATE_PATCH}:
            raise PolicyError("Phase 0 policy cannot authorize GitHub write, merge, or deployment actions.")

    def permits_path(self, repository_relative_path: str) -> bool:
        """Return whether a normalized repository path is administrator-approved."""

        try:
            candidate = _validate_relative_path(repository_relative_path)
        except PolicyError:
            return False
        return any(candidate == root or candidate.startswith(f"{root}/") for root in self.allowed_paths)

    def permits(self, requested_mode: TaskMode) -> bool:
        """Capabilities are ordered; no model-provided input can elevate them."""

        order = {
            TaskMode.READ_ONLY: 0,
            TaskMode.VALIDATE_PATCH: 1,
            TaskMode.OPEN_PULL_REQUEST: 2,
            TaskMode.MERGE: 3,
            TaskMode.DEPLOY: 4,
        }
        return order[requested_mode] <= order[self.allowed_mode]


def _validate_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyError("Paths must be non-empty repository-relative strings.")
    # Policies use portable POSIX repository paths only. Reject Windows syntax
    # before converting to a path object because it is otherwise ordinary text
    # to PurePosixPath and could become traversal in a Windows worker later.
    if "\\" in value or ":" in value:
        raise PolicyError("Paths must use normalized POSIX repository-relative syntax.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
        raise PolicyError("Paths must be normalized and remain inside the repository.")
    return path.as_posix().rstrip("/")
