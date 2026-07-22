import json
import sqlite3
from pathlib import Path

from bot.core.models import ChatMessage, Role
from bot.observability.trace import backup_sqlite_database, export_trace_bundle
from bot.sessions import SQLiteSessionStore


def _event(
    sequence: int, event_type: str, session_id: str, run_id: str, payload: dict
) -> dict:
    return {
        "id": f"event-{sequence}",
        "schema_version": 1,
        "type": event_type,
        "session_id": session_id,
        "run_id": run_id,
        "sequence": sequence,
        "timestamp": f"2026-07-21T00:00:{sequence:02d}Z",
        "payload": payload,
    }


def test_trace_bundle_expands_full_context_blobs(tmp_path: Path) -> None:
    state_path = tmp_path / "state.db"
    store = SQLiteSessionStore(state_path)
    session_id = store.create_session(tmp_path, session_id="session-1")
    run_id = "run-1"
    store.start_run(session_id, run_id)
    store.append_message(
        session_id,
        run_id,
        ChatMessage(role=Role.USER, content="inspect everything"),
    )
    full_output = "first line\n" + "complete evidence\n" * 30
    reference = store.put_context_blob(
        session_id=session_id,
        run_id=run_id,
        content=full_output,
        media_type="application/vnd.bot.tool-result+json",
    )
    store.record_tool_run(
        session_id=session_id,
        run_id=run_id,
        tool_call_id="call-1",
        tool_name="read_file",
        arguments={"path": "large.txt"},
        status="completed",
        result={"success": True, "output": full_output},
    )
    store.finish_run(run_id, "completed")
    store.close()

    snapshot_path = tmp_path / "snapshot.db"
    backup_sqlite_database(state_path, snapshot_path)
    with sqlite3.connect(snapshot_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)

    events = [
        _event(1, "run.started", session_id, run_id, {"prompt": "fix it"}),
        _event(2, "assistant.delta", session_id, run_id, {"text": "Inspecting."}),
        _event(
            3,
            "tool.requested",
            session_id,
            run_id,
            {
                "tool_call_id": "call-1",
                "name": "read_file",
                "arguments": {"path": "large.txt"},
            },
        ),
        _event(
            4,
            "tool.completed",
            session_id,
            run_id,
            {
                "tool_call_id": "call-1",
                "name": "read_file",
                "success": True,
                "output_excerpt": "first line...",
                "truncated": False,
                "context_ref": reference,
            },
        ),
        _event(5, "run.completed", session_id, run_id, {"status": "completed"}),
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )

    bundle = tmp_path / "trace"
    manifest = export_trace_bundle(
        events_path=events_path,
        state_path=snapshot_path,
        output_dir=bundle,
        inline_output_chars=100,
    )

    assert manifest["event_count"] == 5
    assert manifest["tool_count"] == 1
    assert manifest["blob_count"] == 1
    assert manifest["message_count"] == 1
    transcript = (bundle / "transcript.md").read_text(encoding="utf-8")
    assert "Inspecting." in transcript
    assert "complete evidence" in transcript
    assert "characters omitted" in transcript
    blob_index = json.loads((bundle / "blobs.json").read_text(encoding="utf-8"))
    blob_path = bundle / blob_index[reference]["file"]
    assert blob_path.read_text(encoding="utf-8") == full_output
    tool_metadata = next((bundle / "tools").glob("*.json"))
    assert json.loads(tool_metadata.read_text(encoding="utf-8"))["arguments"] == {
        "path": "large.txt"
    }
    assert (bundle / "state.db").stat().st_mode & 0o777 == 0o600


def test_trace_bundle_accepts_legacy_non_event_result_record(tmp_path: Path) -> None:
    state_path = tmp_path / "state.db"
    store = SQLiteSessionStore(state_path)
    store.close()
    events_path = tmp_path / "events.jsonl"
    events_path.write_text('{"status":"limit_reached"}\n', encoding="utf-8")

    manifest = export_trace_bundle(
        events_path=events_path,
        state_path=state_path,
        output_dir=tmp_path / "trace",
    )

    assert manifest["event_count"] == 0
    assert manifest["non_event_record_count"] == 1
