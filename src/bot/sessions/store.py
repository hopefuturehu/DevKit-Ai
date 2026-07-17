from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from bot.core.events import AgentEvent, EventSink
from bot.core.models import ChatMessage

SCHEMA_VERSION = 1


class SQLiteSessionStore(EventSink):
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _migrate(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    workspace TEXT NOT NULL,
                    parent_session_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(parent_session_id) REFERENCES sessions(id)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE INDEX IF NOT EXISTS idx_events_session_sequence
                    ON events(session_id, sequence);
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT,
                    message_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session_position
                    ON messages(session_id, position);
                CREATE TABLE IF NOT EXISTS tool_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    deleted_at TEXT
                );
                """
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, datetime.now(UTC).isoformat()),
            )

    def create_session(
        self,
        workspace: Path,
        *,
        session_id: str | None = None,
        parent_session_id: str | None = None,
    ) -> str:
        session_id = session_id or uuid4().hex
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO sessions(id, workspace, parent_session_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, str(workspace.resolve()), parent_session_id, now, now),
            )
        return session_id

    def ensure_session(self, session_id: str, workspace: Path) -> str:
        if self.session_exists(session_id):
            return session_id
        return self.create_session(workspace, session_id=session_id)

    def session_exists(self, session_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return row is not None

    def latest_session(self, workspace: Path | None = None) -> str | None:
        query = "SELECT id FROM sessions"
        arguments: tuple[Any, ...] = ()
        if workspace:
            query += " WHERE workspace = ?"
            arguments = (str(workspace.resolve()),)
        query += " ORDER BY updated_at DESC LIMIT 1"
        with self._lock:
            row = self._connection.execute(query, arguments).fetchone()
        return str(row["id"]) if row else None

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, workspace, parent_session_id, created_at, updated_at
                FROM sessions ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def start_run(self, session_id: str, run_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO runs(id, session_id, status, started_at) VALUES (?, ?, ?, ?)",
                (run_id, session_id, "running", now),
            )
            self._connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id)
            )

    def finish_run(self, run_id: str, status: str, error: str | None = None) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE runs SET status = ?, completed_at = ?, error = ? WHERE id = ?",
                (status, datetime.now(UTC).isoformat(), error, run_id),
            )

    async def publish(self, event: AgentEvent) -> None:
        payload = json.dumps(event.payload, ensure_ascii=False)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO events(id, session_id, run_id, sequence, type, timestamp, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.session_id,
                    event.run_id,
                    event.sequence,
                    event.type.value,
                    event.timestamp.isoformat(),
                    payload,
                ),
            )
            self._connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (event.timestamp.isoformat(), event.session_id),
            )

    def append_message(self, session_id: str, run_id: str, message: ChatMessage) -> None:
        with self._lock, self._connection:
            position = self._connection.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            self._connection.execute(
                """
                INSERT INTO messages(
                    session_id, run_id, position, role, content, message_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    run_id,
                    position,
                    message.role.value,
                    message.content,
                    message.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def load_messages(self, session_id: str) -> list[ChatMessage]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT message_json FROM messages WHERE session_id = ? ORDER BY position",
                (session_id,),
            ).fetchall()
        return [ChatMessage.model_validate_json(row["message_json"]) for row in rows]

    def record_tool_run(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        status: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO tool_runs(
                    session_id, run_id, tool_call_id, tool_name, arguments_json,
                    status, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    run_id,
                    tool_call_id,
                    tool_name,
                    json.dumps(arguments, ensure_ascii=False),
                    status,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def record_approval(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_call_id: str,
        decision: str,
        reason: str,
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO approvals(
                    session_id, run_id, tool_call_id, decision, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    run_id,
                    tool_call_id,
                    decision,
                    reason,
                    datetime.now(UTC).isoformat(),
                ),
            )
