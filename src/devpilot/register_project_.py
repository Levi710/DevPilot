
from pathlib import Path

# Import the function responsible for registering local repositories.
from repository_registry import register_local_repository


def main():
    """Register the current DevPilot project in the local registry."""

    # Resolve the project root from this script's location.
    # This avoids depending on which directory PowerShell is in.
    project_root = Path(__file__).resolve().parents[2]

    # Register this project under a stable ID that the agent can use.
    repository = register_local_repository(
        repository_id="devpilot",
        name="DevPilot",
        path=str(project_root),
    )

    # Display the saved metadata so we can verify the registration.
    print("Repository registered successfully!")
    print(f"ID: {repository['repository_id']}")
    print(f"Name: {repository['name']}")
    print(f"Type: {repository['source_type']}")
    print(f"Path: {repository['location']}")
    print(f"Status: {repository['status']}")


if __name__ == "__main__":
    # Run the registration only when this script is executed directly.
    main()
