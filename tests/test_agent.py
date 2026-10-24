
"""Unit tests for DevPilot's investigation agent.

These tests mock the repository tools and do not call NVIDIA's API.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


# Locate the project directories.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
DEV_PILOT_DIR = SRC_DIR / "devpilot"

# agent.py imports tools as a top-level module, so add devpilot
# to the import path as well as src.
sys.path.insert(0, str(DEV_PILOT_DIR))
sys.path.insert(0, str(SRC_DIR))

from devpilot.agent import execute_tool


def make_tool_call(name, arguments, call_id="test-call-1"):
    """Build a mock model tool call."""
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments),
        ),
    )


def test_execute_search_code_with_valid_arguments():
    """A valid search request should call the repository search tool."""
    tool_call = make_tool_call(
        "search_code",
        {"query": "def run_investigation"},
    )

    with patch(
        "devpilot.agent.search_code",
        return_value="agent.py:10: def run_investigation",
    ) as mocked_search:
        result = execute_tool(tool_call, ".")

    mocked_search.assert_called_once_with(
        "def run_investigation",
        repo_root=".",
    )
    assert "run_investigation" in result


def test_execute_read_file_with_valid_arguments():
    """A valid file request should call the file-reading tool."""
    tool_call = make_tool_call(
        "read_file",
        {"path": "src/devpilot/agent.py"},
    )

    with patch(
        "devpilot.agent.read_file",
        return_value="FILE: src/devpilot/agent.py",
    ) as mocked_read:
        result = execute_tool(tool_call, ".")

    mocked_read.assert_called_once_with(
        "src/devpilot/agent.py",
        repo_root=".",
    )
    assert "agent.py" in result


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("unknown_tool", {}),
        ("search_code", {}),
        ("search_code", {"query": "x", "extra": "unexpected"}),
        ("read_file", {"path": "agent.py", "extra": "unexpected"}),
        ("search_code", []),
    ],
)
def test_execute_tool_rejects_invalid_requests(name, arguments):
    """Unknown tools and malformed arguments should be rejected."""
    tool_call = make_tool_call(name, arguments)

    with pytest.raises((ValueError, TypeError, KeyError)):
        execute_tool(tool_call, ".")


def test_execute_tool_rejects_invalid_json():
    """Malformed JSON must not be silently accepted."""
    tool_call = SimpleNamespace(
        id="test-call-2",
        function=SimpleNamespace(
            name="search_code",
            arguments="{invalid json",
        ),
    )

    with pytest.raises((ValueError, TypeError, KeyError)):
        execute_tool(tool_call, ".")
