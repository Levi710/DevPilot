"""Tests for the fail-closed, non-networked GitHub App configuration contract."""

from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from devpilot.github_app_config import GitHubAppConfigurationError, load_github_app_configuration


def test_github_app_access_is_disabled_by_default():
    assert load_github_app_configuration({}) is None


def test_enabled_github_app_requires_all_identity_fields(tmp_path):
    with pytest.raises(GitHubAppConfigurationError, match="APP_ID"):
        load_github_app_configuration({"DEVPILOT_GITHUB_APP_ENABLED": "true"}, workspace_root=tmp_path)


@pytest.mark.parametrize("bad_id", ("0", "-1", "not-a-number"))
def test_enabled_github_app_rejects_non_positive_ids(bad_id, tmp_path):
    with pytest.raises(GitHubAppConfigurationError):
        load_github_app_configuration(
            {
                "DEVPILOT_GITHUB_APP_ENABLED": "true",
                "DEVPILOT_GITHUB_APP_ID": bad_id,
                "DEVPILOT_GITHUB_INSTALLATION_ID": "1",
            },
            workspace_root=tmp_path,
        )


def test_private_key_must_be_existing_absolute_pem_outside_repository(tmp_path):
    inside_key = tmp_path / "secrets.pem"
    inside_key.write_text("not-read", encoding="utf-8")
    values = {
        "DEVPILOT_GITHUB_APP_ENABLED": "true",
        "DEVPILOT_GITHUB_APP_ID": "123",
        "DEVPILOT_GITHUB_INSTALLATION_ID": "456",
        "DEVPILOT_GITHUB_APP_PRIVATE_KEY_PATH": str(inside_key),
    }
    with pytest.raises(GitHubAppConfigurationError, match="outside"):
        load_github_app_configuration(values, workspace_root=tmp_path)


def test_valid_config_references_external_key_without_reading_it(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    key = tmp_path / "devpilot-github-app.pem"
    key.write_text("private-key-content-is-not-parsed-here", encoding="utf-8")
    config = load_github_app_configuration(
        {
            "DEVPILOT_GITHUB_APP_ENABLED": "true",
            "DEVPILOT_GITHUB_APP_ID": "123",
            "DEVPILOT_GITHUB_INSTALLATION_ID": "456",
            "DEVPILOT_GITHUB_APP_PRIVATE_KEY_PATH": str(key),
        },
        workspace_root=workspace,
    )
    assert config is not None
    assert config.app_id == 123
    assert config.private_key_path == key.resolve()
