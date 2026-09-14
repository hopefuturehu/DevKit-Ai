import asyncio

import pytest

from bot.config.models import RecoveryConfig
from bot.core.context import synthetic_user_context_message
from bot.core.events import EventBus, EventType
from bot.core.models import ModelRequest
from bot.core.termination.recovery import RecoveryController
from bot.providers.base import ProviderStream
from bot.sessions import SQLiteSessionStore


@pytest.mark.asyncio
async def test_recovery_commit_is_atomic_and_survives_restart(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    store = SQLiteSessionStore(path)
    session = store.create_session(tmp_path)
    store.start_run(session, "run")
    state = store.begin_recovery_task(session, "scope", new_task=False, wall_seconds=50)
    controller = RecoveryController(RecoveryConfig(enabled=True), state)
    message = synthetic_user_context_message(
        name="runtime_context",
        kind="model_response_recovery",
        content="recover",
        source="runtime",
        scope="task",
    )
    bus = EventBus([store])
    original = store._insert_event

    def fail(event):
        raise RuntimeError("disk write failed")

    monkeypatch.setattr(store, "_insert_event", fail)

    async def commit():
        await bus.emit(
            EventType.RUN_RECOVERY_STARTED,
            session_id=session,
            run_id="run",
            before_publish=lambda event: store.commit_recovery(
                session, "scope", controller.next_attempt(), message, event
            ),
        )

    with pytest.raises(RuntimeError, match="disk write"):
        await commit()
    assert store.load_messages(session) == []
    assert (
        store.begin_recovery_task(session, "scope", new_task=False, wall_seconds=500)[
            "task_attempts"
        ]
        == 0
    )
    monkeypatch.setattr(store, "_insert_event", original)
    await commit()
    store.close()
    restored = SQLiteSessionStore(path)
    try:
        saved = restored.begin_recovery_task(session, "scope", new_task=False, wall_seconds=500)
        assert saved["task_attempts"] == 1 and saved["task_deadline"] == state["task_deadline"]
        assert len(restored.load_messages(session)) == 1
        assert sum(e["type"] == "run.recovery_started" for e in restored.list_events(session)) == 1
        new = restored.begin_recovery_task(session, "scope", new_task=True, wall_seconds=500)
        assert new["task_generation"] == 2 and new["task_attempts"] == 0
    finally:
        restored.close()


@pytest.mark.asyncio
async def test_provider_close_has_a_deadline():
    released = asyncio.Event()

    class Iterator:
        async def aclose(self):
            try:
                await released.wait()
            except asyncio.CancelledError:
                await released.wait()

    class Provider:
        def stream(self, request):
            return Iterator()

    stream = ProviderStream(Provider(), ModelRequest(model="mock", messages=[]), timeout=0.01)
    async with asyncio.timeout(0.2):
        async with stream:
            pass
    assert stream.close_status == "timeout"
    released.set()
    await asyncio.sleep(0)
