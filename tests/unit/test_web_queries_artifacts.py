import asyncio
from pathlib import Path

import pytest

from bot.core.events import AgentEvent, EventType
from bot.observability import Redactor
from bot.sessions import SQLiteSessionStore
from bot.web.artifacts import ArtifactStore
from bot.web.queries import CursorExpired, WebQueries
from bot.web.sink import WebSocketEventSink


def publish(store, kind, *, session="s1", run="r1", sequence=1, **payload):
    event = AgentEvent(
        type=kind, session_id=session, run_id=run, sequence=sequence, payload=payload
    )
    asyncio.run(store.publish(event))
    return event


@pytest.fixture
def records(tmp_path):
    store = SQLiteSessionStore(tmp_path / ".bot/state.db")
    for session in ("s1", "s2"):
        store.create_session(tmp_path, session_id=session)
    store.start_run("s1", "r1")
    store.start_run("s1", "r2")
    store.start_run("s2", "r3")
    yield store, WebQueries(store, tmp_path)
    store.close()


def test_cursor_uses_persistent_order_not_eventbus_sequence(records):
    store, queries = records
    first = publish(store, EventType.RUN_STARTED, sequence=98, prompt="first")
    second = publish(
        store, EventType.TOOL_REQUESTED, sequence=1, tool_call_id="same", name="read_file"
    )
    publish(store, EventType.RUN_STARTED, session="s2", run="r3", sequence=99, prompt="other")
    page = queries.events("s1", run_id="r1", limit=1)
    assert [e["id"] for e in page["events"]] == [first.id]
    publish(store, EventType.RUN_COMPLETED, sequence=2)
    next_page = queries.events("s1", run_id="r1", cursor=page["cursor"], through=page["through"])
    assert [e["id"] for e in next_page["events"]] == [second.id]
    assert not next_page["has_more"]
    live = queries.events("s1", run_id="r1", cursor=next_page["cursor"])
    assert [e["type"] for e in live["events"]] == ["run.completed"]
    with pytest.raises(CursorExpired):
        WebQueries(store, queries.workspace).events("s1", cursor=live["cursor"])
    with pytest.raises(LookupError):
        queries.events("s2", run_id="r1")


def test_tool_and_unicode_blob_are_scoped_and_paged(records):
    store, queries = records
    reference = store.put_context_blob(session_id="s1", run_id="r1", content="中文输出αβ")
    publish(
        store,
        EventType.TOOL_REQUESTED,
        tool_call_id="same",
        name="read_file",
        arguments={"path": "a"},
    )
    publish(
        store,
        EventType.TOOL_COMPLETED,
        tool_call_id="same",
        name="read_file",
        success=True,
        status="completed",
        context_ref=reference,
        output_excerpt="摘要",
        truncated=True,
    )
    publish(
        store,
        EventType.TOOL_REQUESTED,
        run="r2",
        tool_call_id="same",
        name="read_file",
        arguments={"path": "b"},
    )
    tool = queries.tool("r1", "same", limit=2)
    assert tool["arguments"] == {"path": "a"}
    assert tool["source_truncated"] and tool["full_output_available"]
    assert tool["output"]["content"] == "中文"
    assert queries.blob("r1", reference, offset=2, limit=2)["content"] == "输出"
    assert queries.tool("r2", "same")["arguments"] == {"path": "b"}
    with pytest.raises(LookupError):
        queries.blob("r2", reference)
    with pytest.raises(LookupError):
        queries.blob("r3", reference)
    for index in range(4):
        publish(store, EventType.TOOL_OUTPUT, tool_call_id="same", data=str(index))
    page = queries.events("s1", run_id="r1", tool_call_id="same", limit=2, children=False)
    seen = []
    while True:
        seen.extend(page["events"])
        if not page["has_more"]:
            break
        page = queries.events(
            "s1",
            run_id="r1",
            tool_call_id="same",
            limit=2,
            children=False,
            cursor=page["cursor"],
            through=page["through"],
        )
    assert len(seen) == 6
    assert all(event["run_id"] == "r1" for event in seen)


def test_workspace_scope_and_cumulative_usage(records, tmp_path):
    store, queries = records
    publish(store, EventType.MODEL_USAGE, input_tokens=10, output_tokens=1)
    publish(store, EventType.MODEL_USAGE, input_tokens=25, output_tokens=4, cost_usd=None)
    assert queries.run("r1")["input_tokens"] == 25
    assert queries.run("r1")["cost_usd"] is None
    store.create_session(tmp_path / "other", session_id="foreign")
    store.start_run("foreign", "foreign-run")
    with pytest.raises(LookupError):
        queries.run("foreign-run")
    assert {s["id"] for s in queries.sessions()} == {"s1", "s2"}


def test_artifacts_capture_real_changes_redact_and_reject_symlinks(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("before\n")
    (workspace / "deleted.txt").write_text("deleted\n")
    (workspace / ".env").write_text("SECRET=secret-token")
    outside = tmp_path / "outside.txt"
    outside.write_text("private outside")
    (workspace / "link.txt").symlink_to(outside)
    artifacts = ArtifactStore(tmp_path / "archive", Redactor(["secret-token"]))
    artifacts.capture("run", workspace, initial=True)
    (workspace / "note.txt").write_text("after secret-token\n")
    (workspace / "deleted.txt").unlink()
    (workspace / "new.txt").write_text("new\n")
    artifacts.capture("run", workspace)
    listing = artifacts.listing("run")
    assert {a["path"]: a["change"] for a in listing["artifacts"]} == {
        "note.txt": "modified",
        "deleted.txt": "deleted",
        "new.txt": "added",
    }
    changed = next(a for a in listing["artifacts"] if a["path"] == "note.txt")
    detail = artifacts.detail("run", changed["id"])
    assert "-before" in detail["diff"] and "+after" in detail["diff"]
    assert "secret-token" not in detail["diff"]
    assert changed["after"]["redacted"]
    _, content = artifacts.content("run", changed["id"])
    assert b"secret-token" not in content
    assert not artifacts.listing("old-run")["available"]
    with pytest.raises(LookupError):
        artifacts.content("different-run", changed["id"])
    with pytest.raises((OSError, ValueError)):
        artifacts._read(workspace, Path("link.txt"))


def test_live_queues_are_scoped_and_overflow_requires_replay():
    async def scenario():
        sink = WebSocketEventSink(max_queue=1)
        queue = sink.subscribe(lambda event: event.session_id == "s1")
        other = AgentEvent(type=EventType.RUN_STARTED, session_id="s2", run_id="r2")
        await sink.publish(other)
        assert queue.empty()
        one = AgentEvent(type=EventType.RUN_STARTED, session_id="s1", run_id="r1")
        await sink.publish(one)
        await sink.publish(one)
        assert "resync_required" in queue.get_nowait()
        await sink.publish(one)
        assert queue.empty()
        sink.configure(queue, lambda event: True)
        await sink.publish(one)
        assert "schema_version" in queue.get_nowait()
        sink.unsubscribe(queue)

    asyncio.run(scenario())


def test_incomplete_snapshot_never_invents_deleted_files(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in ("a", "b"):
        (workspace / name).write_text(name)
    artifacts = ArtifactStore(tmp_path / "archive", Redactor([]))
    artifacts.capture("run", workspace, initial=True)
    monkeypatch.setattr("bot.web.artifacts.MAX_FILES", 1)
    artifacts.capture("run", workspace)
    assert artifacts.listing("run")["artifacts"] == []
    assert artifacts.listing("run")["warnings"]
    artifacts.failed("run")
    assert "最新文件快照读取失败" in artifacts.listing("run")["warnings"][-1]
