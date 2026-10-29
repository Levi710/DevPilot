"""Tests for bounded, signed read-only webhook task ingress."""

import hashlib
import hmac
import json
from pathlib import Path
import sys
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from devpilot.webhook_service import (
    MAX_WEBHOOK_BODY_BYTES,
    BoundedBodyAccumulator,
    WebhookIngressError,
    accept_pull_request_webhook,
    read_bounded_body,
)


SECRET = "webhook-secret"


def make_body(**overrides):
    payload = {
        "action": "opened",
        "number": 12,
        "repository": {"node_id": "R_kgDOExample"},
        "pull_request": {"base": {"sha": "a" * 40}},
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def signature(body):
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_creates_read_only_task_through_atomic_store_only():
    body = make_body()
    store = MagicMock()
    store.accept_verified_delivery_and_create_task.return_value = MagicMock(
        delivery_was_new=True, task=MagicMock(task_id="task-1")
    )
    result = accept_pull_request_webhook(
        body=body, signature_header=signature(body), delivery_id="delivery-1",
        event_name="pull_request", webhook_secret=SECRET, store=store,
    )
    assert result.status == "queued"
    assert result.task_id == "task-1"
    kwargs = store.accept_verified_delivery_and_create_task.call_args.kwargs
    assert kwargs["base_sha"] == "a" * 40
    assert kwargs["repository_node_id"] == "R_kgDOExample"


def test_webhook_rejects_oversized_body_before_signature_or_store():
    store = MagicMock()
    with pytest.raises(WebhookIngressError) as error:
        accept_pull_request_webhook(
            body=b"x" * (MAX_WEBHOOK_BODY_BYTES + 1), signature_header=None,
            delivery_id="delivery-1", event_name="pull_request", webhook_secret=SECRET, store=store,
        )
    assert error.value.status_code == 413
    store.accept_verified_delivery_and_create_task.assert_not_called()


def test_body_accumulator_rejects_chunked_oversize_before_buffering_excess():
    accumulator = BoundedBodyAccumulator()
    accumulator.append(b"x" * (MAX_WEBHOOK_BODY_BYTES - 10))
    with pytest.raises(WebhookIngressError) as error:
        accumulator.append(b"x" * 11)
    assert error.value.status_code == 413
    assert accumulator.value() == b"x" * (MAX_WEBHOOK_BODY_BYTES - 10)


@pytest.mark.parametrize("event_name,action", (("issues", "opened"), ("pull_request", "closed")))
def test_non_triggering_events_do_not_reach_store(event_name, action):
    body = make_body(action=action)
    store = MagicMock()
    with pytest.raises(WebhookIngressError) as error:
        accept_pull_request_webhook(
            body=body, signature_header=signature(body), delivery_id="delivery-1",
            event_name=event_name, webhook_secret=SECRET, store=store,
        )
    assert error.value.status_code == 202
    store.accept_verified_delivery_and_create_task.assert_not_called()


def test_webhook_rejects_missing_configuration_and_invalid_base_sha():
    body = make_body()
    with pytest.raises(WebhookIngressError) as disabled:
        accept_pull_request_webhook(
            body=body, signature_header=signature(body), delivery_id="delivery-1",
            event_name="pull_request", webhook_secret="", store=MagicMock(),
        )
    assert disabled.value.status_code == 503
    malformed = make_body(pull_request={"base": {"sha": "not-a-sha"}})
    with pytest.raises(WebhookIngressError) as invalid:
        accept_pull_request_webhook(
            body=malformed, signature_header=signature(malformed), delivery_id="delivery-2",
            event_name="pull_request", webhook_secret=SECRET, store=MagicMock(),
        )
    assert invalid.value.status_code == 400
    non_hex = make_body(pull_request={"base": {"sha": "z" * 40}})
    with pytest.raises(WebhookIngressError) as non_hex_error:
        accept_pull_request_webhook(
            body=non_hex, signature_header=signature(non_hex), delivery_id="delivery-3",
            event_name="pull_request", webhook_secret=SECRET, store=MagicMock(),
        )
    assert non_hex_error.value.status_code == 400
