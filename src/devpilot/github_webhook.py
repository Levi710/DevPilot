"""Read-only GitHub webhook boundary.

This module deliberately performs no GitHub API action. It verifies that an
event genuinely came from GitHub before the future durable task layer records
it. Delivery deduplication is represented as a protocol so a future PostgreSQL
implementation, rather than process memory, owns the replay boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Protocol


SUPPORTED_READ_ONLY_EVENTS = frozenset(
    {"installation", "installation_repositories", "issues", "issue_comment", "pull_request"}
)
_DELIVERY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class GitHubWebhookError(ValueError):
    """Raised for malformed or unauthenticated GitHub webhook input."""


class DeliveryReceiptStore(Protocol):
    """Durable replay barrier required before a webhook can enqueue work."""

    def record_if_new(self, delivery_id: str) -> bool:
        """Atomically record a delivery and return True only for the first receipt."""


@dataclass(frozen=True)
class VerifiedWebhook:
    """Authenticated event metadata; payload remains untrusted evidence."""

    delivery_id: str
    event_name: str
    payload: dict


def verify_signature(payload: bytes, signature_header: str | None, webhook_secret: str) -> None:
    """Verify GitHub's SHA-256 signature using constant-time comparison."""

    if not webhook_secret:
        raise GitHubWebhookError("GitHub webhook verification is not configured.")
    if not signature_header or not signature_header.startswith("sha256="):
        raise GitHubWebhookError("Missing or malformed X-Hub-Signature-256 header.")
    supplied = signature_header.removeprefix("sha256=")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", supplied):
        raise GitHubWebhookError("Malformed X-Hub-Signature-256 digest.")
    expected = hmac.new(webhook_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied.lower()):
        raise GitHubWebhookError("GitHub webhook signature verification failed.")


def verify_read_only_webhook(
    *,
    payload: bytes,
    signature_header: str | None,
    delivery_id: str | None,
    event_name: str | None,
    webhook_secret: str,
) -> VerifiedWebhook:
    """Authenticate and parse a supported webhook without performing side effects."""

    verify_signature(payload, signature_header, webhook_secret)
    if not delivery_id or not _DELIVERY_ID_RE.fullmatch(delivery_id):
        raise GitHubWebhookError("Missing or malformed X-GitHub-Delivery header.")
    if event_name not in SUPPORTED_READ_ONLY_EVENTS:
        raise GitHubWebhookError("Unsupported GitHub webhook event for read-only phase.")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GitHubWebhookError("GitHub webhook payload must be a UTF-8 JSON object.") from exc
    if not isinstance(decoded, dict):
        raise GitHubWebhookError("GitHub webhook payload must be a JSON object.")
    return VerifiedWebhook(delivery_id=delivery_id, event_name=event_name, payload=decoded)


def redact_github_credentials(value: str) -> str:
    """Redact common GitHub token forms before a value is logged or persisted."""

    if not isinstance(value, str):
        return "[non-text value redacted]"
    value = re.sub(
        r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
        "[REDACTED_GITHUB_CREDENTIAL]",
        value,
    )
    return re.sub(
        r"(?i)(authorization:\s*bearer\s+)[^\s]+",
        r"\1[REDACTED_GITHUB_CREDENTIAL]",
        value,
    )
