import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src" / "devpilot"))

from repository_registry import register_local_repository

fixture_path = PROJECT_ROOT / "test_fixtures" / "data_flow_bug"

register_local_repository(
    repository_id="devpilot-data-flow-test",
    name="DevPilot Data Flow Test",
    path=str(fixture_path),
)

print("Registered:", fixture_path)
