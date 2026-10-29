"""Fail-closed configuration contract for DevPilot's future GitHub App identity.

This module does not parse keys, mint JWTs, call GitHub, or clone repositories.
It only validates the administrator-supplied local prerequisites for that later
read-only phase.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class GitHubAppConfigurationError(ValueError):
    """Raised when GitHub App identity is requested without safe prerequisites."""


@dataclass(frozen=True)
class GitHubAppConfiguration:
    app_id: int
    installation_id: int
    private_key_path: Path


def load_github_app_configuration(
    environ: dict[str, str] | None = None,
    *,
    workspace_root: Path | None = None,
) -> GitHubAppConfiguration | None:
    """Return validated config only after an explicit feature opt-in.

    A private key must live outside the repository so a source checkout, patch,
    artifact, or Git commit cannot capture it. The file content is never read
    here; key parsing happens only in a later, separately reviewed phase.
    """

    values = os.environ if environ is None else environ
    enabled = values.get("DEVPILOT_GITHUB_APP_ENABLED", "").strip().lower()
    if enabled in {"", "false", "0", "no"}:
        return None
    if enabled not in {"true", "1", "yes"}:
        raise GitHubAppConfigurationError("DEVPILOT_GITHUB_APP_ENABLED must be true or false.")

    app_id = _positive_integer(values.get("DEVPILOT_GITHUB_APP_ID"), "DEVPILOT_GITHUB_APP_ID")
    installation_id = _positive_integer(
        values.get("DEVPILOT_GITHUB_INSTALLATION_ID"), "DEVPILOT_GITHUB_INSTALLATION_ID"
    )
    raw_path = values.get("DEVPILOT_GITHUB_APP_PRIVATE_KEY_PATH", "").strip()
    if not raw_path:
        raise GitHubAppConfigurationError(
            "DEVPILOT_GITHUB_APP_PRIVATE_KEY_PATH is required when GitHub App access is enabled."
        )
    key_path = Path(raw_path).expanduser()
    if not key_path.is_absolute() or key_path.suffix.lower() != ".pem" or not key_path.is_file():
        raise GitHubAppConfigurationError(
            "DEVPILOT_GITHUB_APP_PRIVATE_KEY_PATH must be an existing absolute .pem file."
        )
    resolved_key_path = key_path.resolve()
    root = (workspace_root or Path(__file__).resolve().parents[2]).resolve()
    if resolved_key_path.is_relative_to(root):
        raise GitHubAppConfigurationError(
            "GitHub App private key must be stored outside the DevPilot repository."
        )
    return GitHubAppConfiguration(app_id, installation_id, resolved_key_path)


def _positive_integer(value: str | None, variable: str) -> int:
    try:
        parsed = int((value or "").strip())
    except ValueError as exc:
        raise GitHubAppConfigurationError(f"{variable} must be a positive integer.") from exc
    if parsed < 1:
        raise GitHubAppConfigurationError(f"{variable} must be a positive integer.")
    return parsed
