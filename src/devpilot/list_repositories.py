
# Import the registry function that lists registered projects.
from repository_registry import list_repositories


def main():
    """Display all registered repositories in a readable format."""

    # Retrieve repository records, including refreshed availability.
    repositories = list_repositories()

    # Give a clear message when the registry has no entries.
    if not repositories:
        print("No repositories registered yet.")
        return

    print("\nRegistered repositories:\n")

    # Show the metadata needed to identify and select each project.
    for repository in repositories:
        print(f"ID:     {repository['repository_id']}")
        print(f"Name:   {repository['name']}")
        print(f"Type:   {repository['source_type']}")
        print(f"Path:   {repository['location']}")
        print(f"Status: {repository['status']}")
        print("-" * 40)


if __name__ == "__main__":
    # Run the listing only when this file is executed directly.
    main()
