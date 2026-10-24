
import json
from pathlib import Path


# Store the registry separately from the source-code tools.
# The registry records which repositories the user has connected.
REGISTRY_PATH = Path(__file__).resolve().parent / "repositories.json"


def load_repositories() -> dict:
    """Load registered repositories from the JSON registry."""

    # A missing registry means no repositories have been connected yet.
    if not REGISTRY_PATH.exists():
        return {}

    # Read the saved JSON data and convert it into a Python dictionary.
    try:
        with REGISTRY_PATH.open("r", encoding="utf-8") as file:
            repositories = json.load(file)
    except (json.JSONDecodeError, OSError) as exc:
        # Don't silently treat a corrupted registry as an empty one.
        raise RuntimeError(
            f"Could not load repository registry: {exc}"
        ) from exc

    # Reject unexpected data shapes before other functions use the registry.
    if not isinstance(repositories, dict):
        raise ValueError("Repository registry must contain a JSON object.")

    return repositories


def save_repositories(repositories: dict) -> None:
    """Save the current repository registry to disk."""

    # Ensure the registry's parent directory exists before writing.
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Write readable JSON so the user can inspect the registry manually.
    with REGISTRY_PATH.open("w", encoding="utf-8") as file:
        json.dump(repositories, file, indent=4)


def register_local_repository(
    repository_id: str,
    name: str,
    path: str,
) -> dict:
    """Register an existing local repository folder."""

    # Validate the supplied identifiers before changing the registry.
    if not repository_id.strip() or not name.strip():
        raise ValueError("Repository ID and name cannot be empty.")

    # Resolve the path to an absolute location for consistent comparisons.
    repository_path = Path(path).expanduser().resolve()

    # Only register directories that actually exist.
    if not repository_path.is_dir():
        raise ValueError("The supplied repository path is not a directory.")

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

    # Local repositories must still exist at their registered locations.
    if repository.get("source_type") == "local":
        repository_path = Path(repository["location"])

        # A moved or deleted project is no longer available for investigation.
        if not repository_path.is_dir():
            repository["status"] = "unavailable"
            save_repositories(repositories)
            return repository

        # Refresh the status when a previously unavailable folder returns.
        repository["status"] = "connected"
        save_repositories(repositories)

    # GitHub availability checks will be implemented with the GitHub connector.
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

