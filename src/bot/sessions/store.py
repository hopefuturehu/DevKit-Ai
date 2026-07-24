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

SCHEMA_VERSION = 7


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
                CREATE TABLE IF NOT EXISTS memory_episodes (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    start_position INTEGER NOT NULL,
                    end_position INTEGER NOT NULL,
                    message_count INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    title TEXT,
                    summary TEXT,
                    keywords_json TEXT NOT NULL DEFAULT '[]',
                    consolidation_id TEXT,
                    created_at TEXT NOT NULL,
                    consolidated_at TEXT,
                    archived_at TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(id),
                    UNIQUE(session_id, run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_episodes_session_status_position
                    ON memory_episodes(session_id, status, start_position);
                CREATE TABLE IF NOT EXISTS memory_consolidation_runs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL,
                    model TEXT NOT NULL,
                    episode_ids_json TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_consolidation_session_status_started
                    ON memory_consolidation_runs(session_id, status, started_at);
                CREATE TABLE IF NOT EXISTS memory_cards (
                    id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    session_id TEXT,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    content TEXT NOT NULL,
                    normalized_key TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    source_positions_json TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_cards_lookup
                    ON memory_cards(workspace, session_id, scope, status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_memory_cards_normalized
                    ON memory_cards(workspace, scope, kind, normalized_key, status);
                CREATE TABLE IF NOT EXISTS memory_candidates (
                    id TEXT PRIMARY KEY,
                    consolidation_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    content TEXT NOT NULL,
                    target_memory_id TEXT,
                    source_positions_json TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    status TEXT NOT NULL,
                    rejection_reason TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(consolidation_id) REFERENCES memory_consolidation_runs(id)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_candidates_consolidation
                    ON memory_candidates(consolidation_id, status);
                CREATE TABLE IF NOT EXISTS memory_card_versions (
                    card_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    consolidation_id TEXT NOT NULL,
                    candidate_id TEXT,
                    operation TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(card_id, version),
                    FOREIGN KEY(card_id) REFERENCES memory_cards(id),
                    FOREIGN KEY(consolidation_id) REFERENCES memory_consolidation_runs(id)
                );
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
        blob_references = {
            reference
            for row in rows
            for reference in re.findall(r"blob:[0-9a-f]{64}", row["message_json"])
        }
        source_snapshot = self.latest_context_snapshot(session_id)
        if source_snapshot:
            _, snapshot = source_snapshot
            max_position = up_to_position
            if max_position is None:
                max_position = self.latest_message_position(session_id)
            if snapshot.cursor_position <= max_position:
                blob_references.update(re.findall(r"blob:[0-9a-f]{64}", snapshot.model_dump_json()))
                self.save_context_snapshot(
                    session_id=new_session_id,
                    run_id=f"fork:{session_id}",
                    snapshot=snapshot,
                )
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
                            'completed', 'failed', 'limit_reached', 'cancelled', 'interrupted'
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
                            'completed', 'failed', 'limit_reached', 'cancelled', 'interrupted'
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

    def seal_memory_episodes(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Seal completed run message ranges as immutable consolidation inputs."""
        query = """
            SELECT m.run_id, MIN(m.position) AS start_position,
                   MAX(m.position) AS end_position, COUNT(*) AS message_count
            FROM messages AS m
            LEFT JOIN runs AS r ON r.id = m.run_id
            WHERE m.session_id = ?
              AND (r.status IS NULL OR r.status <> 'running')
              AND NOT EXISTS (
                  SELECT 1 FROM memory_episodes AS e
                  WHERE e.session_id = m.session_id AND e.run_id = m.run_id
              )
        """
        arguments: list[Any] = [session_id]
        if run_id is not None:
            query += " AND m.run_id = ?"
            arguments.append(run_id)
        query += " GROUP BY m.run_id ORDER BY MIN(m.position)"
        now = datetime.now(UTC).isoformat()
        created: list[dict[str, Any]] = []
        with self._lock, self._connection:
            groups = self._connection.execute(query, tuple(arguments)).fetchall()
            for group in groups:
                message_rows = self._connection.execute(
                    """
                    SELECT message_json FROM messages
                    WHERE session_id = ? AND run_id = ? ORDER BY position
                    """,
                    (session_id, group["run_id"]),
                ).fetchall()
                digest = hashlib.sha256()
                for row in message_rows:
                    digest.update(str(row["message_json"]).encode("utf-8", errors="replace"))
                    digest.update(b"\n")
                episode_id = uuid4().hex
                self._connection.execute(
                    """
                    INSERT INTO memory_episodes(
                        id, session_id, run_id, start_position, end_position,
                        message_count, status, source_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        episode_id,
                        session_id,
                        str(group["run_id"]),
                        int(group["start_position"]),
                        int(group["end_position"]),
                        int(group["message_count"]),
                        digest.hexdigest(),
                        now,
                    ),
                )
                created.append(
                    {
                        "id": episode_id,
                        "session_id": session_id,
                        "run_id": str(group["run_id"]),
                        "start_position": int(group["start_position"]),
                        "end_position": int(group["end_position"]),
                        "message_count": int(group["message_count"]),
                        "status": "pending",
                        "source_sha256": digest.hexdigest(),
                        "created_at": now,
                    }
                )
        return created

    def list_memory_episodes(
        self,
        session_id: str,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT id, session_id, run_id, start_position, end_position,
                   message_count, status, source_sha256, title, summary,
                   keywords_json, consolidation_id, created_at, consolidated_at,
                   archived_at
            FROM memory_episodes WHERE session_id = ?
        """
        arguments: list[Any] = [session_id]
        if status is not None:
            query += " AND status = ?"
            arguments.append(status)
        query += " ORDER BY start_position LIMIT ?"
        arguments.append(max(1, limit))
        with self._lock:
            rows = self._connection.execute(query, tuple(arguments)).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["keywords"] = json.loads(item.pop("keywords_json"))
            result.append(item)
        return result

    def start_memory_consolidation(
        self,
        *,
        session_id: str,
        trigger: str,
        model: str,
        episode_ids: list[str],
    ) -> str:
        if not episode_ids:
            raise ValueError("记忆整合至少需要一个 Episode")
        consolidation_id = uuid4().hex
        placeholders = ",".join("?" for _ in episode_ids)
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connection:
            count = self._connection.execute(
                f"""
                SELECT COUNT(*) FROM memory_episodes
                WHERE session_id = ? AND status = 'pending'
                  AND id IN ({placeholders})
                """,
                (session_id, *episode_ids),
            ).fetchone()[0]
            if int(count) != len(set(episode_ids)):
                raise ValueError("记忆整合 Episode 不存在、重复或已被处理")
            self._connection.execute(
                """
                INSERT INTO memory_consolidation_runs(
                    id, session_id, trigger, status, model, episode_ids_json, started_at
                ) VALUES (?, ?, ?, 'building', ?, ?, ?)
                """,
                (
                    consolidation_id,
                    session_id,
                    trigger,
                    model,
                    json.dumps(episode_ids, ensure_ascii=False),
                    now,
                ),
            )
        return consolidation_id

    def fail_memory_consolidation(self, consolidation_id: str, error: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE memory_consolidation_runs
                SET status = 'failed', completed_at = ?, error = ?
                WHERE id = ? AND status = 'building'
                """,
                (datetime.now(UTC).isoformat(), str(self._sanitizer(error)), consolidation_id),
            )

    def latest_memory_consolidation(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT id, trigger, status, model, episode_ids_json, input_tokens,
                       output_tokens, started_at, completed_at, error
                FROM memory_consolidation_runs
                WHERE session_id = ?
                ORDER BY started_at DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["episode_ids"] = json.loads(item.pop("episode_ids_json"))
        return item

    def list_memory_consolidations(
        self,
        session_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, trigger, status, model, episode_ids_json, input_tokens,
                       output_tokens, started_at, completed_at, error
                FROM memory_consolidation_runs
                WHERE session_id = ?
                ORDER BY started_at DESC LIMIT ?
                """,
                (session_id, max(1, limit)),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["episode_ids"] = json.loads(item.pop("episode_ids_json"))
            result.append(item)
        return result

    def memory_message_metadata(
        self,
        session_id: str,
        positions: set[int],
    ) -> dict[int, dict[str, Any]]:
        if not positions:
            return {}
        ordered = sorted(positions)
        placeholders = ",".join("?" for _ in ordered)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT position, role, run_id, message_json
                FROM messages
                WHERE session_id = ? AND position IN ({placeholders})
                """,
                (session_id, *ordered),
            ).fetchall()
        return {
            int(row["position"]): {
                "position": int(row["position"]),
                "role": str(row["role"]),
                "run_id": str(row["run_id"]),
                "message": ChatMessage.model_validate_json(row["message_json"]),
            }
            for row in rows
        }

    def list_tool_runs(
        self,
        session_id: str,
        *,
        run_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT run_id, tool_call_id, tool_name, arguments_json, status,
                   result_json, created_at
            FROM tool_runs WHERE session_id = ?
        """
        arguments: list[Any] = [session_id]
        if run_ids:
            ordered = sorted(run_ids)
            placeholders = ",".join("?" for _ in ordered)
            query += f" AND run_id IN ({placeholders})"
            arguments.extend(ordered)
        query += " ORDER BY id"
        with self._lock:
            rows = self._connection.execute(query, tuple(arguments)).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["arguments"] = json.loads(item.pop("arguments_json"))
            raw_result = item.pop("result_json")
            item["result"] = json.loads(raw_result) if raw_result else None
            result.append(item)
        return result

    def has_context_blob_access(self, session_id: str, reference: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT 1 FROM context_blob_access
                WHERE session_id = ? AND blob_id = ?
                """,
                (session_id, reference),
            ).fetchone()
        return row is not None

    def list_active_memory_cards(
        self,
        *,
        workspace: Path,
        session_id: str,
        limit: int = 1_000,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, scope, workspace, session_id, kind, status, content,
                       confidence, source_positions_json, evidence_refs_json,
                       version, created_at, updated_at
                FROM memory_cards
                WHERE workspace = ? AND status = 'active'
                  AND (
                      (scope = 'workspace' AND session_id IS NULL)
                      OR (scope = 'session' AND session_id = ?)
                  )
                ORDER BY updated_at DESC LIMIT ?
                """,
                (str(workspace.resolve()), session_id, max(1, limit)),
            ).fetchall()
        return [self._decode_memory_card(row) for row in rows]

    def get_memory_card(self, memory_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT id, scope, workspace, session_id, kind, status, content,
                       confidence, source_positions_json, evidence_refs_json,
                       version, created_at, updated_at
                FROM memory_cards WHERE id = ?
                """,
                (memory_id,),
            ).fetchone()
        return self._decode_memory_card(row) if row else None

    def list_memory_card_versions(self, memory_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT card_id, version, consolidation_id, candidate_id,
                       operation, payload_json, created_at
                FROM memory_card_versions
                WHERE card_id = ? ORDER BY version
                """,
                (memory_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def complete_memory_consolidation(
        self,
        *,
        consolidation_id: str,
        session_id: str,
        episode_summaries: list[dict[str, Any]],
        verified_candidates: list[dict[str, Any]],
        input_tokens: int,
        output_tokens: int,
        stale_after_days: int,
        max_active_cards: int,
    ) -> dict[str, int]:
        """Atomically publish summaries, candidate decisions, cards and run state."""
        now = datetime.now(UTC)
        now_text = now.isoformat()
        sanitized_summaries = self._sanitizer(episode_summaries)
        sanitized_candidates = self._sanitizer(verified_candidates)
        counters = {
            "episodes_consolidated": 0,
            "candidates_accepted": 0,
            "candidates_rejected": 0,
            "cards_created": 0,
            "cards_updated": 0,
            "cards_staled": 0,
        }
        with self._lock, self._connection:
            consolidation = self._connection.execute(
                """
                SELECT episode_ids_json, status
                FROM memory_consolidation_runs
                WHERE id = ? AND session_id = ?
                """,
                (consolidation_id, session_id),
            ).fetchone()
            if consolidation is None or consolidation["status"] != "building":
                raise ValueError("记忆整合运行不存在或不再处于 building 状态")
            expected_episode_ids = set(json.loads(consolidation["episode_ids_json"]))
            actual_episode_ids = {str(item["episode_id"]) for item in sanitized_summaries}
            if actual_episode_ids != expected_episode_ids:
                raise ValueError("LLM 返回的 Episode 集合与整合输入不一致")
            session = self._connection.execute(
                "SELECT workspace FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise ValueError(f"会话不存在: {session_id}")
            workspace = str(Path(session["workspace"]).resolve())

            for summary in sanitized_summaries:
                cursor = self._connection.execute(
                    """
                    UPDATE memory_episodes
                    SET title = ?, summary = ?, keywords_json = ?,
                        status = 'consolidated', consolidation_id = ?,
                        consolidated_at = ?
                    WHERE id = ? AND session_id = ? AND status = 'pending'
                    """,
                    (
                        str(summary["title"]),
                        str(summary["summary"]),
                        json.dumps(summary.get("keywords") or [], ensure_ascii=False),
                        consolidation_id,
                        now_text,
                        str(summary["episode_id"]),
                        session_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"Episode 无法提交: {summary['episode_id']}")
                counters["episodes_consolidated"] += 1

            for verified in sanitized_candidates:
                candidate = dict(verified["candidate"])
                accepted = bool(verified["accepted"])
                candidate_id = uuid4().hex
                self._connection.execute(
                    """
                    INSERT INTO memory_candidates(
                        id, consolidation_id, operation, kind, scope, content,
                        target_memory_id, source_positions_json, evidence_refs_json,
                        confidence, status, rejection_reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate_id,
                        consolidation_id,
                        str(candidate["operation"]),
                        str(candidate["kind"]),
                        str(candidate["scope"]),
                        str(candidate["content"]),
                        candidate.get("target_memory_id"),
                        json.dumps(candidate["source_positions"], ensure_ascii=False),
                        json.dumps(candidate.get("evidence_refs") or [], ensure_ascii=False),
                        float(candidate["confidence"]),
                        "accepted" if accepted else "rejected",
                        verified.get("rejection_reason"),
                        now_text,
                    ),
                )
                if not accepted:
                    counters["candidates_rejected"] += 1
                    continue
                counters["candidates_accepted"] += 1
                created = self._apply_memory_candidate_locked(
                    consolidation_id=consolidation_id,
                    candidate_id=candidate_id,
                    session_id=session_id,
                    workspace=workspace,
                    candidate=candidate,
                    now=now_text,
                )
                counters["cards_created" if created else "cards_updated"] += 1

            counters["cards_staled"] = self._prune_memory_cards_locked(
                consolidation_id=consolidation_id,
                workspace=workspace,
                cutoff=(now - timedelta(days=stale_after_days)).isoformat(),
                max_active_cards=max_active_cards,
                now=now_text,
            )
            self._connection.execute(
                """
                UPDATE memory_consolidation_runs
                SET status = 'ready', completed_at = ?, input_tokens = ?,
                    output_tokens = ?, error = NULL
                WHERE id = ? AND status = 'building'
                """,
                (now_text, max(0, input_tokens), max(0, output_tokens), consolidation_id),
            )
        return counters

    def memory_status(self, session_id: str, *, workspace: Path) -> dict[str, Any]:
        pending = self.list_memory_episodes(session_id, status="pending", limit=10_000)
        cards = self.list_active_memory_cards(
            workspace=workspace,
            session_id=session_id,
            limit=10_000,
        )
        return {
            "pending_episodes": len(pending),
            "pending_messages": sum(int(item["message_count"]) for item in pending),
            "active_cards": len(cards),
            "latest_consolidation": self.latest_memory_consolidation(session_id),
        }

    @staticmethod
    def _decode_memory_card(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["source_positions"] = json.loads(item.pop("source_positions_json"))
        item["evidence_refs"] = json.loads(item.pop("evidence_refs_json"))
        return item

    @staticmethod
    def _memory_normalized_key(kind: str, content: str) -> str:
        normalized = " ".join(content.casefold().split())
        return hashlib.sha256(f"{kind}\0{normalized}".encode()).hexdigest()

    def _apply_memory_candidate_locked(
        self,
        *,
        consolidation_id: str,
        candidate_id: str,
        session_id: str,
        workspace: str,
        candidate: dict[str, Any],
        now: str,
    ) -> bool:
        operation = str(candidate["operation"])
        scope = str(candidate["scope"])
        kind = str(candidate["kind"])
        target_memory_id = candidate.get("target_memory_id")
        card_session_id = session_id if scope == "session" else None
        row = None
        if target_memory_id:
            row = self._connection.execute(
                """
                SELECT * FROM memory_cards
                WHERE id = ? AND workspace = ? AND scope = ?
                  AND (
                      (? = 'workspace' AND session_id IS NULL)
                      OR (? = 'session' AND session_id = ?)
                  )
                """,
                (
                    target_memory_id,
                    workspace,
                    scope,
                    scope,
                    scope,
                    session_id,
                ),
            ).fetchone()
            if row is None:
                raise ValueError(f"目标 Memory Card 不存在或越权: {target_memory_id}")
        elif operation == "upsert":
            normalized_key = self._memory_normalized_key(kind, str(candidate["content"]))
            row = self._connection.execute(
                """
                SELECT * FROM memory_cards
                WHERE workspace = ? AND scope = ? AND kind = ?
                  AND normalized_key = ? AND status = 'active'
                  AND (
                      (? = 'workspace' AND session_id IS NULL)
                      OR (? = 'session' AND session_id = ?)
                  )
                ORDER BY updated_at DESC LIMIT 1
                """,
                (
                    workspace,
                    scope,
                    kind,
                    normalized_key,
                    scope,
                    scope,
                    session_id,
                ),
            ).fetchone()

        if operation == "upsert":
            source_positions = sorted(set(int(item) for item in candidate["source_positions"]))
            evidence_refs = sorted(set(str(item) for item in candidate.get("evidence_refs") or []))
            if row is None:
                card_id = uuid4().hex
                payload = {
                    "id": card_id,
                    "scope": scope,
                    "workspace": workspace,
                    "session_id": card_session_id,
                    "kind": kind,
                    "status": "active",
                    "content": str(candidate["content"]),
                    "confidence": float(candidate["confidence"]),
                    "source_positions": source_positions,
                    "evidence_refs": evidence_refs,
                    "version": 1,
                }
                self._connection.execute(
                    """
                    INSERT INTO memory_cards(
                        id, scope, workspace, session_id, kind, status, content,
                        normalized_key, confidence, source_positions_json,
                        evidence_refs_json, version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        card_id,
                        scope,
                        workspace,
                        card_session_id,
                        kind,
                        payload["content"],
                        self._memory_normalized_key(kind, payload["content"]),
                        payload["confidence"],
                        json.dumps(source_positions),
                        json.dumps(evidence_refs, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                self._insert_memory_card_version_locked(
                    payload=payload,
                    consolidation_id=consolidation_id,
                    candidate_id=candidate_id,
                    operation=operation,
                    now=now,
                )
                return True

            existing_sources = json.loads(row["source_positions_json"])
            existing_refs = json.loads(row["evidence_refs_json"])
            source_positions = sorted(set(existing_sources) | set(source_positions))
            evidence_refs = sorted(set(existing_refs) | set(evidence_refs))
            version = int(row["version"]) + 1
            payload = {
                "id": str(row["id"]),
                "scope": scope,
                "workspace": workspace,
                "session_id": row["session_id"],
                "kind": kind,
                "status": "active",
                "content": str(candidate["content"]),
                "confidence": float(candidate["confidence"]),
                "source_positions": source_positions,
                "evidence_refs": evidence_refs,
                "version": version,
            }
            self._connection.execute(
                """
                UPDATE memory_cards
                SET status = 'active', content = ?, normalized_key = ?,
                    confidence = ?, source_positions_json = ?,
                    evidence_refs_json = ?, version = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    payload["content"],
                    self._memory_normalized_key(kind, payload["content"]),
                    payload["confidence"],
                    json.dumps(source_positions),
                    json.dumps(evidence_refs, ensure_ascii=False),
                    version,
                    now,
                    row["id"],
                ),
            )
            self._insert_memory_card_version_locked(
                payload=payload,
                consolidation_id=consolidation_id,
                candidate_id=candidate_id,
                operation=operation,
                now=now,
            )
            return False

        if row is None:
            raise ValueError(f"{operation} 操作缺少目标 Memory Card")
        status = "resolved" if operation == "resolve" else "retracted"
        sources = sorted(
            set(json.loads(row["source_positions_json"]))
            | set(int(item) for item in candidate["source_positions"])
        )
        refs = sorted(
            set(json.loads(row["evidence_refs_json"]))
            | set(str(item) for item in candidate.get("evidence_refs") or [])
        )
        version = int(row["version"]) + 1
        payload = {
            "id": str(row["id"]),
            "scope": str(row["scope"]),
            "workspace": str(row["workspace"]),
            "session_id": row["session_id"],
            "kind": str(row["kind"]),
            "status": status,
            "content": str(row["content"]),
            "confidence": float(row["confidence"]),
            "source_positions": sources,
            "evidence_refs": refs,
            "version": version,
            "transition_reason": str(candidate["content"]),
        }
        self._connection.execute(
            """
            UPDATE memory_cards
            SET status = ?, source_positions_json = ?, evidence_refs_json = ?,
                version = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                json.dumps(sources),
                json.dumps(refs, ensure_ascii=False),
                version,
                now,
                row["id"],
            ),
        )
        self._insert_memory_card_version_locked(
            payload=payload,
            consolidation_id=consolidation_id,
            candidate_id=candidate_id,
            operation=operation,
            now=now,
        )
        return False

    def _insert_memory_card_version_locked(
        self,
        *,
        payload: dict[str, Any],
        consolidation_id: str,
        candidate_id: str | None,
        operation: str,
        now: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO memory_card_versions(
                card_id, version, consolidation_id, candidate_id,
                operation, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["id"],
                payload["version"],
                consolidation_id,
                candidate_id,
                operation,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )

    def _prune_memory_cards_locked(
        self,
        *,
        consolidation_id: str,
        workspace: str,
        cutoff: str,
        max_active_cards: int,
        now: str,
    ) -> int:
        transient_kinds = ("project", "task", "error", "artifact", "verification")
        placeholders = ",".join("?" for _ in transient_kinds)
        rows = list(
            self._connection.execute(
                f"""
                SELECT * FROM memory_cards
                WHERE workspace = ? AND status = 'active' AND updated_at < ?
                  AND kind IN ({placeholders})
                ORDER BY updated_at
                """,
                (workspace, cutoff, *transient_kinds),
            ).fetchall()
        )
        active_count = int(
            self._connection.execute(
                """
                SELECT COUNT(*) FROM memory_cards
                WHERE workspace = ? AND status = 'active'
                """,
                (workspace,),
            ).fetchone()[0]
        )
        overflow = max(0, active_count - max_active_cards)
        if overflow and len(rows) < overflow:
            already = {str(row["id"]) for row in rows}
            extra = self._connection.execute(
                f"""
                SELECT * FROM memory_cards
                WHERE workspace = ? AND status = 'active'
                  AND kind IN ({placeholders})
                ORDER BY confidence ASC, updated_at ASC
                """,
                (workspace, *transient_kinds),
            ).fetchall()
            for row in extra:
                if str(row["id"]) in already:
                    continue
                rows.append(row)
                already.add(str(row["id"]))
                if len(rows) >= overflow:
                    break
        for row in rows:
            version = int(row["version"]) + 1
            payload = {
                "id": str(row["id"]),
                "scope": str(row["scope"]),
                "workspace": str(row["workspace"]),
                "session_id": row["session_id"],
                "kind": str(row["kind"]),
                "status": "stale",
                "content": str(row["content"]),
                "confidence": float(row["confidence"]),
                "source_positions": json.loads(row["source_positions_json"]),
                "evidence_refs": json.loads(row["evidence_refs_json"]),
                "version": version,
            }
            self._connection.execute(
                """
                UPDATE memory_cards
                SET status = 'stale', version = ?, updated_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (version, now, row["id"]),
            )
            self._insert_memory_card_version_locked(
                payload=payload,
                consolidation_id=consolidation_id,
                candidate_id=None,
                operation="stale",
                now=now,
            )
        return len(rows)

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
        terminal = ("completed", "failed", "limit_reached", "cancelled", "interrupted")
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
        if status not in {"completed", "failed", "limit_reached", "cancelled"}:
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
            if status in {"completed", "failed", "limit_reached", "cancelled", "interrupted"}:
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
