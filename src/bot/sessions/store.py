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

SCHEMA_VERSION = 3


class SQLiteSessionStore(EventSink):
    def __init__(self, path: Path, sanitizer=None) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._sanitizer = sanitizer or (lambda value: value)
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
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cost_usd REAL,
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
                    scope TEXT NOT NULL DEFAULT 'once',
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approval_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tool_name TEXT NOT NULL,
                    action_fingerprint TEXT NOT NULL UNIQUE,
                    arguments_json TEXT NOT NULL,
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
            approval_columns = {
                row[1] for row in self._connection.execute("PRAGMA table_info(approvals)")
            }
            if "scope" not in approval_columns:
                self._connection.execute(
                    "ALTER TABLE approvals ADD COLUMN scope TEXT NOT NULL DEFAULT 'once'"
                )
            run_columns = {row[1] for row in self._connection.execute("PRAGMA table_info(runs)")}
            if "input_tokens" not in run_columns:
                self._connection.execute(
                    "ALTER TABLE runs ADD COLUMN input_tokens INTEGER NOT NULL DEFAULT 0"
                )
            if "output_tokens" not in run_columns:
                self._connection.execute(
                    "ALTER TABLE runs ADD COLUMN output_tokens INTEGER NOT NULL DEFAULT 0"
                )
            if "cost_usd" not in run_columns:
                self._connection.execute("ALTER TABLE runs ADD COLUMN cost_usd REAL")
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

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT id, workspace, parent_session_id, created_at, updated_at
                FROM sessions WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def fork_session(self, session_id: str, *, up_to_position: int | None = None) -> str:
        source = self.get_session(session_id)
        if source is None:
            raise ValueError(f"会话不存在: {session_id}")
        new_session_id = self.create_session(
            Path(source["workspace"]), parent_session_id=session_id
        )
        query = (
            "SELECT position, role, content, message_json, created_at "
            "FROM messages WHERE session_id = ?"
        )
        arguments: list[Any] = [session_id]
        if up_to_position is not None:
            query += " AND position <= ?"
            arguments.append(up_to_position)
        query += " ORDER BY position"
        with self._lock, self._connection:
            rows = self._connection.execute(query, tuple(arguments)).fetchall()
            self._connection.executemany(
                """
                INSERT INTO messages(
                    session_id, run_id, position, role, content, message_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        new_session_id,
                        f"fork:{session_id}",
                        row["position"],
                        row["role"],
                        row["content"],
                        row["message_json"],
                        row["created_at"],
                    )
                    for row in rows
                ],
            )
        return new_session_id

    def list_events(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, run_id, sequence, type, timestamp, payload_json
                FROM events WHERE session_id = ?
                ORDER BY timestamp DESC, sequence DESC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in reversed(rows):
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            events.append(item)
        return events

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

    def finish_run(
        self,
        run_id: str,
        status: str,
        error: str | None = None,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float | None = None,
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE runs SET status = ?, completed_at = ?, error = ?,
                    input_tokens = ?, output_tokens = ?, cost_usd = ?
                WHERE id = ?
                """,
                (
                    status,
                    datetime.now(UTC).isoformat(),
                    error,
                    input_tokens,
                    output_tokens,
                    cost_usd,
                    run_id,
                ),
            )

    async def publish(self, event: AgentEvent) -> None:
        payload = json.dumps(self._sanitizer(event.payload), ensure_ascii=False)
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
        message = ChatMessage.model_validate(self._sanitizer(message.model_dump(mode="python")))
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
            existing = self._connection.execute(
                """
                SELECT id FROM tool_runs
                WHERE session_id = ? AND run_id = ? AND tool_call_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (session_id, run_id, tool_call_id),
            ).fetchone()
            if existing:
                self._connection.execute(
                    """
                    UPDATE tool_runs SET status = ?, result_json = ? WHERE id = ?
                    """,
                    (
                        status,
                        json.dumps(self._sanitizer(result), ensure_ascii=False)
                        if result is not None
                        else None,
                        existing["id"],
                    ),
                )
                return
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
                    json.dumps(self._sanitizer(arguments), ensure_ascii=False),
                    status,
                    json.dumps(self._sanitizer(result), ensure_ascii=False)
                    if result is not None
                    else None,
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
        scope: str,
        reason: str,
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO approvals(
                    session_id, run_id, tool_call_id, decision, scope, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    run_id,
                    tool_call_id,
                    decision,
                    scope,
                    reason,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def save_approval_rule(
        self,
        *,
        tool_name: str,
        action_fingerprint: str,
        arguments: dict[str, Any],
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT OR REPLACE INTO approval_rules(
                    tool_name, action_fingerprint, arguments_json, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    tool_name,
                    action_fingerprint,
                    json.dumps(self._sanitizer(arguments), ensure_ascii=False, sort_keys=True),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def has_approval_rule(self, action_fingerprint: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM approval_rules WHERE action_fingerprint = ?",
                (action_fingerprint,),
            ).fetchone()
        return row is not None

    def add_memory(self, content: str, *, source: str = "user") -> int:
        content = self._sanitizer(content.strip())
        if not content:
            raise ValueError("记忆内容不能为空")
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT INTO memories(content, source, created_at) VALUES (?, ?, ?)",
                (content, source, datetime.now(UTC).isoformat()),
            )
        return int(cursor.lastrowid)

    def list_memories(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, content, source, created_at
                FROM memories WHERE deleted_at IS NULL ORDER BY id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_memory(self, memory_id: int) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE memories SET deleted_at = ?
                WHERE id = ? AND deleted_at IS NULL
                """,
                (datetime.now(UTC).isoformat(), memory_id),
            )
        return cursor.rowcount > 0
