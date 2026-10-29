"""Durable PostgreSQL records for future DevPilot tasks and webhook receipts.

The store contains no GitHub credentials and makes no GitHub API calls. Its
first security purpose is an atomic replay barrier: a GitHub delivery ID can be
accepted once even when a webhook is retried concurrently.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # Keep local investigation mode usable without PostgreSQL.
    psycopg = None
    dict_row = None


class TaskStoreError(RuntimeError):
    """Raised when the durable task store is unavailable or receives invalid data."""


@dataclass(frozen=True)
class DurableTask:
    task_id: str
    source: str
    repository_node_id: str
    base_sha: str
    state: str
    created_at: datetime


@dataclass(frozen=True)
class TaskAuditEvent:
    event_id: int
    event_type: str
    event_metadata: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class DeliveryTaskAcceptance:
    """Result of atomically accepting one delivery and creating one task."""

    delivery_was_new: bool
    task: DurableTask


_ALLOWED_METADATA_KEYS = frozenset({"trigger", "issue_number", "issue_node_id", "requested_by"})
_SENSITIVE_METADATA_KEY = re.compile(r"(?i)(token|secret|password|authorization|private.?key)")
_GITHUB_CREDENTIAL = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_SCHEMA_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class PostgresTaskStore:
    """PostgreSQL store with transaction-scoped, idempotent mutations."""

    def __init__(self, database_url: str, *, schema: str = "public") -> None:
        if not isinstance(database_url, str) or not database_url.strip():
            raise TaskStoreError("A PostgreSQL database URL is required for the task store.")
        if psycopg is None:
            raise TaskStoreError("psycopg is required for the PostgreSQL task store.")
        if not _SCHEMA_NAME.fullmatch(schema):
            raise TaskStoreError("Task-store schema must be a safe PostgreSQL identifier.")
        self._database_url = database_url
        self._schema = schema

    def _connect(self):
        return psycopg.connect(
            self._database_url,
            row_factory=dict_row,
            options=f"-c search_path={self._schema}",
        )

    def initialize(self) -> None:
        """Create the initial durable schema. This is idempotent and credential-free."""

        statements = (
            """
            CREATE TABLE IF NOT EXISTS webhook_deliveries (
                delivery_id TEXT PRIMARY KEY,
                event_name TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id UUID PRIMARY KEY,
                source TEXT NOT NULL,
                repository_node_id TEXT NOT NULL,
                base_sha TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('queued', 'policy_checked', 'cancelled', 'failed')),
                source_delivery_id TEXT UNIQUE REFERENCES webhook_deliveries(delivery_id),
                request_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS task_audit_events (
                event_id BIGSERIAL PRIMARY KEY,
                task_id UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                event_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_task_audit_task_id ON task_audit_events(task_id, event_id)",
        )
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {self._schema}")
                cursor.execute(f"SET LOCAL search_path TO {self._schema}")
                for statement in statements:
                    cursor.execute(statement)
                # Forward-compatible migration for databases initialized by an
                # earlier read-only task-store build.
                cursor.execute(
                    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS source_delivery_id TEXT "
                    "REFERENCES webhook_deliveries(delivery_id)"
                )
                cursor.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_source_delivery_id "
                    "ON tasks(source_delivery_id) WHERE source_delivery_id IS NOT NULL"
                )
        except Exception as exc:
            raise TaskStoreError("Could not initialize the PostgreSQL task store.") from exc

    def record_delivery_if_new(self, *, delivery_id: str, event_name: str, payload: bytes) -> bool:
        """Atomically record a verified delivery; return False for a replay.

        The payload itself is not persisted because webhook data is untrusted
        and can be large. The SHA-256 digest provides audit correlation.
        """

        if not _valid_delivery_input(delivery_id, event_name, payload):
            raise TaskStoreError("A delivery ID, event name, and byte payload are required.")
        digest = hashlib.sha256(payload).hexdigest()
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO webhook_deliveries (delivery_id, event_name, payload_sha256)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (delivery_id) DO NOTHING
                    RETURNING delivery_id
                    """,
                    (delivery_id, event_name, digest),
                )
                inserted = cursor.fetchone() is not None
                if inserted:
                    return True
                cursor.execute(
                    "SELECT event_name, payload_sha256 FROM webhook_deliveries WHERE delivery_id = %s",
                    (delivery_id,),
                )
                existing = cursor.fetchone()
                if (
                    existing is None
                    or existing["payload_sha256"] != digest
                    or existing["event_name"] != event_name
                ):
                    raise TaskStoreError("Webhook delivery ID was replayed with different event data.")
                return False
        except Exception as exc:
            raise TaskStoreError("Could not record GitHub webhook delivery.") from exc

    def create_read_only_task(
        self,
        *,
        source: str,
        repository_node_id: str,
        base_sha: str,
        request_metadata: dict[str, Any] | None = None,
    ) -> DurableTask:
        """Create an immutable queued task with an audit event; no work is run."""

        if not all(isinstance(value, str) and value.strip() for value in (source, repository_node_id, base_sha)):
            raise TaskStoreError("source, repository_node_id, and base_sha are required.")
        metadata = _validated_metadata(request_metadata)
        task_id = uuid.uuid4()
        metadata_json = json.dumps(metadata, ensure_ascii=False)
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO tasks (task_id, source, repository_node_id, base_sha, state, request_metadata)
                    VALUES (%s, %s, %s, %s, 'queued', %s::jsonb)
                    RETURNING task_id, source, repository_node_id, base_sha, state, created_at
                    """,
                    (task_id, source.strip(), repository_node_id.strip(), base_sha.strip(), metadata_json),
                )
                row = cursor.fetchone()
                cursor.execute(
                    """
                    INSERT INTO task_audit_events (task_id, event_type, event_metadata)
                    VALUES (%s, 'task_created', %s::jsonb)
                    """,
                    (task_id, json.dumps({"source": source.strip()})),
                )
        except Exception as exc:
            raise TaskStoreError("Could not create durable read-only task.") from exc
        return _task_from_row(row)

    def accept_verified_delivery_and_create_task(
        self,
        *,
        delivery_id: str,
        event_name: str,
        payload: bytes,
        source: str,
        repository_node_id: str,
        base_sha: str,
        request_metadata: dict[str, Any] | None = None,
    ) -> DeliveryTaskAcceptance:
        """Atomically create one task for a verified delivery, or return its prior task.

        A webhook endpoint must call this method rather than separately recording
        a receipt and then creating a task. Both mutations share one database
        transaction, preventing a crash from consuming a delivery without work.
        """

        if not _valid_delivery_input(delivery_id, event_name, payload):
            raise TaskStoreError("A delivery ID, event name, and byte payload are required.")
        if not all(isinstance(value, str) and value.strip() for value in (source, repository_node_id, base_sha)):
            raise TaskStoreError("source, repository_node_id, and base_sha are required.")
        metadata = _validated_metadata(request_metadata)
        digest = hashlib.sha256(payload).hexdigest()
        task_id = uuid.uuid4()
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO webhook_deliveries (delivery_id, event_name, payload_sha256)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (delivery_id) DO NOTHING
                    RETURNING delivery_id
                    """,
                    (delivery_id, event_name, digest),
                )
                delivery_was_new = cursor.fetchone() is not None
                if not delivery_was_new:
                    cursor.execute(
                        "SELECT event_name, payload_sha256 FROM webhook_deliveries WHERE delivery_id = %s",
                        (delivery_id,),
                    )
                    existing_delivery = cursor.fetchone()
                    if (
                        existing_delivery is None
                        or existing_delivery["payload_sha256"] != digest
                        or existing_delivery["event_name"] != event_name
                    ):
                        raise TaskStoreError("Webhook delivery ID was replayed with different event data.")
                    cursor.execute(
                        """
                        SELECT task_id, source, repository_node_id, base_sha, state, created_at
                        FROM tasks WHERE source_delivery_id = %s
                        """,
                        (delivery_id,),
                    )
                    existing_task = cursor.fetchone()
                    if existing_task is None:
                        raise TaskStoreError("Webhook delivery exists without its durable task.")
                    return DeliveryTaskAcceptance(False, _task_from_row(existing_task))

                cursor.execute(
                    """
                    INSERT INTO tasks (
                        task_id, source, repository_node_id, base_sha, state,
                        source_delivery_id, request_metadata
                    ) VALUES (%s, %s, %s, %s, 'queued', %s, %s::jsonb)
                    RETURNING task_id, source, repository_node_id, base_sha, state, created_at
                    """,
                    (
                        task_id, source.strip(), repository_node_id.strip(), base_sha.strip(),
                        delivery_id, json.dumps(metadata, ensure_ascii=False),
                    ),
                )
                row = cursor.fetchone()
                cursor.execute(
                    """
                    INSERT INTO task_audit_events (task_id, event_type, event_metadata)
                    VALUES (%s, 'webhook_task_created', %s::jsonb)
                    """,
                    (task_id, json.dumps({"delivery_id": delivery_id, "event_name": event_name})),
                )
        except TaskStoreError:
            raise
        except Exception as exc:
            raise TaskStoreError("Could not atomically accept GitHub delivery and create task.") from exc
        return DeliveryTaskAcceptance(True, _task_from_row(row))

    def get_task(self, task_id: str) -> DurableTask | None:
        """Return one task's non-sensitive summary, without worker artifacts."""
        parsed_id = _parse_task_id(task_id)
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT task_id, source, repository_node_id, base_sha, state, created_at "
                    "FROM tasks WHERE task_id = %s", (parsed_id,)
                )
                row = cursor.fetchone()
        except Exception as exc:
            raise TaskStoreError("Could not retrieve durable task.") from exc
        return _task_from_row(row) if row else None

    def list_task_audit_events(self, task_id: str, *, limit: int = 50) -> list[TaskAuditEvent]:
        """Return bounded, sanitized audit data for one durable task."""
        parsed_id = _parse_task_id(task_id)
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise TaskStoreError("Audit event limit must be between 1 and 100.")
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT event_id, event_type, event_metadata, created_at "
                    "FROM task_audit_events WHERE task_id = %s ORDER BY event_id ASC LIMIT %s",
                    (parsed_id, limit),
                )
                rows = cursor.fetchall()
        except Exception as exc:
            raise TaskStoreError("Could not retrieve task audit events.") from exc
        return [
            TaskAuditEvent(row["event_id"], row["event_type"], dict(row["event_metadata"]), row["created_at"])
            for row in rows
        ]


def _valid_delivery_input(delivery_id: str, event_name: str, payload: bytes) -> bool:
    return (
        isinstance(delivery_id, str) and 1 <= len(delivery_id) <= 128
        and isinstance(event_name, str) and 1 <= len(event_name) <= 100
        and isinstance(payload, bytes) and len(payload) <= 1_000_000
    )


def _validated_metadata(value: dict[str, Any] | None) -> dict[str, str | int | bool | None]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TaskStoreError("request_metadata must be a JSON object.")
    if set(value) - _ALLOWED_METADATA_KEYS:
        raise TaskStoreError("request_metadata contains unsupported keys.")
    result: dict[str, str | int | bool | None] = {}
    for key, item in value.items():
        if _SENSITIVE_METADATA_KEY.search(key):
            raise TaskStoreError("request_metadata must not contain credentials.")
        if item is not None and not isinstance(item, (str, int, bool)):
            raise TaskStoreError("request_metadata values must be scalar.")
        if isinstance(item, str) and (len(item) > 500 or _GITHUB_CREDENTIAL.search(item)):
            raise TaskStoreError("request_metadata must not contain credentials or oversized text.")
        result[key] = item
    return result


def _task_from_row(row: dict[str, Any]) -> DurableTask:
    return DurableTask(
        task_id=str(row["task_id"]),
        source=row["source"],
        repository_node_id=row["repository_node_id"],
        base_sha=row["base_sha"],
        state=row["state"],
        created_at=row["created_at"],
    )


def _parse_task_id(value: str) -> uuid.UUID:
    if not isinstance(value, str):
        raise TaskStoreError("Task ID must be a UUID string.")
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise TaskStoreError("Task ID must be a UUID string.") from exc
