"""Tests for image upload and screenshot error extraction in DevPilot."""

import json
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
DEV_PILOT_DIR = SRC_DIR / "devpilot"
sys.path.insert(0, str(DEV_PILOT_DIR))
sys.path.insert(0, str(SRC_DIR))

from devpilot.agent import extract_error_from_image, run_investigation
from devpilot.api import app
from fastapi.testclient import TestClient


def test_extract_error_from_image_success():
    """Verify extract_error_from_image parses vision model responses correctly."""
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "ZeroDivisionError: division by zero in calculate_average() at src/math.py:42"
    mock_response.choices = [mock_choice]

    with patch("devpilot.agent.client.chat.completions.create", return_value=mock_response):
        extracted = extract_error_from_image("data:image/png;base64,iVBORw0KGgo=")

    assert "ZeroDivisionError" in extracted
    assert "src/math.py:42" in extracted


def test_extract_error_from_image_empty_data():
    """Empty image data must raise ValueError."""
    with pytest.raises(ValueError, match="Image data cannot be empty"):
        extract_error_from_image("")


def test_run_investigation_with_screenshot_only():
    """Investigation with screenshot only should extract error and run investigation."""
    fake_screenshot_analysis = "AttributeError: 'NoneType' object has no attribute 'get' in parse_header at src/parser.py:15"

    fake_agent_result = {
        "status": "completed",
        "repository_id": "test-repo",
        "repository_name": "Test Repo",
        "answer": "The NoneType error occurs because the header was not validated.",
        "evidence": [],
        "iterations": 1,
    }

    with (
        patch("devpilot.agent.extract_error_from_image", return_value=fake_screenshot_analysis),
        patch("devpilot.agent.get_repository", return_value={"name": "Test Repo", "status": "connected", "source_type": "local", "location": "."}),
        patch("devpilot.agent._call_model_with_retry") as mock_call,
    ):
        mock_msg = MagicMock()
        mock_msg.tool_calls = None
        mock_msg.content = fake_agent_result["answer"]
        mock_choice = MagicMock()
        mock_choice.message = mock_msg
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_call.return_value = mock_resp

        result = run_investigation(
            repository_id="test-repo",
            task="",
            image_data="data:image/png;base64,abc12345",
        )

    assert result["status"] == "completed"
    assert result["screenshot_analysis"] == fake_screenshot_analysis


def test_api_investigations_with_image_data():
    """API endpoint /api/investigations accepts image_data and returns screenshot_analysis."""
    client = TestClient(app)

    connected_repo = {
        "repository_id": "test-repo",
        "name": "Test Repository",
        "status": "connected",
        "source_type": "local",
        "location": ".",
    }

    mock_investigation = {
        "status": "completed",
        "repository_id": "test-repo",
        "repository_name": "Test Repository",
        "answer": "Bug identified from uploaded screenshot.",
        "evidence": [],
        "iterations": 1,
        "screenshot_analysis": "KeyError: 'user_id' in session handler",
    }

    with (
        patch("devpilot.api.get_repository", return_value=connected_repo),
        patch("devpilot.api.run_investigation", return_value=mock_investigation),
    ):
        response = client.post(
            "/api/investigations",
            json={
                "repository_id": "test-repo",
                "description": "",
                "image_data": "data:image/png;base64,sampleimagecontent",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["screenshot_analysis"] == "KeyError: 'user_id' in session handler"
