"""SQLite persistence for DevPilot investigation reports."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Keep app data separate from the repositories being inspected.
DATA_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = DATA_DIR / "investigations.sqlite3"


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_store() -> None:
    """Create the history table if it does not exist yet."""
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS investigations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repository_id TEXT NOT NULL,
                repository_name TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL,
                answer TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                iterations INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_investigations_created_at "
            "ON investigations(created_at DESC)"
        )


def save_investigation(
    *,
    repository_id: str,
    repository_name: str,
    description: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Persist a report and return the saved record."""
    created_at = datetime.now(timezone.utc).isoformat()
    evidence = result.get("evidence") or []
    with _connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO investigations (
                repository_id, repository_name, description, status,
                answer, evidence_json, iterations, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                repository_id,
                repository_name,
                description,
                str(result.get("status", "unknown")),
                str(result.get("answer", "")),
                json.dumps(evidence, ensure_ascii=False),
                result.get("iterations"),
                created_at,
            ),
        )
        record_id = cursor.lastrowid

    return get_investigation(record_id)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "repository_id": row["repository_id"],
        "repository_name": row["repository_name"],
        "description": row["description"],
        "status": row["status"],
        "answer": row["answer"],
        "evidence": json.loads(row["evidence_json"]),
        "iterations": row["iterations"],
        "created_at": row["created_at"],
    }


def list_investigations(limit: int = 50) -> list[dict[str, Any]]:
    """Return the newest investigation records first."""
    safe_limit = max(1, min(int(limit), 100))
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM investigations ORDER BY id DESC LIMIT ?",
            (safe_limit,),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_investigation(record_id: int) -> dict[str, Any] | None:
    """Return one saved report, or None if it does not exist."""
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM investigations WHERE id = ?",
            (record_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None
