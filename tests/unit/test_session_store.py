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
    assert 7 in versions
