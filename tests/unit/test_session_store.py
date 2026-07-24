import sqlite3
from pathlib import Path

import pytest

from bot.core.events import AgentEvent, EventType
from bot.core.models import ChatMessage, Role
from bot.sessions import SQLiteSessionStore


@pytest.mark.asyncio
async def test_session_store_persists_events_and_messages(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "state.db")
    session_id = store.create_session(tmp_path)
    run_id = "run-1"
    store.start_run(session_id, run_id)
    await store.publish(
        AgentEvent(
            type=EventType.RUN_STARTED,
            session_id=session_id,
            run_id=run_id,
            sequence=1,
        )
    )
    store.append_message(session_id, run_id, ChatMessage(role=Role.USER, content="hello"))
    with pytest.raises(ValueError, match="拒绝持久化无效消息"):
        store.append_message(session_id, run_id, ChatMessage(role=Role.ASSISTANT))
    store.finish_run(
        run_id,
        "completed",
        input_tokens=10,
        output_tokens=4,
        cost_usd=0.002,
    )

    assert store.latest_session(tmp_path) == session_id
    assert store.load_messages(session_id)[0].content == "hello"
    assert store.list_events(session_id)[0]["schema_version"] == 1
    assert store.session_usage(session_id) == {
        "runs": 1,
        "input_tokens": 10,
        "output_tokens": 4,
        "cost_usd": 0.002,
    }
    memory_id = store.add_memory("use pnpm")
    assert store.list_memories()[0]["id"] == memory_id
    assert store.delete_memory(memory_id)
    assert store.list_memories() == []

    fingerprint = "abc"
    store.save_approval_rule(
        tool_name="run_command",
        action_fingerprint=fingerprint,
        arguments={"argv": ["make", "test"]},
    )
    assert store.has_approval_rule(fingerprint)

    forked = store.fork_session(session_id)
    assert store.get_session(forked)["parent_session_id"] == session_id
    assert store.load_messages(forked)[0].content == "hello"
    store.close()

    reopened = SQLiteSessionStore(tmp_path / "state.db")
    assert reopened.load_messages(session_id)[0].content == "hello"
    reopened.close()


def test_session_store_migrates_legacy_event_schema(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE events (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            type TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    connection.commit()
    connection.close()

    store = SQLiteSessionStore(path)
    store.close()

    connection = sqlite3.connect(path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
    versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    connection.close()
    assert "schema_version" in columns
    assert 8 in versions


def test_memory_episode_segments_are_claimed_atomically_and_follow_forks(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "state.db")
    session_id = store.create_session(tmp_path)
    store.start_run(session_id, "run-1")
    for index in range(4):
        store.append_message(
            session_id,
            "run-1",
            ChatMessage(
                role=Role.USER if index % 2 == 0 else Role.ASSISTANT,
                content=f"message-{index}",
            ),
        )

    first = store.seal_memory_episode_range(
        session_id=session_id,
        run_id="run-1",
        end_position=2,
    )
    store.finish_run("run-1", "completed")
    remainder = store.seal_memory_episodes(session_id)

    assert first is not None
    assert [(item["start_position"], item["end_position"]) for item in remainder] == [(3, 4)]
    episodes = store.list_memory_episodes(session_id)
    assert [(item["start_position"], item["end_position"]) for item in episodes] == [
        (1, 2),
        (3, 4),
    ]

    claimed = store.start_memory_consolidation(
        session_id=session_id,
        trigger="test",
        model="mock",
        episode_ids=[first["id"]],
    )
    with pytest.raises(ValueError, match="不存在、重复或已被处理"):
        store.start_memory_consolidation(
            session_id=session_id,
            trigger="test-race",
            model="mock",
            episode_ids=[first["id"]],
        )
    store.fail_memory_consolidation(claimed, "retry")
    assert store.list_memory_episodes(session_id)[0]["status"] == "pending"

    consolidation_id = store.start_memory_consolidation(
        session_id=session_id,
        trigger="test",
        model="mock",
        episode_ids=[item["id"] for item in episodes],
    )
    store.complete_memory_consolidation(
        consolidation_id=consolidation_id,
        session_id=session_id,
        episode_summaries=[
            {
                "episode_id": item["id"],
                "title": f"segment-{index}",
                "objective": "test",
                "summary": f"summary-{index}",
                "keywords": ["segment"],
                "topics": ["test"],
                "depth": "deep",
                "token_estimate": 10,
            }
            for index, item in enumerate(episodes)
        ],
        verified_candidates=[],
        input_tokens=10,
        output_tokens=5,
        source_chars=100,
        summary_chars=20,
        duration_ms=1,
        stale_after_days=90,
        max_active_cards=500,
    )

    forked = store.fork_session(session_id)

    assert store.consolidated_memory_cursor(session_id) == 4
    assert store.consolidated_memory_cursor(forked) == 4
    assert len(store.list_consolidated_episode_summaries(forked)) == 2
    assert store.latest_context_snapshot(forked) is None
    store.close()


def test_schema_v8_rebuilds_v7_episode_table_without_losing_data(tmp_path: Path) -> None:
    path = tmp_path / "v7.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            workspace TEXT NOT NULL,
            parent_session_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE memory_episodes (
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
            UNIQUE(session_id, run_id)
        );
        INSERT INTO sessions VALUES (
            'session', '/tmp/workspace', NULL,
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
        );
        INSERT INTO memory_episodes VALUES (
            'episode', 'session', 'run', 1, 2, 2, 'consolidated',
            'abc', 'title', 'summary', '["keyword"]', 'consolidation',
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:01:00+00:00', NULL
        );
        """
    )
    connection.commit()
    connection.close()

    store = SQLiteSessionStore(path)
    episodes = store.list_memory_episodes("session")

    assert episodes[0]["id"] == "episode"
    assert episodes[0]["summary"] == "summary"
    assert episodes[0]["source_kind"] == "run"
    assert episodes[0]["topics"] == []
    with store._lock:  # noqa: SLF001
        sql = store._connection.execute(  # noqa: SLF001
            "SELECT sql FROM sqlite_master WHERE name = 'memory_episodes'"
        ).fetchone()[0]
    assert "UNIQUE(session_id, start_position, end_position)" in sql
    store.close()
