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
