
# Import the registry functions used to display and validate projects.
from repository_registry import list_repositories, get_repository

# Import the agent entry point so we can launch an investigation.
from agent import test_tool_call


def main():
    """Let the user choose a registered repository for investigation."""

    # Load repository records and refresh their availability status.
    repositories = list_repositories()

    if not repositories:
        print("No repositories registered. Register one first.")
        return

    print("\nAvailable repositories:\n")

    # Display each repository ID so the user knows what to enter.
    for repository in repositories:
        print(
            f"- {repository['repository_id']} "
            f"({repository['name']}) "
            f"[{repository['source_type']}, {repository['status']}]"
        )

    # Ask for an ID rather than accepting a filesystem path from the user.
    repository_id = input("\nEnter repository ID: ").strip()

    # Resolve the ID through the registry instead of trusting the input as a path.
    repository = get_repository(repository_id)

    if repository is None:
        print(f"Unknown repository ID: {repository_id}")
        return

    # Stop if the repository exists in the registry but is unavailable.
    if repository["status"] != "connected":
        print(f"Repository '{repository_id}' is unavailable.")
        print("Check its location and reconnect it before continuing.")
        return

    # This version of the agent can investigate local repositories only.
    if repository["source_type"] != "local":
        print("This version supports local repositories only.")
        return

    print(f"\nStarting investigation in: {repository['name']}")

    # Pass the registered ID to the agent; it resolves the path itself.
    test_tool_call(repository_id=repository_id)


if __name__ == "__main__":
    # Run the selection workflow only when this script is executed directly.
    main()
