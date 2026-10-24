
"""Security and correctness tests for DevPilot repository tools."""

from pathlib import Path

import pytest

import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
DEV_PILOT_DIR = SRC_DIR / "devpilot"

sys.path.insert(0, str(DEV_PILOT_DIR))
sys.path.insert(0, str(SRC_DIR))

import tools


@pytest.fixture
def repository(tmp_path):
    """Create an isolated temporary repository for each test."""
    return tmp_path


# ---------------------------------------------------------
# read_file: path containment and access restrictions
# ---------------------------------------------------------

def test_read_file_blocks_path_traversal(repository):
    """A relative path must not escape the selected repository."""
    outside_file = repository.parent / "outside_secret.txt"
    outside_file.write_text("do not expose this", encoding="utf-8")

    result = tools.read_file(
        "../outside_secret.txt",
        repo_root=str(repository),
    )

    assert result == "Error: path is outside the repository."
    assert "do not expose this" not in result


def test_read_file_blocks_absolute_paths(repository):
    """Callers must provide repository-relative paths."""
    result = tools.read_file(
        str(repository / "example.py"),
        repo_root=str(repository),
    )

    assert result == "Error: use a repository-relative path."


@pytest.mark.parametrize(
    "directory",
    [".git", ".venv", "__pycache__", "node_modules"],
)
def test_read_file_blocks_excluded_directories(repository, directory):
    """Files under excluded directories must not be readable."""
    excluded_dir = repository / directory
    excluded_dir.mkdir()
    (excluded_dir / "example.py").write_text(
        "private generated content",
        encoding="utf-8",
    )

    result = tools.read_file(
        f"{directory}/example.py",
        repo_root=str(repository),
    )

    assert result == "Error: access to excluded directories is blocked."
    assert "private generated content" not in result


@pytest.mark.parametrize(
    "filename",
    [".env", ".env.local", ".env.production", "credentials.json"],
)
def test_read_file_blocks_sensitive_filenames(repository, filename):
    """Known secret/configuration filenames must not be exposed."""
    secret_file = repository / filename
    secret_file.write_text("fake-test-secret", encoding="utf-8")

    result = tools.read_file(filename, repo_root=str(repository))

    assert result == "Error: access to sensitive files is blocked."
    assert "fake-test-secret" not in result


def test_read_file_blocks_sensitive_names_case_insensitively(repository):
    """Sensitive filename checks should work with Windows-style casing."""
    secret_file = repository / ".ENV"
    secret_file.write_text("fake-test-secret", encoding="utf-8")

    result = tools.read_file(".ENV", repo_root=str(repository))

    assert result == "Error: access to sensitive files is blocked."
    assert "fake-test-secret" not in result


def test_read_file_returns_verified_line_numbers(repository):
    """Returned source lines should preserve their original numbering."""
    source = repository / "example.py"
    source.write_text(
        "first_line = 1\nsecond_line = 2\nthird_line = 3\n",
        encoding="utf-8",
    )

    result = tools.read_file("example.py", repo_root=str(repository))

    assert "FILE: example.py" in result
    assert "   1 | first_line = 1" in result
    assert "   2 | second_line = 2" in result
    assert "   3 | third_line = 3" in result


def test_read_file_rejects_oversized_files(repository, monkeypatch):
    """Files above the configured read limit should not be returned."""
    source = repository / "large.py"
    source.write_text("x" * 100, encoding="utf-8")

    monkeypatch.setattr(tools, "MAX_READ_SIZE", 50)

    result = tools.read_file("large.py", repo_root=str(repository))

    assert result == "Error: file is too large to read."
    assert "xxx" not in result


def test_read_file_rejects_missing_files(repository):
    """Missing files should return a readable error."""
    result = tools.read_file("missing.py", repo_root=str(repository))

    assert result == "Error: file does not exist."


# ---------------------------------------------------------
# search_code: directory exclusions and search limits
# ---------------------------------------------------------

def test_search_code_ignores_excluded_directories(repository):
    """Search results must not include files in excluded directories."""
    normal_file = repository / "main.py"
    normal_file.write_text("target_function()", encoding="utf-8")

    excluded_dir = repository / ".venv"
    excluded_dir.mkdir()
    (excluded_dir / "hidden.py").write_text(
        "target_function()",
        encoding="utf-8",
    )

    result = tools.search_code(
        "target_function",
        repo_root=str(repository),
    )

    assert "main.py:1:" in result
    assert ".venv" not in result


def test_search_code_ignores_oversized_files(repository, monkeypatch):
    """Search must skip source files above its configured size limit."""
    source = repository / "large.py"
    source.write_text(
        "unique_marker = True\n" + ("x" * 100),
        encoding="utf-8",
    )

    monkeypatch.setattr(tools, "MAX_FILE_SIZE", 50)

    result = tools.search_code(
        "unique_marker",
        repo_root=str(repository),
    )

    assert "No matches found" in result


def test_search_code_limits_results_to_twenty(repository):
    """A large number of matches must not produce unbounded output."""
    for index in range(25):
        source = repository / f"module_{index}.py"
        source.write_text("shared_marker = True\n", encoding="utf-8")

    result = tools.search_code(
        "shared_marker",
        repo_root=str(repository),
    )

    assert "Matches (maximum 20):" in result
    assert result.count(":1: shared_marker") == 20


def test_search_code_rejects_empty_query(repository):
    """An empty search query should return an error."""
    result = tools.search_code("   ", repo_root=str(repository))

    assert result == "Error: search query cannot be empty."


def test_read_file_rejects_empty_path(repository):
    """An empty file path should return an error."""
    result = tools.read_file("   ", repo_root=str(repository))

    assert result == "Error: file path cannot be empty."


# ---------------------------------------------------------
# Symlink protection
# ---------------------------------------------------------

def test_read_file_blocks_symlink_escaping_repository(repository, tmp_path):
    """A symlink pointing outside the repository must not expose a file."""
    outside_file = tmp_path / "external.txt"
    outside_file.write_text("external private content", encoding="utf-8")

    link = repository / "external_link.txt"

    try:
        link.symlink_to(outside_file)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is unavailable on this system.")

    result = tools.read_file(
        "external_link.txt",
        repo_root=str(repository),
    )

    assert result == "Error: path is outside the repository."
    assert "external private content" not in result

def test_search_code_blocks_sensitive_python_filename(repository):
    """Search must not expose matching lines from sensitive filenames."""
    secret_file = repository / ".env.py"
    secret_file.write_text(
        'API_KEY = "fake-test-secret"\n',
        encoding="utf-8",
    )

    result = tools.search_code(
        "API_KEY",
        repo_root=str(repository),
    )

    assert "fake-test-secret" not in result
    assert "No matches found" in result
