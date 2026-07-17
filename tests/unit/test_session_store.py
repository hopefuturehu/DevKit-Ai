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
    store.finish_run(run_id, "completed")

    assert store.latest_session(tmp_path) == session_id
    assert store.load_messages(session_id)[0].content == "hello"
    store.close()
