import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DEV_PILOT_DIR = PROJECT_ROOT / "src" / "devpilot"

sys.path.insert(0, str(DEV_PILOT_DIR))

from repository_registry import register_local_repository

fixture_path = PROJECT_ROOT / "test_fixtures" / "known_bug"

register_local_repository(
    repository_id="devpilot-known-bug-test",
    name="DevPilot Known Bug Test",
    path=str(fixture_path),
)

print("Test repository registered:", fixture_path)
