
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

_REGISTRY_LOCK = threading.RLock()


# Store the registry separately from the source-code tools.
# The registry records which repositories the user has connected.
REGISTRY_PATH = Path(__file__).resolve().parent / "repositories.json"
MANAGED_REPOSITORY_ROOT = Path(__file__).resolve().parent / "data" / "repositories"
_REPOSITORY_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}$")
_GITHUB_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _validate_repository_id(repository_id: str) -> None:
    """Reject empty or path-like identifiers before using them as directories."""
    if not isinstance(repository_id, str) or not _REPOSITORY_ID_RE.fullmatch(repository_id):
        raise ValueError(
            "Repository ID must be 1-100 letters, digits, dots, underscores, or hyphens."
        )


def _resolve_python_runtime(repository_path: Path, python_executable: str | None) -> str | None:
    """Accept only an explicitly chosen Python runtime inside the repository."""
    if python_executable is None:
        return None
    if not isinstance(python_executable, str) or not python_executable.strip():
        raise ValueError("Python runtime path must be a non-empty string.")

    requested = Path(python_executable).expanduser()
    candidate = (repository_path / requested if not requested.is_absolute() else requested).resolve()
    if not candidate.is_relative_to(repository_path) or not candidate.is_file():
        raise ValueError("Python runtime must be an existing file inside the registered repository.")
    if candidate.name.lower() not in {"python", "python.exe", "python3", "python3.exe"}:
        raise ValueError("Python runtime must point to a Python executable.")
    return str(candidate)


def _discover_repository_python_runtime(repository_path: Path) -> str | None:
    """Use a conventional, user-created project virtualenv when one exists."""
    candidates = (
        repository_path / ".venv" / "Scripts" / "python.exe",
        repository_path / ".venv" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def _normalise_public_github_url(remote_url: str) -> str:
    """Allow only canonical public github.com HTTPS clone URLs, without credentials."""
    if not isinstance(remote_url, str) or not remote_url.strip():
        raise ValueError("GitHub repository URL cannot be empty.")

    parsed = urlparse(remote_url.strip())
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Only public https://github.com/owner/repository URLs are supported.")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise ValueError("GitHub URL must identify exactly an owner and repository.")
    owner, repository = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not _GITHUB_NAME_RE.fullmatch(owner) or not _GITHUB_NAME_RE.fullmatch(repository):
        raise ValueError("GitHub owner and repository names contain unsupported characters.")
    return f"https://github.com/{owner}/{repository}.git"


def load_repositories() -> dict:
    """Load registered repositories from the JSON registry safely with retry support."""
    with _REGISTRY_LOCK:
        if not REGISTRY_PATH.exists():
            return {}

        last_exc = None
        for _ in range(5):
            try:
                with REGISTRY_PATH.open("r", encoding="utf-8") as file:
                    content = file.read()
                    if not content.strip():
                        time.sleep(0.04)
                        continue
                    repositories = json.loads(content)
                    if isinstance(repositories, dict):
                        return repositories
                    raise ValueError("Repository registry must contain a JSON object.")
            except (json.JSONDecodeError, OSError) as exc:
                last_exc = exc
                time.sleep(0.04)

        if last_exc:
            raise RuntimeError(
                f"Could not load repository registry: {last_exc}"
            ) from last_exc

        return {}


def save_repositories(repositories: dict) -> None:
    """Save the current repository registry to disk atomically and safely."""
    with _REGISTRY_LOCK:
        REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = REGISTRY_PATH.with_suffix(".tmp")
        try:
            with temp_path.open("w", encoding="utf-8") as file:
                json.dump(repositories, file, indent=4)
            temp_path.replace(REGISTRY_PATH)
        except Exception:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            raise


def register_local_repository(
    repository_id: str,
    name: str,
    path: str,
    python_executable: str | None = None,
    source_url: str | None = None,
) -> dict:
    """Register an existing local repository folder."""

    # Validate the supplied identifiers before changing the registry.
    _validate_repository_id(repository_id)
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Repository name cannot be empty.")

    # Resolve the path to an absolute location for consistent comparisons.
    repository_path = Path(path).expanduser().resolve()

    # Only register directories that actually exist.
    if not repository_path.is_dir():
        raise ValueError("The supplied repository path is not a directory.")

    selected_runtime = _resolve_python_runtime(repository_path, python_executable)
    if selected_runtime is None:
        selected_runtime = _discover_repository_python_runtime(repository_path)

    # Load existing registrations so new entries don't overwrite old ones.
    repositories = load_repositories()

    if repository_id in repositories:
        raise ValueError(f"Repository ID already exists: {repository_id}")

    # Save the repository's metadata, not its source-code contents.
    repository = {
        "repository_id": repository_id,
        "name": name,
        "source_type": "local",
        "location": str(repository_path),
        "status": "connected",
    }
    if selected_runtime:
        repository["python_executable"] = selected_runtime
    if source_url:
        repository["source_url"] = source_url

    repositories[repository_id] = repository
    save_repositories(repositories)

    return repository


def clone_public_github_repository(
    repository_id: str,
    name: str,
    github_url: str,
) -> dict:
    """Clone a public GitHub repository into DevPilot-owned local storage.

    Credentials in clone URLs are rejected.  The resulting checkout is treated
    exactly like a local repository; DevPilot neither stores GitHub tokens nor
    pushes changes back to GitHub.
    """
    _validate_repository_id(repository_id)
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Repository name cannot be empty.")
    clone_url = _normalise_public_github_url(github_url)

    repositories = load_repositories()
    if repository_id in repositories:
        raise ValueError(f"Repository ID already exists: {repository_id}")

    git_executable = shutil.which("git")
    if not git_executable:
        raise RuntimeError("Git is required to import a GitHub repository but was not found on PATH.")

    MANAGED_REPOSITORY_ROOT.mkdir(parents=True, exist_ok=True)
    target = (MANAGED_REPOSITORY_ROOT / repository_id).resolve()
    if not target.is_relative_to(MANAGED_REPOSITORY_ROOT.resolve()):
        raise ValueError("Repository ID resolves outside managed repository storage.")
    if target.exists():
        raise ValueError("A managed checkout with this repository ID already exists.")

    # Clone into an automatically cleaned staging folder, then publish it only
    # after git exits successfully.  No shell is involved and interactive
    # credential prompts are disabled.
    with tempfile.TemporaryDirectory(prefix="clone-", dir=str(MANAGED_REPOSITORY_ROOT)) as staging_dir:
        staged_checkout = Path(staging_dir) / "checkout"
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        clone_env = {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "PATH": os.environ.get("PATH", ""),
            "SystemRoot": system_root,
            "WINDIR": os.environ.get("WINDIR", system_root),
        }
        try:
            completed = subprocess.run(
                [
                    git_executable,
                    "-c",
                    "credential.helper=",
                    "clone",
                    "--depth",
                    "1",
                    "--",
                    clone_url,
                    str(staged_checkout),
                ],
                capture_output=True,
                text=True,
                timeout=120,
                env=clone_env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("GitHub clone timed out after 120 seconds.") from exc
        except OSError as exc:
            raise RuntimeError(f"Could not start Git: {exc}") from exc

        if completed.returncode != 0 or not staged_checkout.is_dir():
            details = (completed.stderr or completed.stdout or "unknown Git error").strip()
            raise RuntimeError(f"GitHub clone failed: {details[:500]}")

        shutil.move(str(staged_checkout), str(target))

    repository = {
        "repository_id": repository_id,
        "name": name.strip(),
        "source_type": "github_public_clone",
        "source_url": clone_url[:-4],
        "location": str(target),
        "status": "connected",
    }
    repositories[repository_id] = repository
    save_repositories(repositories)
    return repository



def get_repository(repository_id: str) -> dict | None:
    """Return a registered repository after validating its current status."""

    # Load the registry and find the requested repository ID.
    repositories = load_repositories()
    repository = repositories.get(repository_id)

    # Return None when the requested project has never been registered.
    if repository is None:
        return None

    # Every supported source type is represented by a local checkout by the
    # time investigation begins, including managed public GitHub clones.
    if repository.get("source_type") in {"local", "github_public_clone"}:
        repository_path = Path(repository["location"])
        changed = False

        # A moved or deleted project is no longer available for investigation.
        if not repository_path.is_dir():
            if repository.get("status") != "unavailable":
                repository["status"] = "unavailable"
                save_repositories(repositories)
            return repository

        runtime_path = repository.get("python_executable")
        if not runtime_path:
            runtime_path = _discover_repository_python_runtime(repository_path)
            if runtime_path:
                repository["python_executable"] = runtime_path
                changed = True
        if runtime_path and not Path(runtime_path).is_file():
            if repository.get("status") != "runtime_unavailable":
                repository["status"] = "runtime_unavailable"
                changed = True
        else:
            if repository.get("status") != "connected":
                repository["status"] = "connected"
                changed = True

        if changed:
            save_repositories(repositories)

    return repository



def list_repositories() -> list[dict]:
    """Return all registered repositories with refreshed availability."""

    # Load every repository entry from the local JSON registry.
    repositories = load_repositories()

    # Reuse get_repository() so each local path is checked for availability.
    # We copy the keys first because get_repository() saves status updates.
    repository_ids = list(repositories.keys())

    # Collect the refreshed repository records for the caller.
    result = []

    for repository_id in repository_ids:
        # Resolve the entry to refresh its connected/unavailable status.
        repository = get_repository(repository_id)

        # Skip entries removed between loading and resolving the registry.
        if repository is not None:
            result.append(repository)

    return result

