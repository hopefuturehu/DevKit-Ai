from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from bot.core.context import ContextSnapshot, PositionedMessage, SnapshotStatus
from bot.core.events import AgentEvent, EventSink
from bot.core.models import ChatMessage

SCHEMA_VERSION = 12


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
                    schema_version INTEGER NOT NULL DEFAULT 1,
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
                CREATE TABLE IF NOT EXISTS progress_states (
                    session_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id),
                    FOREIGN KEY(run_id) REFERENCES runs(id)
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
                CREATE TABLE IF NOT EXISTS memory_extraction_runs (
                    run_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    model TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    candidate_count INTEGER NOT NULL DEFAULT 0,
                    added_count INTEGER NOT NULL DEFAULT 0,
                    merged_count INTEGER NOT NULL DEFAULT 0,
                    conflict_count INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT,
                    FOREIGN KEY(run_id) REFERENCES runs(id),
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_extraction_status_started
                    ON memory_extraction_runs(status, started_at);
                CREATE TABLE IF NOT EXISTS context_compactions (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    parent_id TEXT,
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL,
                    model TEXT NOT NULL,
                    covered_start_position INTEGER NOT NULL,
                    covered_end_position INTEGER NOT NULL,
                    delta_start_position INTEGER NOT NULL,
                    summary_text TEXT NOT NULL DEFAULT '',
                    summary_token_estimate INTEGER NOT NULL DEFAULT 0,
                    source_sha256 TEXT NOT NULL,
                    source_refs_json TEXT NOT NULL DEFAULT '[]',
                    anchor_positions_json TEXT NOT NULL DEFAULT '[]',
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    source_chars INTEGER NOT NULL DEFAULT 0,
                    duration_ms REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    ready_at TEXT,
                    superseded_at TEXT,
                    error TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(id),
                    FOREIGN KEY(parent_id) REFERENCES context_compactions(id)
                );
                CREATE INDEX IF NOT EXISTS idx_context_compactions_session_created
                    ON context_compactions(session_id, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_context_compactions_one_ready
                    ON context_compactions(session_id) WHERE status = 'ready';
                CREATE UNIQUE INDEX IF NOT EXISTS idx_context_compactions_one_building
                    ON context_compactions(session_id) WHERE status = 'building';
                CREATE TABLE IF NOT EXISTS context_snapshots (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    cursor_position INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    token_estimate INTEGER NOT NULL,
                    core_policy_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    ready_at TEXT,
                    superseded_at TEXT,
                    error TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE INDEX IF NOT EXISTS idx_context_snapshots_ready
                    ON context_snapshots(session_id, status, cursor_position DESC);
                CREATE TABLE IF NOT EXISTS context_snapshot_refs (
                    snapshot_id TEXT NOT NULL,
                    ref_type TEXT NOT NULL,
                    ref_value TEXT NOT NULL,
                    PRIMARY KEY(snapshot_id, ref_type, ref_value),
                    FOREIGN KEY(snapshot_id) REFERENCES context_snapshots(id)
                );
                CREATE TABLE IF NOT EXISTS context_blobs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    byte_count INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    content BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, sha256),
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE INDEX IF NOT EXISTS idx_context_blobs_session_created
                    ON context_blobs(session_id, created_at);
                CREATE TABLE IF NOT EXISTS context_blob_access (
                    session_id TEXT NOT NULL,
                    blob_id TEXT NOT NULL,
                    granted_at TEXT NOT NULL,
                    PRIMARY KEY(session_id, blob_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(id),
                    FOREIGN KEY(blob_id) REFERENCES context_blobs(id)
                );
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    id TEXT PRIMARY KEY,
                    parent_session_id TEXT NOT NULL,
                    parent_run_id TEXT NOT NULL,
                    child_session_id TEXT NOT NULL UNIQUE,
                    agent_name TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    constraints_json TEXT NOT NULL,
                    acceptance_criteria_json TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    context_refs_json TEXT NOT NULL,
                    required INTEGER NOT NULL DEFAULT 1,
                    isolation TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    status TEXT NOT NULL,
                    owner_id TEXT,
                    idempotency_key TEXT,
                    cancel_requested_at TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    reported_at TEXT,
                    result_json TEXT,
                    error TEXT,
                    FOREIGN KEY(parent_session_id) REFERENCES sessions(id),
                    FOREIGN KEY(child_session_id) REFERENCES sessions(id),
                    UNIQUE(parent_session_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_tasks_parent_created
                    ON agent_tasks(parent_session_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_agent_tasks_status_created
                    ON agent_tasks(status, created_at);
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                    session_id UNINDEXED,
                    position UNINDEXED,
                    content
                );
                CREATE TRIGGER IF NOT EXISTS messages_fts_insert
                AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(rowid, session_id, position, content)
                    VALUES (new.id, new.session_id, new.position, COALESCE(new.content, ''));
                END;
                CREATE TRIGGER IF NOT EXISTS messages_fts_delete
                AFTER DELETE ON messages BEGIN
                    DELETE FROM messages_fts WHERE rowid = old.id;
                END;
                CREATE TRIGGER IF NOT EXISTS messages_fts_update
                AFTER UPDATE OF session_id, position, content ON messages BEGIN
                    DELETE FROM messages_fts WHERE rowid = old.id;
                    INSERT INTO messages_fts(rowid, session_id, position, content)
                    VALUES (new.id, new.session_id, new.position, COALESCE(new.content, ''));
                END;
                """
            )
            self._connection.execute(
                """
                INSERT INTO messages_fts(rowid, session_id, position, content)
                SELECT m.id, m.session_id, m.position, COALESCE(m.content, '')
                FROM messages AS m
                WHERE NOT EXISTS (
                    SELECT 1 FROM messages_fts AS f WHERE f.rowid = m.id
                )
                """
            )
            self._connection.execute(
                """
                INSERT OR IGNORE INTO context_blob_access(session_id, blob_id, granted_at)
                SELECT session_id, id, created_at FROM context_blobs
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
            event_columns = {
                row[1] for row in self._connection.execute("PRAGMA table_info(events)")
            }
            if "schema_version" not in event_columns:
                self._connection.execute(
                    "ALTER TABLE events ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1"
                )
            stale_build_cutoff = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
            self._connection.execute(
                """
                UPDATE context_compactions
                SET status = 'failed',
                    error = 'recovered_stale_build'
                WHERE status = 'building' AND created_at < ?
                """,
                (stale_build_cutoff,),
            )
            self._connection.execute(
                """
                UPDATE memory_extraction_runs
                SET status = 'failed',
                    completed_at = ?,
                    error = 'recovered_stale_build'
                WHERE status = 'building' AND started_at < ?
                """,
                (datetime.now(UTC).isoformat(), stale_build_cutoff),
            )
            applied_at = datetime.now(UTC).isoformat()
            self._connection.executemany(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                [(version, applied_at) for version in range(1, SCHEMA_VERSION + 1)],
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
        existing = self.get_session(session_id)
        if existing is not None:
            if Path(existing["workspace"]).resolve() != workspace.resolve():
                raise ValueError(f"会话 {session_id} 属于其他工作区: {existing['workspace']}")
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

    def update_session_workspace(self, session_id: str, workspace: Path) -> None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE sessions SET workspace = ?, updated_at = ? WHERE id = ?",
                (str(workspace.resolve()), datetime.now(UTC).isoformat(), session_id),
            )
        if cursor.rowcount != 1:
            raise ValueError(f"会话不存在: {session_id}")

    def session_usage(self, session_id: str) -> dict[str, int | float]:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT COUNT(*) AS runs,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(cost_usd), 0) AS cost_usd
                FROM runs WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return {
            "runs": int(row["runs"]),
            "input_tokens": int(row["input_tokens"]),
            "output_tokens": int(row["output_tokens"]),
            "cost_usd": float(row["cost_usd"]),
        }

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
            max_copied_position = (
                max((int(row["position"]) for row in rows), default=0)
                if up_to_position is None
                else up_to_position
            )
            now = datetime.now(UTC).isoformat()
            compaction = self._connection.execute(
                """
                SELECT * FROM context_compactions
                WHERE session_id = ? AND status IN ('ready', 'superseded')
                  AND covered_end_position <= ?
                ORDER BY covered_end_position DESC, ready_at DESC
                LIMIT 1
                """,
                (session_id, max_copied_position),
            ).fetchone()
            if compaction is not None:
                self._connection.execute(
                    """
                    INSERT INTO context_compactions(
                        id, session_id, parent_id, trigger, status, model,
                        covered_start_position, covered_end_position,
                        delta_start_position, summary_text,
                        summary_token_estimate, source_sha256,
                        source_refs_json, anchor_positions_json,
                        input_tokens, output_tokens, source_chars, duration_ms,
                        created_at, ready_at
                    ) VALUES (?, ?, NULL, 'fork', 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uuid4().hex,
                        new_session_id,
                        compaction["model"],
                        compaction["covered_start_position"],
                        compaction["covered_end_position"],
                        compaction["delta_start_position"],
                        compaction["summary_text"],
                        compaction["summary_token_estimate"],
                        compaction["source_sha256"],
                        compaction["source_refs_json"],
                        compaction["anchor_positions_json"],
                        compaction["input_tokens"],
                        compaction["output_tokens"],
                        compaction["source_chars"],
                        compaction["duration_ms"],
                        now,
                        now,
                    ),
                )
        blob_references = {
            reference
            for row in rows
            for reference in re.findall(r"blob:[0-9a-f]{64}", row["message_json"])
        }
        if blob_references:
            granted_at = datetime.now(UTC).isoformat()
            with self._lock, self._connection:
                self._connection.executemany(
                    """
                    INSERT OR IGNORE INTO context_blob_access(session_id, blob_id, granted_at)
                    SELECT ?, id, ? FROM context_blobs WHERE id = ?
                    """,
                    [
                        (new_session_id, granted_at, reference)
                        for reference in sorted(blob_references)
                    ],
                )
        return new_session_id

    def list_events(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, run_id, schema_version, sequence, type, timestamp, payload_json
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

    def load_progress_state(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT run_id, state_json, updated_at FROM progress_states WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "run_id": str(row["run_id"]),
            "state": json.loads(str(row["state_json"])),
            "updated_at": str(row["updated_at"]),
        }

    def save_progress_state(
        self,
        *,
        session_id: str,
        run_id: str,
        state: dict[str, Any],
    ) -> None:
        now = datetime.now(UTC).isoformat()
        payload = json.dumps(self._sanitizer(state), ensure_ascii=False, sort_keys=True)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO progress_states(session_id, run_id, state_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    run_id = excluded.run_id,
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (session_id, run_id, payload, now),
            )

    def clear_progress_state(self, session_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM progress_states WHERE session_id = ?",
                (session_id,),
            )

    async def publish(self, event: AgentEvent) -> None:
        payload = json.dumps(self._sanitizer(event.payload), ensure_ascii=False)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO events(
                    id, session_id, run_id, schema_version, sequence, type, timestamp, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.session_id,
                    event.run_id,
                    event.schema_version,
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

    def append_message(self, session_id: str, run_id: str, message: ChatMessage) -> int:
        message = ChatMessage.model_validate(self._sanitizer(message.model_dump(mode="python")))
        if error := message.assistant_payload_error():
            raise ValueError(f"拒绝持久化无效消息: {error}")
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
        return int(position)

    def append_message_and_mark_agent_tasks_reported(
        self,
        *,
        session_id: str,
        run_id: str,
        message: ChatMessage,
        task_ids: list[str],
    ) -> int:
        """Atomically persist a parent-visible result and acknowledge its tasks."""
        message = ChatMessage.model_validate(self._sanitizer(message.model_dump(mode="python")))
        if error := message.assistant_payload_error():
            raise ValueError(f"拒绝持久化无效消息: {error}")
        unique_task_ids = list(dict.fromkeys(task_ids))
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
            if unique_task_ids:
                placeholders = ",".join("?" for _ in unique_task_ids)
                self._connection.execute(
                    f"""
                    UPDATE agent_tasks SET reported_at = ?
                    WHERE parent_session_id = ? AND id IN ({placeholders})
                        AND status IN (
                            'completed', 'blocked', 'failed', 'limit_reached',
                            'cancelled', 'interrupted'
                        )
                    """,
                    (
                        datetime.now(UTC).isoformat(),
                        session_id,
                        *unique_task_ids,
                    ),
                )
        return int(position)

    def append_messages_and_mark_agent_tasks_reported(
        self,
        *,
        session_id: str,
        run_id: str,
        messages: list[ChatMessage],
        task_ids: list[str],
    ) -> list[int]:
        """Atomically append a synthetic Tool exchange and acknowledge results."""
        if not messages:
            return []
        sanitized_messages = [
            ChatMessage.model_validate(self._sanitizer(message.model_dump(mode="python")))
            for message in messages
        ]
        for message in sanitized_messages:
            if error := message.assistant_payload_error():
                raise ValueError(f"拒绝持久化无效消息: {error}")
        unique_task_ids = list(dict.fromkeys(task_ids))
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connection:
            first_position = int(
                self._connection.execute(
                    "SELECT COALESCE(MAX(position), 0) + 1 FROM messages WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
            )
            positions = [first_position + index for index in range(len(sanitized_messages))]
            self._connection.executemany(
                """
                INSERT INTO messages(
                    session_id, run_id, position, role, content, message_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        session_id,
                        run_id,
                        position,
                        message.role.value,
                        message.content,
                        message.model_dump_json(),
                        now,
                    )
                    for position, message in zip(positions, sanitized_messages, strict=True)
                ],
            )
            if unique_task_ids:
                placeholders = ",".join("?" for _ in unique_task_ids)
                self._connection.execute(
                    f"""
                    UPDATE agent_tasks SET reported_at = ?
                    WHERE parent_session_id = ? AND id IN ({placeholders})
                        AND status IN (
                            'completed', 'blocked', 'failed', 'limit_reached',
                            'cancelled', 'interrupted'
                        )
                    """,
                    (now, session_id, *unique_task_ids),
                )
        return positions

    def load_messages(
        self,
        session_id: str,
        *,
        after_position: int = 0,
        through_position: int | None = None,
    ) -> list[ChatMessage]:
        return [
            entry.message
            for entry in self.load_positioned_messages(
                session_id,
                after_position=after_position,
                through_position=through_position,
            )
        ]

    def load_positioned_messages(
        self,
        session_id: str,
        *,
        after_position: int = 0,
        through_position: int | None = None,
    ) -> list[PositionedMessage]:
        query = "SELECT position, message_json FROM messages WHERE session_id = ? AND position > ?"
        arguments: list[Any] = [session_id, after_position]
        if through_position is not None:
            query += " AND position <= ?"
            arguments.append(through_position)
        query += " ORDER BY position"
        with self._lock:
            rows = self._connection.execute(query, tuple(arguments)).fetchall()
        return [
            PositionedMessage(
                position=int(row["position"]),
                message=ChatMessage.model_validate_json(row["message_json"]),
            )
            for row in rows
        ]

    def latest_message_position(self, session_id: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(position), 0) AS position FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row["position"])

    def start_context_compaction(
        self,
        *,
        session_id: str,
        parent_id: str | None,
        trigger: str,
        model: str,
        covered_start_position: int,
        covered_end_position: int,
        delta_start_position: int,
        source_sha256: str,
        anchor_positions: list[int],
        source_chars: int,
    ) -> str:
        if covered_start_position <= 0 or covered_end_position < covered_start_position:
            raise ValueError("上下文压缩覆盖范围无效")
        if not covered_start_position <= delta_start_position <= covered_end_position:
            raise ValueError("上下文压缩增量起点无效")
        compaction_id = uuid4().hex
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connection:
            current = self._connection.execute(
                """
                SELECT id FROM context_compactions
                WHERE session_id = ? AND status = 'ready'
                """,
                (session_id,),
            ).fetchone()
            current_id = str(current["id"]) if current is not None else None
            if current_id != parent_id:
                raise ValueError("上下文压缩父版本已变化，拒绝基于过期摘要继续发布")
            self._connection.execute(
                """
                INSERT INTO context_compactions(
                    id, session_id, parent_id, trigger, status, model,
                    covered_start_position, covered_end_position,
                    delta_start_position, source_sha256, anchor_positions_json,
                    source_chars, created_at
                ) VALUES (?, ?, ?, ?, 'building', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    compaction_id,
                    session_id,
                    parent_id,
                    str(self._sanitizer(trigger)),
                    str(self._sanitizer(model)),
                    covered_start_position,
                    covered_end_position,
                    delta_start_position,
                    source_sha256,
                    json.dumps(sorted(set(anchor_positions))),
                    max(0, source_chars),
                    now,
                ),
            )
        return compaction_id

    def complete_context_compaction(
        self,
        compaction_id: str,
        *,
        summary_text: str,
        summary_token_estimate: int,
        source_refs: list[str],
        input_tokens: int,
        output_tokens: int,
        duration_ms: float,
    ) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        safe_summary = str(self._sanitizer(summary_text))
        safe_refs = self._sanitizer(source_refs)
        with self._lock, self._connection:
            building = self._connection.execute(
                """
                SELECT * FROM context_compactions
                WHERE id = ? AND status = 'building'
                """,
                (compaction_id,),
            ).fetchone()
            if building is None:
                raise ValueError("上下文压缩记录不存在或已完成")
            current = self._connection.execute(
                """
                SELECT id FROM context_compactions
                WHERE session_id = ? AND status = 'ready'
                """,
                (building["session_id"],),
            ).fetchone()
            current_id = str(current["id"]) if current is not None else None
            parent_id = str(building["parent_id"]) if building["parent_id"] is not None else None
            if current_id != parent_id:
                raise ValueError("上下文压缩父版本已变化，生成结果不会推进活动边界")
            if current_id is not None:
                self._connection.execute(
                    """
                    UPDATE context_compactions
                    SET status = 'superseded', superseded_at = ?
                    WHERE id = ? AND status = 'ready'
                    """,
                    (now, current_id),
                )
            cursor = self._connection.execute(
                """
                UPDATE context_compactions
                SET status = 'ready', summary_text = ?,
                    summary_token_estimate = ?, source_refs_json = ?,
                    input_tokens = ?, output_tokens = ?, duration_ms = ?,
                    ready_at = ?, superseded_at = NULL, error = NULL
                WHERE id = ? AND status = 'building'
                """,
                (
                    safe_summary,
                    max(0, summary_token_estimate),
                    json.dumps(safe_refs, ensure_ascii=False),
                    max(0, input_tokens),
                    max(0, output_tokens),
                    max(0.0, duration_ms),
                    now,
                    compaction_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("上下文压缩事务发布失败")
            row = self._connection.execute(
                "SELECT * FROM context_compactions WHERE id = ?",
                (compaction_id,),
            ).fetchone()
        return self._decode_context_compaction(row)

    def fail_context_compaction(self, compaction_id: str, error: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE context_compactions
                SET status = 'failed', error = ?
                WHERE id = ? AND status = 'building'
                """,
                (str(self._sanitizer(error)), compaction_id),
            )

    def get_context_compaction(
        self,
        session_id: str,
        compaction_id: str,
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM context_compactions
                WHERE session_id = ? AND id = ?
                """,
                (session_id, compaction_id),
            ).fetchone()
        return self._decode_context_compaction(row) if row is not None else None

    def latest_ready_context_compaction(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM context_compactions
                WHERE session_id = ? AND status = 'ready'
                ORDER BY covered_end_position DESC, ready_at DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return self._decode_context_compaction(row) if row is not None else None

    def list_context_compactions(
        self,
        session_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM context_compactions
                WHERE session_id = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (session_id, max(1, limit)),
            ).fetchall()
        return [self._decode_context_compaction(row) for row in rows]

    def count_context_compactions(
        self,
        session_id: str,
        *,
        statuses: set[str],
    ) -> int:
        allowed = {"building", "ready", "superseded", "failed"}
        selected = sorted(statuses & allowed)
        if not selected:
            return 0
        placeholders = ",".join("?" for _ in selected)
        with self._lock:
            row = self._connection.execute(
                f"""
                SELECT COUNT(*) AS count FROM context_compactions
                WHERE session_id = ? AND status IN ({placeholders})
                """,
                (session_id, *selected),
            ).fetchone()
        return int(row["count"])

    def activate_context_compaction(
        self,
        *,
        session_id: str,
        compaction_id: str,
    ) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connection:
            target = self._connection.execute(
                """
                SELECT * FROM context_compactions
                WHERE session_id = ? AND id = ?
                  AND status IN ('ready', 'superseded')
                """,
                (session_id, compaction_id),
            ).fetchone()
            if target is None:
                raise ValueError("可回滚的上下文压缩版本不存在")
            if target["status"] == "ready":
                return self._decode_context_compaction(target)
            current = self._connection.execute(
                """
                SELECT id FROM context_compactions
                WHERE session_id = ? AND status = 'ready'
                """,
                (session_id,),
            ).fetchone()
            if current is not None:
                self._connection.execute(
                    """
                    UPDATE context_compactions
                    SET status = 'superseded', superseded_at = ?
                    WHERE id = ? AND status = 'ready'
                    """,
                    (now, current["id"]),
                )
            cursor = self._connection.execute(
                """
                UPDATE context_compactions
                SET status = 'ready', ready_at = ?, superseded_at = NULL,
                    error = NULL
                WHERE id = ? AND session_id = ? AND status = 'superseded'
                """,
                (now, compaction_id, session_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("上下文压缩回滚事务失败")
            row = self._connection.execute(
                "SELECT * FROM context_compactions WHERE id = ?",
                (compaction_id,),
            ).fetchone()
        return self._decode_context_compaction(row)

    def invalidate_context_compaction(
        self,
        *,
        session_id: str,
        compaction_id: str,
        error: str,
    ) -> dict[str, Any] | None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connection:
            current = self._connection.execute(
                """
                SELECT * FROM context_compactions
                WHERE session_id = ? AND id = ? AND status = 'ready'
                """,
                (session_id, compaction_id),
            ).fetchone()
            if current is None:
                fallback = self._connection.execute(
                    """
                    SELECT * FROM context_compactions
                    WHERE session_id = ? AND status = 'ready'
                    ORDER BY covered_end_position DESC, ready_at DESC
                    LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                return self._decode_context_compaction(fallback) if fallback is not None else None
            self._connection.execute(
                """
                UPDATE context_compactions
                SET status = 'failed', error = ?, superseded_at = ?
                WHERE id = ? AND status = 'ready'
                """,
                (str(self._sanitizer(error)), now, compaction_id),
            )
            parent = None
            if current["parent_id"] is not None:
                self._connection.execute(
                    """
                    UPDATE context_compactions
                    SET status = 'ready', ready_at = ?, superseded_at = NULL
                    WHERE id = ? AND session_id = ? AND status = 'superseded'
                    """,
                    (now, current["parent_id"], session_id),
                )
                parent = self._connection.execute(
                    """
                    SELECT * FROM context_compactions
                    WHERE id = ? AND session_id = ? AND status = 'ready'
                    """,
                    (current["parent_id"], session_id),
                ).fetchone()
        return self._decode_context_compaction(parent) if parent is not None else None

    def read_context_compaction_source(
        self,
        *,
        requesting_session_id: str,
        compaction_id: str,
        start_position: int | None = None,
        end_position: int | None = None,
    ) -> dict[str, Any] | None:
        record = self.get_context_compaction(requesting_session_id, compaction_id)
        if record is None:
            return None
        covered_start = int(record["covered_start_position"])
        covered_end = int(record["covered_end_position"])
        selected_start = max(covered_start, start_position or covered_start)
        selected_end = min(covered_end, end_position or covered_end)
        if selected_start > selected_end:
            raise ValueError("请求的压缩来源范围无效")
        entries = self.load_positioned_messages(
            requesting_session_id,
            after_position=selected_start - 1,
            through_position=selected_end,
        )
        return {
            "compaction": record,
            "requested_range": [selected_start, selected_end],
            "messages": [
                {
                    "position": entry.position,
                    "message": entry.message.model_dump(mode="json"),
                }
                for entry in entries
            ],
        }

    def search_session_messages(
        self,
        session_id: str,
        query: str,
        *,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        normalized = " ".join(query.split()).strip()
        if not normalized:
            return []
        terms = re.findall(r"[\w./:@-]+", normalized, flags=re.UNICODE)
        fts_query = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms[:12])
        rows: list[sqlite3.Row] = []
        if fts_query:
            try:
                with self._lock:
                    rows = self._connection.execute(
                        """
                        SELECT m.position, m.role, m.content, m.message_json,
                               bm25(messages_fts) AS rank
                        FROM messages_fts
                        JOIN messages AS m ON m.id = messages_fts.rowid
                        WHERE messages_fts MATCH ?
                          AND messages_fts.session_id = ?
                        ORDER BY rank, m.position DESC LIMIT ?
                        """,
                        (fts_query, session_id, max(1, min(limit, 50))),
                    ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            with self._lock:
                rows = self._connection.execute(
                    """
                    SELECT position, role, content, message_json, 0.0 AS rank
                    FROM messages
                    WHERE session_id = ? AND content LIKE ? ESCAPE '\\'
                    ORDER BY position DESC LIMIT ?
                    """,
                    (
                        session_id,
                        f"%{self._escape_like(normalized)}%",
                        max(1, min(limit, 50)),
                    ),
                ).fetchall()
        return [
            {
                "position": int(row["position"]),
                "role": str(row["role"]),
                "content": row["content"],
                "message": ChatMessage.model_validate_json(row["message_json"]).model_dump(
                    mode="json"
                ),
                "rank": float(row["rank"]),
            }
            for row in rows
        ]

    @staticmethod
    def _decode_context_compaction(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["source_refs"] = json.loads(item.pop("source_refs_json"))
        item["anchor_positions"] = json.loads(item.pop("anchor_positions_json"))
        return item

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def save_context_snapshot(
        self,
        *,
        session_id: str,
        run_id: str,
        snapshot: ContextSnapshot,
    ) -> str:
        """Commit a checkpoint with a building -> ready transition.

        Existing ready checkpoints are superseded only after the new row is fully
        written, so an interrupted compaction never destroys the recovery point.
        """
        snapshot_id = uuid4().hex
        now = datetime.now(UTC).isoformat()
        payload = self._sanitizer(snapshot.model_dump(mode="json"))
        validated = ContextSnapshot.model_validate(payload)
        references = [("evidence", value) for value in validated.evidence_refs]
        references.extend(("file", value) for value in validated.files)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO context_snapshots(
                    id, session_id, run_id, status, cursor_position, snapshot_json,
                    token_estimate, core_policy_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    session_id,
                    run_id,
                    SnapshotStatus.BUILDING.value,
                    validated.cursor_position,
                    validated.model_dump_json(),
                    validated.token_estimate,
                    validated.core_policy_version,
                    now,
                ),
            )
        try:
            with self._lock, self._connection:
                if references:
                    self._connection.executemany(
                        """
                        INSERT OR IGNORE INTO context_snapshot_refs(
                            snapshot_id, ref_type, ref_value
                        ) VALUES (?, ?, ?)
                        """,
                        [(snapshot_id, kind, value) for kind, value in references],
                    )
                self._connection.execute(
                    """
                    UPDATE context_snapshots
                    SET status = ?, superseded_at = ?
                    WHERE session_id = ? AND status = ? AND id <> ?
                    """,
                    (
                        SnapshotStatus.SUPERSEDED.value,
                        now,
                        session_id,
                        SnapshotStatus.READY.value,
                        snapshot_id,
                    ),
                )
                self._connection.execute(
                    """
                    UPDATE context_snapshots SET status = ?, ready_at = ? WHERE id = ?
                    """,
                    (SnapshotStatus.READY.value, now, snapshot_id),
                )
        except Exception as exc:
            with self._lock, self._connection:
                self._connection.execute(
                    """
                    UPDATE context_snapshots SET status = ?, error = ? WHERE id = ?
                    """,
                    (SnapshotStatus.FAILED.value, str(exc), snapshot_id),
                )
            raise
        return snapshot_id

    def latest_context_snapshot(self, session_id: str) -> tuple[str, ContextSnapshot] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT id, snapshot_json FROM context_snapshots
                WHERE session_id = ? AND status = ?
                ORDER BY cursor_position DESC, ready_at DESC LIMIT 1
                """,
                (session_id, SnapshotStatus.READY.value),
            ).fetchone()
        if row is None:
            return None
        return str(row["id"]), ContextSnapshot.model_validate_json(row["snapshot_json"])

    def list_context_snapshots(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, run_id, status, cursor_position, token_estimate,
                       core_policy_version, created_at, ready_at, superseded_at, error
                FROM context_snapshots WHERE session_id = ?
                ORDER BY created_at, id
                """,
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def put_context_blob(
        self,
        *,
        session_id: str,
        run_id: str,
        content: str | bytes,
        media_type: str = "text/plain",
    ) -> str:
        if isinstance(content, str):
            sanitized = self._sanitizer(content)
            raw = str(sanitized).encode("utf-8", errors="replace")
        else:
            raw = content
        digest = hashlib.sha256(raw).hexdigest()
        blob_id = f"blob:{digest}"
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO context_blobs(
                    id, session_id, run_id, media_type, byte_count, sha256, content, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    blob_id,
                    session_id,
                    run_id,
                    media_type,
                    len(raw),
                    digest,
                    raw,
                    datetime.now(UTC).isoformat(),
                ),
            )
            self._connection.execute(
                """
                INSERT OR IGNORE INTO context_blob_access(session_id, blob_id, granted_at)
                VALUES (?, ?, ?)
                """,
                (session_id, blob_id, datetime.now(UTC).isoformat()),
            )
        return blob_id

    def read_context_blob(
        self,
        session_id: str,
        reference: str,
        *,
        offset: int = 0,
        limit: int = 16_000,
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT b.media_type, b.byte_count, b.sha256, b.content
                FROM context_blobs AS b
                JOIN context_blob_access AS a ON a.blob_id = b.id
                WHERE a.session_id = ? AND b.id = ?
                """,
                (session_id, reference),
            ).fetchone()
        if row is None:
            return None
        content = bytes(row["content"])
        chunk = content[max(0, offset) : max(0, offset) + max(1, limit)]
        return {
            "reference": reference,
            "media_type": row["media_type"],
            "byte_count": int(row["byte_count"]),
            "sha256": row["sha256"],
            "offset": max(0, offset),
            "next_offset": max(0, offset) + len(chunk),
            "content": chunk.decode("utf-8", errors="replace"),
            "eof": max(0, offset) + len(chunk) >= len(content),
        }

    def grant_context_blob_access(
        self,
        *,
        source_session_id: str,
        target_session_id: str,
        reference: str,
    ) -> bool:
        """Grant one explicit blob reference after verifying source ownership."""
        with self._lock, self._connection:
            source = self._connection.execute(
                """
                SELECT 1 FROM context_blob_access
                WHERE session_id = ? AND blob_id = ?
                """,
                (source_session_id, reference),
            ).fetchone()
            target = self._connection.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (target_session_id,)
            ).fetchone()
            if source is None or target is None:
                return False
            self._connection.execute(
                """
                INSERT OR IGNORE INTO context_blob_access(session_id, blob_id, granted_at)
                VALUES (?, ?, ?)
                """,
                (target_session_id, reference, datetime.now(UTC).isoformat()),
            )
        return True

    def create_agent_task(
        self,
        *,
        task_id: str,
        parent_session_id: str,
        parent_run_id: str,
        agent_name: str,
        objective: str,
        constraints: list[str],
        acceptance_criteria: list[str],
        spec: dict[str, Any],
        context_refs: list[str],
        required: bool,
        isolation: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        """Atomically create an isolated child session and its queued task."""
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connection:
            if idempotency_key:
                existing = self._connection.execute(
                    """
                    SELECT id FROM agent_tasks
                    WHERE parent_session_id = ? AND idempotency_key = ?
                    """,
                    (parent_session_id, idempotency_key),
                ).fetchone()
                if existing:
                    return self._get_agent_task_locked(str(existing["id"]))
            parent = self._connection.execute(
                "SELECT workspace FROM sessions WHERE id = ?", (parent_session_id,)
            ).fetchone()
            if parent is None:
                raise ValueError(f"父会话不存在: {parent_session_id}")
            child_session_id = uuid4().hex
            workspace = str(Path(parent["workspace"]).resolve())
            self._connection.execute(
                """
                INSERT INTO sessions(id, workspace, parent_session_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (child_session_id, workspace, parent_session_id, now, now),
            )
            self._connection.execute(
                """
                INSERT INTO agent_tasks(
                    id, parent_session_id, parent_run_id, child_session_id,
                    agent_name, objective, constraints_json, acceptance_criteria_json,
                    spec_json, context_refs_json, required, isolation, workspace,
                    status, idempotency_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    task_id,
                    parent_session_id,
                    parent_run_id,
                    child_session_id,
                    agent_name,
                    self._sanitizer(objective),
                    json.dumps(self._sanitizer(constraints), ensure_ascii=False),
                    json.dumps(self._sanitizer(acceptance_criteria), ensure_ascii=False),
                    json.dumps(self._sanitizer(spec), ensure_ascii=False),
                    json.dumps(context_refs, ensure_ascii=False),
                    int(required),
                    isolation,
                    workspace,
                    idempotency_key,
                    now,
                ),
            )
            return self._get_agent_task_locked(task_id)

    def get_agent_task(
        self,
        task_id: str,
        *,
        parent_session_id: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM agent_tasks
                WHERE id = ? AND (? IS NULL OR parent_session_id = ?)
                """,
                (task_id, parent_session_id, parent_session_id),
            ).fetchone()
            return self._decode_agent_task(row) if row else None

    def _get_agent_task_locked(self, task_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM agent_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"子 Agent 任务不存在: {task_id}")
        return self._decode_agent_task(row)

    @staticmethod
    def _decode_agent_task(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for source, target in (
            ("constraints_json", "constraints"),
            ("acceptance_criteria_json", "acceptance_criteria"),
            ("spec_json", "spec"),
            ("context_refs_json", "context_refs"),
            ("result_json", "result"),
        ):
            value = item.pop(source)
            item[target] = json.loads(value) if value is not None else None
        item["required"] = bool(item["required"])
        item.pop("idempotency_key", None)
        item.pop("owner_id", None)
        item.pop("cancel_requested_at", None)
        return item

    def list_agent_tasks(
        self,
        parent_session_id: str | None = None,
        *,
        workspace: Path | None = None,
        statuses: list[str] | None = None,
        unreported_required_only: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        arguments: list[Any] = []
        if parent_session_id is not None:
            clauses.append("parent_session_id = ?")
            arguments.append(parent_session_id)
        if workspace is not None:
            clauses.append("workspace = ?")
            arguments.append(str(workspace.resolve()))
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            arguments.extend(statuses)
        if unreported_required_only:
            clauses.extend(("required = 1", "reported_at IS NULL"))
        query = "SELECT * FROM agent_tasks"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, id LIMIT ?"
        arguments.append(limit)
        with self._lock:
            rows = self._connection.execute(query, tuple(arguments)).fetchall()
        return [self._decode_agent_task(row) for row in rows]

    def count_agent_tasks(
        self,
        parent_session_id: str | None = None,
        *,
        workspace: Path | None = None,
    ) -> int:
        terminal = (
            "completed",
            "blocked",
            "failed",
            "limit_reached",
            "cancelled",
            "interrupted",
        )
        placeholders = ",".join("?" for _ in terminal)
        query = f"SELECT COUNT(*) FROM agent_tasks WHERE status NOT IN ({placeholders})"
        arguments: list[Any] = list(terminal)
        if parent_session_id is not None:
            query += " AND parent_session_id = ?"
            arguments.append(parent_session_id)
        if workspace is not None:
            query += " AND workspace = ?"
            arguments.append(str(workspace.resolve()))
        with self._lock:
            return int(self._connection.execute(query, tuple(arguments)).fetchone()[0])

    def agent_task_usage(self, parent_session_id: str) -> dict[str, int | float]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT result_json FROM agent_tasks WHERE parent_session_id = ?",
                (parent_session_id,),
            ).fetchall()
        input_tokens = 0
        output_tokens = 0
        cost_usd = 0.0
        for row in rows:
            if not row["result_json"]:
                continue
            result = json.loads(row["result_json"])
            input_tokens += int(result.get("input_tokens") or 0)
            output_tokens += int(result.get("output_tokens") or 0)
            cost_usd += float(result.get("cost_usd") or 0)
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
        }

    def claim_agent_task(self, task_id: str, *, owner_id: str) -> bool:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE agent_tasks SET status = 'running', owner_id = ?, started_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (owner_id, now, task_id),
            )
        return cursor.rowcount == 1

    def set_agent_task_waiting_approval(self, task_id: str) -> bool:
        return self._transition_agent_task(
            task_id,
            from_statuses=("running",),
            status="waiting_approval",
        )

    def resume_agent_task_after_approval(self, task_id: str) -> bool:
        return self._transition_agent_task(
            task_id,
            from_statuses=("waiting_approval",),
            status="running",
        )

    def finish_agent_task(
        self,
        task_id: str,
        *,
        status: str,
        result: dict[str, Any] | None,
        error: str | None,
    ) -> bool:
        if status not in {"completed", "blocked", "failed", "limit_reached", "cancelled"}:
            raise ValueError(f"非法子 Agent 终态: {status}")
        from_statuses = ("running", "waiting_approval")
        if status == "cancelled":
            from_statuses = (*from_statuses, "cancelling", "queued")
        return self._transition_agent_task(
            task_id,
            from_statuses=from_statuses,
            status=status,
            result=result,
            error=error,
            completed=True,
        )

    def request_agent_task_cancel(
        self,
        task_id: str,
        *,
        parent_session_id: str,
        reason: str,
    ) -> str | None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connection:
            row = self._connection.execute(
                """
                SELECT status FROM agent_tasks WHERE id = ? AND parent_session_id = ?
                """,
                (task_id, parent_session_id),
            ).fetchone()
            if row is None:
                return None
            status = str(row["status"])
            if status in {
                "completed",
                "blocked",
                "failed",
                "limit_reached",
                "cancelled",
                "interrupted",
            }:
                return status
            if status == "queued":
                cursor = self._connection.execute(
                    """
                    UPDATE agent_tasks SET status = 'cancelled', cancel_requested_at = ?,
                        completed_at = ?, error = ? WHERE id = ? AND status = 'queued'
                    """,
                    (now, now, reason, task_id),
                )
                if cursor.rowcount == 1:
                    return "cancelled"
            else:
                cursor = self._connection.execute(
                    """
                    UPDATE agent_tasks
                    SET status = 'cancelling', cancel_requested_at = ?, error = ?
                    WHERE id = ? AND status IN ('running', 'waiting_approval')
                    """,
                    (now, reason, task_id),
                )
                if cursor.rowcount == 1:
                    return "cancelling"
            current = self._connection.execute(
                "SELECT status FROM agent_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            return str(current["status"]) if current else None

    def interrupt_recoverable_agent_tasks(self, *, workspace: Path) -> list[str]:
        """Fail closed on work that may already have performed side effects."""
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connection:
            rows = self._connection.execute(
                """
                SELECT id FROM agent_tasks
                WHERE status IN ('running', 'waiting_approval', 'cancelling')
                    AND workspace = ?
                """,
                (str(workspace.resolve()),),
            ).fetchall()
            self._connection.execute(
                """
                UPDATE agent_tasks SET status = 'interrupted', completed_at = ?,
                    error = COALESCE(
                        error,
                        '进程退出后检测到未完成任务；为避免重复副作用未自动重放'
                    )
                WHERE status IN ('running', 'waiting_approval', 'cancelling')
                    AND workspace = ?
                """,
                (now, str(workspace.resolve())),
            )
        return [str(row["id"]) for row in rows]

    def interrupt_owned_agent_tasks(
        self,
        owner_id: str,
        *,
        workspace: Path,
        include_queued: bool = False,
    ) -> int:
        statuses = ["running", "waiting_approval", "cancelling"]
        owner_clause = "owner_id = ?"
        arguments: list[Any] = [owner_id]
        if include_queued:
            statuses.append("queued")
            owner_clause = "(owner_id = ? OR owner_id IS NULL)"
        placeholders = ",".join("?" for _ in statuses)
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                f"""
                UPDATE agent_tasks SET status = 'interrupted', completed_at = ?,
                    error = COALESCE(error, 'Worker Pool 已关闭')
                WHERE {owner_clause} AND workspace = ? AND status IN ({placeholders})
                """,
                (now, *arguments, str(workspace.resolve()), *statuses),
            )
        return cursor.rowcount

    def _transition_agent_task(
        self,
        task_id: str,
        *,
        from_statuses: tuple[str, ...],
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        completed: bool = False,
    ) -> bool:
        placeholders = ",".join("?" for _ in from_statuses)
        completed_at = datetime.now(UTC).isoformat() if completed else None
        with self._lock, self._connection:
            cursor = self._connection.execute(
                f"""
                UPDATE agent_tasks SET status = ?, result_json = ?, error = ?,
                    completed_at = COALESCE(?, completed_at)
                WHERE id = ? AND status IN ({placeholders})
                """,
                (
                    status,
                    json.dumps(self._sanitizer(result), ensure_ascii=False)
                    if result is not None
                    else None,
                    error,
                    completed_at,
                    task_id,
                    *from_statuses,
                ),
            )
        return cursor.rowcount == 1

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

    def list_session_approvals(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT tool_call_id, decision, scope, reason, created_at
                FROM approvals WHERE session_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

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

    def list_memory_extraction_candidates(
        self,
        workspace: Path,
        *,
        limit: int,
        max_attempts: int,
        run_id: str | None = None,
        exclude_run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = [
            "r.status = 'completed'",
            "s.parent_session_id IS NULL",
            "s.workspace = ?",
            "(e.run_id IS NULL OR (e.status = 'failed' AND e.attempts < ?))",
        ]
        parameters: list[Any] = [str(workspace.resolve()), max_attempts]
        if run_id is not None:
            clauses.append("r.id = ?")
            parameters.append(run_id)
        if exclude_run_id is not None:
            clauses.append("r.id != ?")
            parameters.append(exclude_run_id)
        parameters.append(max(1, min(limit, 100)))
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT r.id AS run_id, r.session_id, r.completed_at, s.workspace,
                       COALESCE(e.attempts, 0) AS attempts
                FROM runs AS r
                JOIN sessions AS s ON s.id = r.session_id
                LEFT JOIN memory_extraction_runs AS e ON e.run_id = r.id
                WHERE {" AND ".join(clauses)}
                ORDER BY r.completed_at DESC, r.id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def load_run_positioned_messages(self, run_id: str) -> list[PositionedMessage]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT position, message_json
                FROM messages WHERE run_id = ? ORDER BY position
                """,
                (run_id,),
            ).fetchall()
        return [
            PositionedMessage(
                position=int(row["position"]),
                message=ChatMessage.model_validate_json(row["message_json"]),
            )
            for row in rows
        ]

    def load_messages_at_positions(
        self,
        session_id: str,
        positions: list[int],
    ) -> list[PositionedMessage]:
        selected = sorted({position for position in positions if position > 0})
        if not selected:
            return []
        placeholders = ",".join("?" for _ in selected)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT position, message_json
                FROM messages
                WHERE session_id = ? AND position IN ({placeholders})
                ORDER BY position
                """,
                (session_id, *selected),
            ).fetchall()
        return [
            PositionedMessage(
                position=int(row["position"]),
                message=ChatMessage.model_validate_json(row["message_json"]),
            )
            for row in rows
        ]

    def start_memory_extraction(
        self,
        *,
        run_id: str,
        session_id: str,
        workspace: Path,
        source_sha256: str,
        model: str,
    ) -> bool:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connection:
            existing = self._connection.execute(
                """
                SELECT status, source_sha256, attempts
                FROM memory_extraction_runs WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if existing is not None and existing["status"] == "building":
                return False
            if (
                existing is not None
                and existing["status"] == "ready"
                and existing["source_sha256"] == source_sha256
            ):
                return False
            attempts = int(existing["attempts"]) + 1 if existing is not None else 1
            self._connection.execute(
                """
                INSERT INTO memory_extraction_runs(
                    run_id, session_id, workspace, source_sha256, status, model,
                    attempts, started_at
                ) VALUES (?, ?, ?, ?, 'building', ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    workspace = excluded.workspace,
                    source_sha256 = excluded.source_sha256,
                    status = 'building',
                    model = excluded.model,
                    attempts = excluded.attempts,
                    candidate_count = 0,
                    added_count = 0,
                    merged_count = 0,
                    conflict_count = 0,
                    input_tokens = 0,
                    output_tokens = 0,
                    started_at = excluded.started_at,
                    completed_at = NULL,
                    error = NULL
                """,
                (
                    run_id,
                    session_id,
                    str(workspace.resolve()),
                    source_sha256,
                    model,
                    attempts,
                    now,
                ),
            )
        return True

    def complete_memory_extraction(
        self,
        run_id: str,
        *,
        candidate_count: int,
        added_count: int,
        merged_count: int,
        conflict_count: int,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE memory_extraction_runs
                SET status = 'ready', candidate_count = ?, added_count = ?,
                    merged_count = ?, conflict_count = ?, input_tokens = ?,
                    output_tokens = ?, completed_at = ?, error = NULL
                WHERE run_id = ? AND status = 'building'
                """,
                (
                    candidate_count,
                    added_count,
                    merged_count,
                    conflict_count,
                    input_tokens,
                    output_tokens,
                    datetime.now(UTC).isoformat(),
                    run_id,
                ),
            )

    def fail_memory_extraction(self, run_id: str, error: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE memory_extraction_runs
                SET status = 'failed', completed_at = ?, error = ?
                WHERE run_id = ? AND status = 'building'
                """,
                (
                    datetime.now(UTC).isoformat(),
                    str(self._sanitizer(error))[:4_000],
                    run_id,
                ),
            )

    def list_memory_extractions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM memory_extraction_runs
                ORDER BY started_at DESC LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

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
