
"""Tests for DevPilot's complete investigation loop.

All NVIDIA API responses and repository access are mocked.
These tests make no network calls and consume no API credits.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# Support the project's current src/devpilot import structure.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
DEV_PILOT_DIR = SRC_DIR / "devpilot"

sys.path.insert(0, str(DEV_PILOT_DIR))
sys.path.insert(0, str(SRC_DIR))

import devpilot.agent as agent


@pytest.fixture
def connected_repository():
    """Provide a registered local repository without touching the registry."""
    return {
        "repository_id": "devpilot",
        "name": "DevPilot",
        "source_type": "local",
        "location": str(PROJECT_ROOT),
        "status": "connected",
    }


def make_message(content=None, tool_calls=None):
    """Create a mock assistant message with model_dump support."""
    calls = tool_calls or []

    message = MagicMock()
    message.content = content
    message.tool_calls = calls

    message.model_dump.return_value = {
        "role": "assistant",
        "content": content,
        **(
            {
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in calls
                ]
            }
            if calls
            else {}
        ),
    }

    return message


def make_tool_call(name, arguments, call_id="call-1"):
    """Create a mock tool call matching the API response structure."""
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments),
        ),
    )


def make_response(message):
    """Wrap a mock message in a chat-completion response."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)]
    )


def setup_repository_mock(mocked_get_repository, repository):
    """Return a successful repository lookup."""
    mocked_get_repository.return_value = repository


def test_investigation_completes_after_tool_call(connected_repository):
    """The agent should execute a tool, send its result back, then finish."""

    tool_call = make_tool_call(
        "search_code",
        {"query": "def run_investigation"},
    )

    tool_response = make_message(tool_calls=[tool_call])
    final_response = make_message(
        content="The function is defined in agent.py. Further inspection is recommended."
    )

    with (
        patch.object(agent, "get_repository", return_value=connected_repository),
        patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=[
                make_response(tool_response),
                make_response(final_response),
            ],
        ) as mocked_create,
        patch.object(
            agent,
            "search_code",
            return_value="agent.py:133: def run_investigation",
        ) as mocked_search,
    ):
        result = agent.run_investigation(
            repository_id="devpilot",
            task="Find the investigation function.",
        )

    assert result["status"] == "completed"
    assert "agent.py" in result["answer"]
    assert result["iterations"] == 2
    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["status"] == "success"

    mocked_search.assert_called_once_with(
        "def run_investigation",
        repo_root=connected_repository["location"],
    )
    assert mocked_create.call_count == 2

    # The second model request should include the tool result.
    second_messages = mocked_create.call_args_list[1].kwargs["messages"]
    assert any(
        message.get("role") == "tool"
        and "agent.py:133" in message.get("content", "")
        for message in second_messages
    )


def test_investigation_handles_empty_final_response(connected_repository):
    """An empty final answer must not be marked completed."""

    empty_message = make_message(content="   ")

    with (
        patch.object(agent, "get_repository", return_value=connected_repository),
        patch.object(
            agent.client.chat.completions,
            "create",
            return_value=make_response(empty_message),
        ),
    ):
        result = agent.run_investigation(
            repository_id="devpilot",
            task="Investigate a reported bug.",
        )

    assert result["status"] == "incomplete"
    assert result["iterations"] == 1
    assert "no final text" in result["answer"].lower()


def test_investigation_handles_no_response_choices(connected_repository):
    """A response with no choices should produce a clear RuntimeError."""

    empty_response = SimpleNamespace(choices=[])

    with (
        patch.object(agent, "get_repository", return_value=connected_repository),
        patch.object(
            agent.client.chat.completions,
            "create",
            return_value=empty_response,
        ),
    ):
        with pytest.raises(RuntimeError, match="no response choices"):
            agent.run_investigation(
                repository_id="devpilot",
                task="Investigate a reported bug.",
            )


def test_investigation_stops_at_iteration_limit(connected_repository):
    """Repeated tool requests must not exceed max_iterations."""

    tool_call = make_tool_call(
        "search_code",
        {"query": "some_function"},
    )
    tool_response = make_response(make_message(tool_calls=[tool_call]))

    with (
        patch.object(agent, "get_repository", return_value=connected_repository),
        patch.object(
            agent.client.chat.completions,
            "create",
            return_value=tool_response,
        ) as mocked_create,
        patch.object(agent, "search_code", return_value="No matches."),
    ):
        result = agent.run_investigation(
            repository_id="devpilot",
            task="Investigate a reported bug.",
            max_iterations=2,
        )

    assert result["status"] == "incomplete"
    assert result["iterations"] == 2
    assert mocked_create.call_count == 2


def test_investigation_rejects_unregistered_repository():
    """The agent must not investigate an unregistered repository."""

    with (
        patch.object(agent, "get_repository", return_value=None),
        patch.object(agent.client.chat.completions, "create") as mocked_create,
    ):
        with pytest.raises(ValueError, match="not registered"):
            agent.run_investigation(
                repository_id="missing-repository",
                task="Investigate a reported bug.",
            )

    mocked_create.assert_not_called()


@pytest.mark.parametrize(
    "repository_id,task,max_iterations",
    [
        ("", "Investigate a bug.", 5),
        ("devpilot", "   ", 5),
        ("devpilot", "Investigate a bug.", 0),
        ("devpilot", "Investigate a bug.", 11),
    ],
)
def test_investigation_rejects_invalid_input(
    connected_repository,
    repository_id,
    task,
    max_iterations,
):
    """Invalid inputs must be rejected before any model call."""

    with (
        patch.object(agent, "get_repository", return_value=connected_repository),
        patch.object(agent.client.chat.completions, "create") as mocked_create,
    ):
        with pytest.raises(ValueError):
            agent.run_investigation(
                repository_id=repository_id,
                task=task,
                max_iterations=max_iterations,
            )

    mocked_create.assert_not_called()
