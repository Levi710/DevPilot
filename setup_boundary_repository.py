import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src" / "devpilot"))

from repository_registry import register_local_repository

repo_path = PROJECT_ROOT / "test_fixtures" / "boundary_bug"

result = register_local_repository(
    repository_id="devpilot-boundary-test",
    name="DevPilot Boundary Bug Test",
    path=str(repo_path),
)

print(result)
