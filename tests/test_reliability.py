"""Regression and reliability tests for DevPilot agent, tools, and API."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from openai import APIStatusError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
DEV_PILOT_DIR = SRC_DIR / "devpilot"

sys.path.insert(0, str(DEV_PILOT_DIR))
sys.path.insert(0, str(SRC_DIR))

import devpilot.agent as agent
import devpilot.tools as tools
from devpilot.api import app, create_investigation, InvestigationRequest


@pytest.fixture
def temp_repo(tmp_path):
    """Create a temporary repository with sample Python files."""
    source_file = tmp_path / "calculator.py"
    source_file.write_text(
        "def calculate_average(numbers):\n"
        "    total = sum(numbers)\n"
        "    return total / len(numbers)\n",
        encoding="utf-8",
    )
    caller_file = tmp_path / "main.py"
    caller_file.write_text(
        "from calculator import calculate_average\n"
        "scores = []\n"
        "print(calculate_average(scores))\n",
        encoding="utf-8",
    )
    return tmp_path


# -------------------------------------------------------------
# 1. Search tool enhancements (unescaping, snake_case, tokens)
# -------------------------------------------------------------

def test_search_code_handles_regex_escaped_punctuation(temp_repo):
    """search_code should strip unnecessary backslashes before punctuation."""
    result = tools.search_code(r"len\(", repo_root=str(temp_repo))
    assert "calculator.py:3:" in result
    assert "len(numbers)" in result


def test_search_code_handles_slash_escaped_punctuation(temp_repo):
    """search_code should match divisions even if escaped."""
    result = tools.search_code(r"/ len\(", repo_root=str(temp_repo))
    assert "calculator.py:3:" in result


def test_search_code_falls_back_to_snake_case(temp_repo):
    """search_code should match snake_case when multi-word query is provided."""
    result = tools.search_code("calculate average", repo_root=str(temp_repo))
    assert "calculator.py:1:" in result
    assert "def calculate_average" in result


# -------------------------------------------------------------
# 2. Tool call deduplication in agent loop
# -------------------------------------------------------------

def test_agent_deduplicates_repeated_tool_calls(temp_repo):
    """Repeated tool calls with identical arguments should return cached notice."""
    connected_repo = {
        "repository_id": "test-repo",
        "name": "Test Repo",
        "source_type": "local",
        "location": str(temp_repo),
        "status": "connected",
    }

    tool_call_1 = SimpleNamespace(
        id="call-1",
        type="function",
        function=SimpleNamespace(
            name="search_code",
            arguments=json.dumps({"query": "calculate_average"}),
        ),
    )
    tool_call_2 = SimpleNamespace(
        id="call-2",
        type="function",
        function=SimpleNamespace(
            name="search_code",
            arguments=json.dumps({"query": "calculate_average"}),
        ),
    )

    msg1 = MagicMock()
    msg1.content = None
    msg1.tool_calls = [tool_call_1]
    msg1.model_dump.return_value = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "search_code", "arguments": '{"query": "calculate_average"}'}}],
    }

    msg2 = MagicMock()
    msg2.content = None
    msg2.tool_calls = [tool_call_2]
    msg2.model_dump.return_value = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call-2", "type": "function", "function": {"name": "search_code", "arguments": '{"query": "calculate_average"}'}}],
    }

    msg3 = MagicMock()
    msg3.content = "Root cause identified in calculator.py line 3."
    msg3.tool_calls = []
    msg3.model_dump.return_value = {
        "role": "assistant",
        "content": msg3.content,
    }

    with (
        patch.object(agent, "get_repository", return_value=connected_repo),
        patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=[
                SimpleNamespace(choices=[SimpleNamespace(message=msg1)]),
                SimpleNamespace(choices=[SimpleNamespace(message=msg2)]),
                SimpleNamespace(choices=[SimpleNamespace(message=msg3)]),
            ],
        ),
    ):
        result = agent.run_investigation(
            repository_id="test-repo",
            task="Find calculate_average bug",
            max_iterations=4,
        )

    assert result["status"] == "completed"
    assert len(result["evidence"]) == 2
    assert result["evidence"][0]["status"] == "success"
    assert result["evidence"][1]["status"] == "cached"
    assert "already executed earlier" in result["evidence"][1]["result"]


# -------------------------------------------------------------
# 3. Provider retry on transient errors
# -------------------------------------------------------------

def test_agent_retries_transient_http_500(temp_repo):
    """The agent should retry transient HTTP 500 and succeed on the next attempt."""
    connected_repo = {
        "repository_id": "test-repo",
        "name": "Test Repo",
        "source_type": "local",
        "location": str(temp_repo),
        "status": "connected",
    }

    success_msg = MagicMock()
    success_msg.content = "Investigation diagnosis found."
    success_msg.tool_calls = []
    success_msg.model_dump.return_value = {
        "role": "assistant",
        "content": success_msg.content,
    }

    # Simulate an HTTP 500 APIStatusError
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.headers = {}
    error_500 = APIStatusError("Internal Server Error", response=mock_response, body={"error": "Failed to generate completions"})

    with (
        patch.object(agent, "get_repository", return_value=connected_repo),
        patch("time.sleep") as mock_sleep,
        patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=[
                error_500,
                SimpleNamespace(choices=[SimpleNamespace(message=success_msg)]),
            ],
        ) as mock_create,
    ):
        result = agent.run_investigation(
            repository_id="test-repo",
            task="Investigate issue",
            max_iterations=2,
        )

    assert result["status"] == "completed"
    assert result["answer"] == "Investigation diagnosis found."
    assert mock_create.call_count == 2
    mock_sleep.assert_called_once()


def test_agent_preserves_evidence_when_provider_fails(temp_repo):
    """When retries are exhausted, RuntimeError preserves collected evidence."""
    connected_repo = {
        "repository_id": "test-repo",
        "name": "Test Repo",
        "source_type": "local",
        "location": str(temp_repo),
        "status": "connected",
    }

    tool_call = SimpleNamespace(
        id="call-1",
        type="function",
        function=SimpleNamespace(
            name="search_code",
            arguments=json.dumps({"query": "calculate_average"}),
        ),
    )
    msg1 = MagicMock()
    msg1.content = None
    msg1.tool_calls = [tool_call]
    msg1.model_dump.return_value = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "search_code", "arguments": '{"query": "calculate_average"}'}}],
    }

    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.headers = {}
    error_500 = APIStatusError("Internal Server Error", response=mock_response, body={"error": "Server error"})

    with (
        patch.object(agent, "get_repository", return_value=connected_repo),
        patch("time.sleep"),
        patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=[
                SimpleNamespace(choices=[SimpleNamespace(message=msg1)]),
                error_500,
                error_500,
                error_500,
            ],
        ),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            agent.run_investigation(
                repository_id="test-repo",
                task="Investigate issue",
                max_iterations=3,
            )

    assert hasattr(exc_info.value, "evidence")
    assert len(exc_info.value.evidence) == 1
    assert exc_info.value.evidence[0]["tool"] == "search_code"


# -------------------------------------------------------------
# 4. Failed investigation persistence in API
# -------------------------------------------------------------

def test_api_persists_failed_investigation():
    """API should save failed investigation with evidence before returning 502."""
    runtime_err = RuntimeError("Model provider error")
    runtime_err.evidence = [{"tool": "search_code", "status": "success", "result": "matched lines"}]
    runtime_err.iterations = 2

    mock_repo = {
        "repository_id": "demo-repo",
        "name": "Demo",
        "status": "connected",
    }

    with (
        patch("devpilot.api.get_repository", return_value=mock_repo),
        patch("devpilot.api.run_investigation", side_effect=runtime_err),
        patch("devpilot.api.save_investigation") as mock_save,
    ):
        with pytest.raises(Exception) as exc_info:
            create_investigation(
                InvestigationRequest(
                    repository_id="demo-repo",
                    description="Empty list division",
                )
            )

        assert exc_info.value.status_code == 502
        mock_save.assert_called_once()
        saved_args = mock_save.call_args.kwargs
        assert saved_args["result"]["status"] == "failed"
        assert len(saved_args["result"]["evidence"]) == 1
        assert saved_args["result"]["iterations"] == 2


# -------------------------------------------------------------
# 5. Fallback ID assignment for empty tool-call IDs
# -------------------------------------------------------------

def test_agent_assigns_fallback_id_if_model_returns_empty_id(temp_repo):
    """The agent should synthesize a fallback ID if the provider omits tool-call id."""
    connected_repo = {
        "repository_id": "test-repo",
        "name": "Test Repo",
        "source_type": "local",
        "location": str(temp_repo),
        "status": "connected",
    }

    tool_call_no_id = SimpleNamespace(
        id="",
        type="function",
        function=SimpleNamespace(
            name="search_code",
            arguments=json.dumps({"query": "calculate_average"}),
        ),
    )
    msg1 = MagicMock()
    msg1.content = None
    msg1.tool_calls = [tool_call_no_id]
    msg1.model_dump.return_value = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call_0_0", "type": "function", "function": {"name": "search_code", "arguments": '{"query": "calculate_average"}'}}],
    }

    final_msg = MagicMock()
    final_msg.content = "Analysis complete."
    final_msg.tool_calls = []
    final_msg.model_dump.return_value = {"role": "assistant", "content": final_msg.content}

    with (
        patch.object(agent, "get_repository", return_value=connected_repo),
        patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=[
                SimpleNamespace(choices=[SimpleNamespace(message=msg1)]),
                SimpleNamespace(choices=[SimpleNamespace(message=final_msg)]),
            ],
        ),
    ):
        result = agent.run_investigation(
            repository_id="test-repo",
            task="Find bug",
            max_iterations=3,
        )

    assert result["status"] == "completed"
    assert tool_call_no_id.id.startswith("call_")


def test_api_persists_and_returns_verification(temp_repo):
    """Verify that when an investigation contains verification metadata, the API persists and returns it."""
    from fastapi.testclient import TestClient

    connected_repo = {
        "repository_id": "test-repo",
        "name": "Test Repo",
        "source_type": "local",
        "location": str(temp_repo),
        "status": "connected",
    }

    mock_verification = {
        "valid": True,
        "reproduction_resolved": True,
        "regression_suite_passed": True,
        "patched_file": "calculator.py",
        "diff": "--- a/calculator.py\n+++ b/calculator.py\n@@ -1 +1,2 @@\n+if not items: return 0\n",
        "pre_fix": {
            "reproduced": True,
            "exception_type": "ZeroDivisionError",
        },
        "post_fix": {
            "exit_code": 0,
        },
        "test_code": "assert calculate_average([]) == 0",
    }

    mock_result = {
        "status": "completed",
        "repository_id": "test-repo",
        "repository_name": "Test Repo",
        "answer": "Bug identified and fixed.",
        "evidence": [{"tool": "validate_patch", "status": "success", "result": "clean pass"}],
        "verification": mock_verification,
        "iterations": 2,
    }

    client = TestClient(app)

    with (
        patch("devpilot.api.get_repository", return_value=connected_repo),
        patch("devpilot.api.run_investigation", return_value=mock_result),
    ):
        response = client.post(
            "/api/investigations",
            json={
                "repository_id": "test-repo",
                "description": "Division by zero on empty list",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["verification"] is not None
    assert data["verification"]["valid"] is True
    assert data["verification"]["pre_fix"]["exception_type"] == "ZeroDivisionError"

    # Verify retrieval via GET /api/investigations
    get_res = client.get("/api/investigations")
    assert get_res.status_code == 200
    all_items = get_res.json()["investigations"]
    saved = next(item for item in all_items if item["id"] == data["id"])
    assert saved["verification"] is not None
    assert saved["verification"]["valid"] is True
    assert saved["verification"]["diff"] == mock_verification["diff"]
