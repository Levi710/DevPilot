
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repository_registry import (
    get_repository,
    load_repositories,
    register_local_repository,
    save_repositories,
)


class TestRepositoryRegistry(unittest.TestCase):
    """Test repository registration without changing the real registry."""

    def setUp(self):
        # Give each test a temporary registry file to avoid modifying
        # the actual DevPilot repository list.
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        self.registry_file = Path(self.temp_dir.name) / "repositories.json"

        # Redirect registry operations to the temporary test file.
        self.registry_patch = patch(
            "repository_registry.REGISTRY_PATH",
            self.registry_file,
        )
        self.registry_patch.start()
        self.addCleanup(self.registry_patch.stop)

        # Create a fake project directory for local-repository tests.
        self.project_dir = Path(self.temp_dir.name) / "demo_project"
        self.project_dir.mkdir()

    def test_empty_registry(self):
        # A registry that doesn't exist should initially be empty.
        self.assertEqual(load_repositories(), {})

    def test_register_and_retrieve_repository(self):
        # Register a temporary project and verify its saved metadata.
        repository = register_local_repository(
            repository_id="demo",
            name="Demo Project",
            path=str(self.project_dir),
        )

        self.assertEqual(repository["source_type"], "local")
        self.assertEqual(repository["status"], "connected")
        self.assertEqual(
            get_repository("demo")["location"],
            str(self.project_dir.resolve()),
        )

    def test_unknown_repository(self):
        # Unknown IDs should not resolve to a repository.
        self.assertIsNone(get_repository("missing"))

    def test_duplicate_repository_id(self):
        # Duplicate IDs must not overwrite an existing registration.
        register_local_repository(
            "demo",
            "Demo Project",
            str(self.project_dir),
        )

        with self.assertRaises(ValueError):
            register_local_repository(
                "demo",
                "Another Project",
                str(self.project_dir),
            )

    def test_invalid_directory(self):
        # Reject paths that do not point to an existing directory.
        missing_dir = self.project_dir / "does_not_exist"

        with self.assertRaises(ValueError):
            register_local_repository(
                "missing",
                "Missing Project",
                str(missing_dir),
            )

    def test_save_and_reload(self):
        # Confirm registry entries survive a save-and-load cycle.
        save_repositories({"demo": {"name": "Demo Project"}})
        self.assertEqual(
            load_repositories()["demo"]["name"],
            "Demo Project",
        )

    
    def test_list_repositories(self):
        # Import here so the test uses the same patched registry path.
        from repository_registry import list_repositories

        # Register a project and verify it appears in the list.
        register_local_repository(
            "demo",
            "Demo Project",
            str(self.project_dir),
        )

        repositories = list_repositories()

        self.assertEqual(len(repositories), 1)
        self.assertEqual(repositories[0]["repository_id"], "demo")
        self.assertEqual(repositories[0]["status"], "connected")



if __name__ == "__main__":
    # Run all registry tests and show a clear pass/fail summary.
    unittest.main(verbosity=2)
