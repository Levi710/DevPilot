"""Security tests for the read-only GitHub webhook boundary."""

import hashlib
import hmac
import json
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from devpilot.github_webhook import (
    GitHubWebhookError,
    redact_github_credentials,
    verify_read_only_webhook,
)


SECRET = "test-webhook-secret"


def signed_payload(payload: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()


def test_valid_supported_webhook_is_verified_without_side_effects():
    payload = json.dumps({"action": "opened", "repository": {"id": 123}}).encode()
    event = verify_read_only_webhook(
        payload=payload,
        signature_header=signed_payload(payload),
        delivery_id="delivery-123",
        event_name="issues",
        webhook_secret=SECRET,
    )
    assert event.delivery_id == "delivery-123"
    assert event.event_name == "issues"
    assert event.payload["repository"]["id"] == 123


@pytest.mark.parametrize(
    "signature",
    (None, "sha1=deadbeef", "sha256=" + "0" * 64),
)
def test_webhook_rejects_missing_malformed_or_wrong_signature(signature):
    with pytest.raises(GitHubWebhookError):
        verify_read_only_webhook(
            payload=b"{}",
            signature_header=signature,
            delivery_id="delivery-123",
            event_name="issues",
            webhook_secret=SECRET,
        )


def test_webhook_rejects_unknown_events_and_bad_delivery_ids():
    payload = b"{}"
    with pytest.raises(GitHubWebhookError):
        verify_read_only_webhook(
            payload=payload,
            signature_header=signed_payload(payload),
            delivery_id="../../replay",
            event_name="push",
            webhook_secret=SECRET,
        )


def test_github_credential_redaction_hides_common_tokens_and_bearer_headers():
    secret = "ghp_abcdefghijklmnopqrstuvwxyz123456 Authorization: Bearer top-secret"
    redacted = redact_github_credentials(secret)
    assert "abcdefghijklmnopqrstuvwxyz" not in redacted
    assert "top-secret" not in redacted
    assert redacted.count("[REDACTED_GITHUB_CREDENTIAL]") == 2
