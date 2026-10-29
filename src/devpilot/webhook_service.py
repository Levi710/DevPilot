"""Narrow read-only GitHub webhook task-ingress policy.

Only pull-request events include an immutable base SHA without making a GitHub
API call. Issue/comment events remain authenticated evidence for a later phase
that can resolve a repository ref through a read-only GitHub App client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import AsyncIterable
import re

try:
    from github_webhook import GitHubWebhookError, verify_read_only_webhook
    from task_store import DeliveryTaskAcceptance, PostgresTaskStore, TaskStoreError
except ImportError:
    from devpilot.github_webhook import GitHubWebhookError, verify_read_only_webhook
    from devpilot.task_store import DeliveryTaskAcceptance, PostgresTaskStore, TaskStoreError


MAX_WEBHOOK_BODY_BYTES = 1_000_000
TASK_TRIGGERING_PULL_REQUEST_ACTIONS = frozenset({"opened", "reopened", "synchronize"})
_SHA1_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class WebhookIngressError(ValueError):
    """An HTTP-safe webhook ingress failure with an explicit status code."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class WebhookIngressResult:
    delivery_was_new: bool
    task_id: str | None
    status: str


@dataclass
class BoundedBodyAccumulator:
    """Incremental request-body limit that is testable without an event loop."""

    maximum_bytes: int = MAX_WEBHOOK_BODY_BYTES
    _body: bytearray = field(default_factory=bytearray)

    def append(self, chunk: bytes) -> None:
        if not isinstance(chunk, bytes):
            raise WebhookIngressError(400, "GitHub webhook body contains invalid data.")
        if len(self._body) + len(chunk) > self.maximum_bytes:
            raise WebhookIngressError(413, "GitHub webhook body exceeds the 1 MB limit.")
        self._body.extend(chunk)

    def value(self) -> bytes:
        return bytes(self._body)


async def read_bounded_body(chunks: AsyncIterable[bytes], *, maximum_bytes: int = MAX_WEBHOOK_BODY_BYTES) -> bytes:
    """Read an ASGI body incrementally without buffering an oversized payload."""

    accumulator = BoundedBodyAccumulator(maximum_bytes=maximum_bytes)
    async for chunk in chunks:
        accumulator.append(chunk)
    return accumulator.value()


def accept_pull_request_webhook(
    *,
    body: bytes,
    signature_header: str | None,
    delivery_id: str | None,
    event_name: str | None,
    webhook_secret: str | None,
    store: PostgresTaskStore,
) -> WebhookIngressResult:
    """Verify and atomically create a queued read-only task from a PR event."""

    if len(body) > MAX_WEBHOOK_BODY_BYTES:
        raise WebhookIngressError(413, "GitHub webhook body exceeds the 1 MB limit.")
    if not webhook_secret:
        raise WebhookIngressError(503, "GitHub webhook ingress is not configured.")
    try:
        webhook = verify_read_only_webhook(
            payload=body,
            signature_header=signature_header,
            delivery_id=delivery_id,
            event_name=event_name,
            webhook_secret=webhook_secret,
        )
    except GitHubWebhookError as exc:
        raise WebhookIngressError(401, "GitHub webhook authentication failed.") from exc

    if webhook.event_name != "pull_request":
        raise WebhookIngressError(
            202,
            "Verified event is not a task trigger in the read-only webhook phase.",
        )
    payload = webhook.payload
    action = payload.get("action")
    repository = payload.get("repository")
    pull_request = payload.get("pull_request")
    if action not in TASK_TRIGGERING_PULL_REQUEST_ACTIONS:
        raise WebhookIngressError(202, "Verified pull-request action does not trigger a task.")
    if not isinstance(repository, dict) or not isinstance(pull_request, dict):
        raise WebhookIngressError(400, "Pull-request webhook has an invalid payload shape.")
    repository_node_id = repository.get("node_id")
    base = pull_request.get("base")
    base_sha = base.get("sha") if isinstance(base, dict) else None
    number = payload.get("number")
    if not isinstance(repository_node_id, str) or not repository_node_id:
        raise WebhookIngressError(400, "Pull-request webhook lacks a repository node ID.")
    if not isinstance(base_sha, str) or not _SHA1_RE.fullmatch(base_sha):
        raise WebhookIngressError(400, "Pull-request webhook lacks a valid immutable base SHA.")
    if not isinstance(number, int) or number < 1:
        raise WebhookIngressError(400, "Pull-request webhook lacks a valid pull-request number.")
    try:
        accepted: DeliveryTaskAcceptance = store.accept_verified_delivery_and_create_task(
            delivery_id=webhook.delivery_id,
            event_name=webhook.event_name,
            payload=body,
            source="github_pull_request",
            repository_node_id=repository_node_id,
            base_sha=base_sha,
            request_metadata={"trigger": "pull_request", "issue_number": number},
        )
    except TaskStoreError as exc:
        raise WebhookIngressError(503, "Durable webhook task storage is unavailable.") from exc
    return WebhookIngressResult(
        delivery_was_new=accepted.delivery_was_new,
        task_id=accepted.task.task_id,
        status="queued" if accepted.delivery_was_new else "replayed",
    )
