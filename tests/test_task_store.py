"""Unit tests for durable task-store input and PostgreSQL contracts."""

from pathlib import Path
import os
import sys
import uuid

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from devpilot.task_store import PostgresTaskStore, TaskStoreError


def test_task_store_requires_a_database_url():
    with pytest.raises(TaskStoreError, match="database URL"):
        PostgresTaskStore("")


def test_task_store_rejects_unsafe_schema_name():
    with pytest.raises(TaskStoreError, match="schema"):
        PostgresTaskStore("postgresql://localhost/devpilot", schema="public;drop")


def test_task_store_rejects_invalid_task_identifier_before_connecting():
    store = PostgresTaskStore("postgresql://localhost/devpilot")
    with pytest.raises(TaskStoreError, match="UUID"):
        store.get_task("not-a-uuid")
    with pytest.raises(TaskStoreError, match="UUID"):
        store.list_task_audit_events("not-a-uuid")


def test_task_store_rejects_malformed_delivery_input_before_connecting():
    store = PostgresTaskStore("postgresql://localhost/devpilot")
    with pytest.raises(TaskStoreError, match="delivery ID"):
        store.record_delivery_if_new(delivery_id="", event_name="issues", payload=b"{}")
    with pytest.raises(TaskStoreError, match="delivery ID"):
        store.record_delivery_if_new(delivery_id="id", event_name="issues", payload="not-bytes")


def test_task_store_rejects_invalid_task_metadata_before_connecting():
    store = PostgresTaskStore("postgresql://localhost/devpilot")
    with pytest.raises(TaskStoreError, match="source"):
        store.create_read_only_task(source="", repository_node_id="R_1", base_sha="abc")
    with pytest.raises(TaskStoreError, match="JSON object"):
        store.create_read_only_task(
            source="manual", repository_node_id="R_1", base_sha="abc", request_metadata=[]
        )


@pytest.mark.parametrize(
    "metadata",
    (
        {"unknown": "value"},
        {"access_token": "not-allowed"},
        {"trigger": "ghp_abcdefghijklmnopqrstuvwxyz123456"},
        {"trigger": {"nested": "not-allowed"}},
    ),
)
def test_task_store_rejects_unbounded_or_secret_like_metadata_before_connecting(metadata):
    store = PostgresTaskStore("postgresql://localhost/devpilot")
    with pytest.raises(TaskStoreError):
        store.create_read_only_task(
            source="manual", repository_node_id="R_1", base_sha="abc", request_metadata=metadata
        )


def test_postgres_store_atomically_deduplicates_delivery_and_task():
    """Run only when an explicit local test database URL is supplied."""
    if os.getenv("DEVPILOT_RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("Set DEVPILOT_RUN_POSTGRES_INTEGRATION=1 for PostgreSQL integration testing.")
    database_url = os.getenv("DEVPILOT_DATABASE_URL")
    if not database_url:
        pytest.skip("DEVPILOT_DATABASE_URL is not configured for PostgreSQL integration testing.")

    schema = f"test_task_store_{uuid.uuid4().hex}"
    store = PostgresTaskStore(database_url, schema=schema)
    try:
        store.initialize()
        kwargs = {
            "delivery_id": "delivery-atomic-test",
            "event_name": "issues",
            "payload": b"same-payload",
            "source": "integration_test",
            "repository_node_id": "R_kgDOIntegration",
            "base_sha": "a" * 40,
            "request_metadata": {"trigger": "manual"},
        }
        first = store.accept_verified_delivery_and_create_task(**kwargs)
        replay = store.accept_verified_delivery_and_create_task(**kwargs)
        assert first.delivery_was_new is True
        assert replay.delivery_was_new is False
        assert replay.task.task_id == first.task.task_id
        assert isinstance(first.task.task_id, str)
        with pytest.raises(TaskStoreError, match="different event data"):
            store.accept_verified_delivery_and_create_task(**(kwargs | {"event_name": "pull_request"}))
        with pytest.raises(TaskStoreError, match="different event data"):
            store.accept_verified_delivery_and_create_task(**(kwargs | {"payload": b"changed"}))
        with store._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS count FROM tasks")
            assert cursor.fetchone()["count"] == 1
            cursor.execute("SELECT COUNT(*) AS count FROM task_audit_events")
            assert cursor.fetchone()["count"] == 1
    finally:
        if os.getenv("DEVPILOT_DATABASE_URL"):
            import psycopg
            from psycopg import sql
            with psycopg.connect(database_url, autocommit=True) as connection, connection.cursor() as cursor:
                cursor.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
