"""Read-only authentication primitives for DevPilot's own GitHub App.

This phase proves the configured App identity only. It does not mint an
installation token, clone a repository, or make any GitHub write request.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import jwt

try:
    from github_app_config import GitHubAppConfiguration
except ImportError:
    from devpilot.github_app_config import GitHubAppConfiguration


GITHUB_API_BASE_URL = "https://api.github.com"
APP_JWT_LIFETIME_SECONDS = 540
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


class GitHubAppAuthenticationError(RuntimeError):
    """Safe public failure for local key or GitHub App identity checks."""


@dataclass(frozen=True)
class GitHubAppIdentity:
    app_id: int
    slug: str
    name: str


@dataclass(frozen=True)
class GitHubInstallationRepository:
    repository_id: int
    node_id: str
    full_name: str
    default_branch: str


def create_app_jwt(config: GitHubAppConfiguration, *, now: int | None = None) -> str:
    """Create a short-lived RS256 JWT; never log or persist its value."""

    issued_at = int(time.time()) if now is None else now
    try:
        private_key = config.private_key_path.read_bytes()
        return jwt.encode(
            {"iat": issued_at - 60, "exp": issued_at + APP_JWT_LIFETIME_SECONDS, "iss": str(config.app_id)},
            private_key,
            algorithm="RS256",
        )
    except Exception as exc:
        raise GitHubAppAuthenticationError("Could not create GitHub App authentication JWT.") from exc


def get_app_identity(
    config: GitHubAppConfiguration,
    *,
    opener: Callable[..., object] = urlopen,
) -> GitHubAppIdentity:
    """Perform a read-only GitHub `GET /app` identity check."""

    token = create_app_jwt(config)
    request = Request(
        f"{GITHUB_API_BASE_URL}/app",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "DevPilot-ReadOnly-Identity-Check",
        },
        method="GET",
    )
    try:
        with opener(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise GitHubAppAuthenticationError("Could not verify GitHub App identity.") from exc
    try:
        app_id = int(payload["id"])
        slug = payload["slug"]
        name = payload["name"]
    except (KeyError, TypeError, ValueError) as exc:
        raise GitHubAppAuthenticationError("GitHub returned an invalid App identity response.") from exc
    if app_id != config.app_id or not isinstance(slug, str) or not isinstance(name, str):
        raise GitHubAppAuthenticationError("GitHub App identity did not match local configuration.")
    return GitHubAppIdentity(app_id=app_id, slug=slug, name=name)


def verify_single_installation_repository(
    config: GitHubAppConfiguration,
    *,
    opener: Callable[..., object] = urlopen,
) -> GitHubInstallationRepository:
    """Mint an in-memory token and verify the installation exposes exactly one repo.

    The token is intentionally never returned, logged, persisted, or added to a
    task record. This function performs no clone or GitHub write operation.
    """

    try:
        installation_token = _mint_installation_token(config, opener=opener)
        repository_request = Request(
            f"{GITHUB_API_BASE_URL}/installation/repositories?per_page=100",
            headers=_github_headers(f"Bearer {installation_token}"),
            method="GET",
        )
        with opener(repository_request, timeout=15) as response:
            repositories_payload = json.loads(response.read().decode("utf-8"))
        repositories = repositories_payload["repositories"]
    except (HTTPError, URLError, TimeoutError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise GitHubAppAuthenticationError("Could not verify GitHub App installation access.") from exc
    if not isinstance(repositories, list) or len(repositories) != 1:
        raise GitHubAppAuthenticationError(
            "GitHub App installation must be limited to exactly one repository for this phase."
        )
    repository = repositories[0]
    try:
        result = GitHubInstallationRepository(
            repository_id=int(repository["id"]),
            node_id=repository["node_id"],
            full_name=repository["full_name"],
            default_branch=repository["default_branch"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GitHubAppAuthenticationError("GitHub returned an invalid installation repository response.") from exc
    if not all(isinstance(value, str) and value for value in (result.node_id, result.full_name, result.default_branch)):
        raise GitHubAppAuthenticationError("GitHub returned an invalid installation repository response.")
    return result


def download_repository_archive(
    config: GitHubAppConfiguration,
    repository: GitHubInstallationRepository,
    commit_sha: str,
    destination: Path,
    *,
    opener: Callable[..., object] = urlopen,
) -> None:
    """Download one bounded commit archive using an in-memory App token."""

    if not isinstance(commit_sha, str) or not _COMMIT_SHA.fullmatch(commit_sha):
        raise GitHubAppAuthenticationError("Repository archive requires a full lowercase commit SHA.")
    if not isinstance(destination, Path) or destination.exists() or not destination.parent.is_dir():
        raise GitHubAppAuthenticationError("Repository archive staging path is unavailable.")
    try:
        installation_token = _mint_installation_token(config, opener=opener)
        request = Request(
            f"{GITHUB_API_BASE_URL}/repos/{repository.full_name}/zipball/{commit_sha}",
            headers=_github_headers(f"Bearer {installation_token}"),
            method="GET",
        )
        with opener(request, timeout=30) as response:
            raw_length = response.headers.get("Content-Length")
            if raw_length is not None and int(raw_length) > MAX_ARCHIVE_BYTES:
                raise ValueError("archive too large")
            written = 0
            with destination.open("xb") as output:
                while chunk := response.read(1024 * 1024):
                    written += len(chunk)
                    if written > MAX_ARCHIVE_BYTES:
                        raise ValueError("archive too large")
                    output.write(chunk)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError) as exc:
        destination.unlink(missing_ok=True)
        raise GitHubAppAuthenticationError("Could not download bounded repository archive.") from exc


def resolve_default_branch_sha(
    config: GitHubAppConfiguration,
    repository: GitHubInstallationRepository,
    *,
    opener: Callable[..., object] = urlopen,
) -> str:
    """Read the current default-branch commit SHA from the sole installation repo."""

    try:
        installation_token = _mint_installation_token(config, opener=opener)
        request = Request(
            f"{GITHUB_API_BASE_URL}/repos/{repository.full_name}/commits/{repository.default_branch}",
            headers=_github_headers(f"Bearer {installation_token}"),
            method="GET",
        )
        with opener(request, timeout=15) as response:
            commit_sha = json.loads(response.read().decode("utf-8"))["sha"]
    except (HTTPError, URLError, TimeoutError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise GitHubAppAuthenticationError("Could not resolve the configured repository default branch.") from exc
    if not isinstance(commit_sha, str) or not _COMMIT_SHA.fullmatch(commit_sha):
        raise GitHubAppAuthenticationError("GitHub returned an invalid default-branch commit SHA.")
    return commit_sha


def _mint_installation_token(
    config: GitHubAppConfiguration,
    *,
    opener: Callable[..., object],
) -> str:
    app_jwt = create_app_jwt(config)
    token_request = Request(
        f"{GITHUB_API_BASE_URL}/app/installations/{config.installation_id}/access_tokens",
        data=b"{}",
        headers=_github_headers(f"Bearer {app_jwt}"),
        method="POST",
    )
    try:
        with opener(token_request, timeout=15) as response:
            token_payload = json.loads(response.read().decode("utf-8"))
        installation_token = token_payload["token"]
        if not isinstance(installation_token, str) or not installation_token:
            raise ValueError("missing token")
        return installation_token
    except (HTTPError, URLError, TimeoutError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise GitHubAppAuthenticationError("Could not mint a GitHub App installation token.") from exc


def _github_headers(authorization: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": authorization,
        "X-GitHub-Api-Version": "2026-03-10",
        "User-Agent": "DevPilot-ReadOnly-Identity-Check",
    }
