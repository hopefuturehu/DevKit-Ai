from __future__ import annotations

import json
import re
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TRACE_SCHEMA_VERSION = 1


def backup_sqlite_database(source: Path, destination: Path) -> None:
    """Create a consistent standalone snapshot of a SQLite database."""
    source = source.resolve()
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    with (
        sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_connection,
        sqlite3.connect(destination) as destination_connection,
    ):
        source_connection.backup(destination_connection)
        integrity = destination_connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise RuntimeError(f"SQLite trace 快照完整性检查失败: {integrity}")
    destination.chmod(0o600)


def export_trace_bundle(
    *,
    events_path: Path,
    state_path: Path,
    output_dir: Path,
    prediction_path: Path | None = None,
    result_path: Path | None = None,
    setup_log_path: Path | None = None,
    stderr_log_path: Path | None = None,
    inline_output_chars: int = 20_000,
) -> dict[str, Any]:
    """Export a durable, readable trace bundle from JSONL events and SQLite state."""
    events_path = events_path.resolve()
    state_path = state_path.resolve()
    output_dir = output_dir.resolve()
    if not events_path.is_file():
        raise FileNotFoundError(f"事件日志不存在: {events_path}")
    if not state_path.is_file():
        raise FileNotFoundError(f"状态快照不存在: {state_path}")
    if inline_output_chars < 1:
        raise ValueError("inline_output_chars 必须大于 0")

    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_dir.chmod(0o700)
    tools_dir = output_dir / "tools"
    blobs_dir = output_dir / "blobs"
    tools_dir.mkdir(exist_ok=True)
    blobs_dir.mkdir(exist_ok=True)
    _clear_regular_files(tools_dir)
    _clear_regular_files(blobs_dir)

    events, non_event_records = _load_events(events_path)
    requested = {
        str(event.get("payload", {}).get("tool_call_id")): event
        for event in events
        if event.get("type") == "tool.requested"
    }

    with sqlite3.connect(f"file:{state_path}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        blob_index, blob_contents = _export_blobs(connection, blobs_dir)
        message_count = _export_json_rows(
            connection,
            "SELECT position, session_id, run_id, role, message_json, created_at "
            "FROM messages ORDER BY session_id, position",
            output_dir / "messages.jsonl",
            json_columns={"message_json": "message"},
        )
        tool_run_count = _export_json_rows(
            connection,
            "SELECT id, session_id, run_id, tool_call_id, tool_name, arguments_json, "
            "status, result_json, created_at FROM tool_runs ORDER BY id",
            output_dir / "tool-runs.jsonl",
            json_columns={"arguments_json": "arguments", "result_json": "result"},
        )

    tool_records: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "tool.completed":
            continue
        payload = event.get("payload", {})
        tool_call_id = str(payload.get("tool_call_id", ""))
        request_payload = requested.get(tool_call_id, {}).get("payload", {})
        reference = payload.get("context_ref")
        blob = blob_index.get(str(reference)) if reference else None
        sequence = int(event.get("sequence", 0))
        tool_name = str(payload.get("name") or request_payload.get("name") or "tool")
        stem = f"{sequence:04d}-{_safe_component(tool_name)}-{_safe_component(tool_call_id[:8])}"
        metadata_path = tools_dir / f"{stem}.json"
        record = {
            "sequence": sequence,
            "timestamp": event.get("timestamp"),
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "arguments": request_payload.get("arguments", {}),
            "success": payload.get("success"),
            "error": payload.get("error"),
            "truncated": payload.get("truncated", False),
            "context_ref": reference,
            "full_output_available": blob is not None,
            "blob": blob,
        }
        _write_json(metadata_path, record)
        record["metadata_file"] = str(metadata_path.relative_to(output_dir))
        if blob is not None:
            record["full_output_file"] = blob["file"]
        tool_records.append(record)

    _write_json(output_dir / "blobs.json", blob_index)
    _copy_private(events_path, output_dir / "events.jsonl")
    _copy_private(state_path, output_dir / "state.db")
    copied_files = {
        "prediction": _copy_optional(prediction_path, output_dir / "prediction.jsonl"),
        "result": _copy_optional(result_path, output_dir / "result.json"),
        "setup_log": _copy_optional(setup_log_path, output_dir / "setup.log"),
        "stderr_log": _copy_optional(stderr_log_path, output_dir / "stderr.log"),
    }

    session_ids = sorted(
        {str(event["session_id"]) for event in events if event.get("session_id")}
    )
    run_ids = sorted({str(event["run_id"]) for event in events if event.get("run_id")})
    terminal = next(
        (
            event
            for event in reversed(events)
            if event.get("type") in {"run.completed", "run.failed"}
        ),
        None,
    )
    manifest = {
        "schema_version": TRACE_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "event_count": len(events),
        "non_event_record_count": len(non_event_records),
        "tool_count": len(tool_records),
        "message_count": message_count,
        "tool_run_count": tool_run_count,
        "blob_count": len(blob_index),
        "session_ids": session_ids,
        "run_ids": run_ids,
        "terminal_event": terminal,
        "files": {
            "transcript": "transcript.md",
            "events": "events.jsonl",
            "state": "state.db",
            "messages": "messages.jsonl",
            "tool_runs": "tool-runs.jsonl",
            "blob_index": "blobs.json",
            **{key: value for key, value in copied_files.items() if value is not None},
        },
    }
    _write_json(output_dir / "manifest.json", manifest)
    transcript = _render_markdown(
        events=events,
        tool_records=tool_records,
        blob_contents=blob_contents,
        manifest=manifest,
        inline_output_chars=inline_output_chars,
    )
    _write_private(output_dir / "transcript.md", transcript)
    return manifest


def _load_events(path: Path) -> tuple[list[dict[str, Any]], list[Any]]:
    events: list[dict[str, Any]] = []
    non_events: list[Any] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number} 不是有效 JSON") from exc
        if isinstance(value, dict) and isinstance(value.get("type"), str):
            events.append(value)
        else:
            non_events.append(value)
    return events, non_events


def _export_blobs(
    connection: sqlite3.Connection, blobs_dir: Path
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    index: dict[str, dict[str, Any]] = {}
    text_contents: dict[str, str] = {}
    rows = connection.execute(
        "SELECT id, session_id, run_id, media_type, byte_count, sha256, content, created_at "
        "FROM context_blobs ORDER BY created_at, id"
    ).fetchall()
    for row in rows:
        reference = str(row["id"])
        content = bytes(row["content"])
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError:
            decoded = None
        extension = ".txt" if decoded is not None else ".bin"
        filename = f"{row['sha256']}{extension}"
        destination = blobs_dir / filename
        if decoded is None:
            destination.write_bytes(content)
            destination.chmod(0o600)
        else:
            _write_private(destination, decoded)
            text_contents[reference] = decoded
        index[reference] = {
            "file": str(destination.relative_to(blobs_dir.parent)),
            "session_id": row["session_id"],
            "run_id": row["run_id"],
            "media_type": row["media_type"],
            "byte_count": int(row["byte_count"]),
            "sha256": row["sha256"],
            "created_at": row["created_at"],
            "text": decoded is not None,
        }
    return index, text_contents


def _export_json_rows(
    connection: sqlite3.Connection,
    query: str,
    destination: Path,
    *,
    json_columns: dict[str, str],
) -> int:
    rows = connection.execute(query).fetchall()
    lines: list[str] = []
    for row in rows:
        item = dict(row)
        for source_name, destination_name in json_columns.items():
            raw = item.pop(source_name)
            item[destination_name] = json.loads(raw) if raw is not None else None
        lines.append(json.dumps(item, ensure_ascii=False))
    _write_private(destination, "".join(f"{line}\n" for line in lines))
    return len(lines)


def _render_markdown(
    *,
    events: list[dict[str, Any]],
    tool_records: list[dict[str, Any]],
    blob_contents: dict[str, str],
    manifest: dict[str, Any],
    inline_output_chars: int,
) -> str:
    lines = [
        "# Bot Agent Trace",
        "",
        f"- Events: {manifest['event_count']}",
        f"- Tool calls: {manifest['tool_count']}",
        f"- Full blobs: {manifest['blob_count']}",
        f"- Sessions: {', '.join(manifest['session_ids']) or 'n/a'}",
        f"- Runs: {', '.join(manifest['run_ids']) or 'n/a'}",
        "",
    ]
    started = next((event for event in events if event.get("type") == "run.started"), None)
    if started:
        lines.extend(["## Prompt", "", _fenced(str(started.get("payload", {}).get("prompt", "")))])

    assistant_chunks: list[str] = []
    assistant_section = 0

    def flush_assistant() -> None:
        nonlocal assistant_section
        if not assistant_chunks:
            return
        assistant_section += 1
        lines.extend(
            [
                f"## Assistant message {assistant_section}",
                "",
                "".join(assistant_chunks),
                "",
            ]
        )
        assistant_chunks.clear()

    for event in events:
        if event.get("type") == "assistant.delta":
            assistant_chunks.append(str(event.get("payload", {}).get("text", "")))
        elif assistant_chunks:
            flush_assistant()
    flush_assistant()

    lines.extend(["## Tool calls", ""])
    for record in tool_records:
        reference = str(record.get("context_ref") or "")
        full_output = blob_contents.get(reference)
        lines.extend(
            [
                f"### Step {record['sequence']} — `{record['name']}`",
                "",
                f"- Timestamp: {record.get('timestamp')}",
                f"- Success: {record.get('success')}",
                f"- Truncated by tool: {record.get('truncated')}",
                f"- Context ref: `{reference or 'n/a'}`",
                f"- Metadata: [{record['metadata_file']}]({record['metadata_file']})",
                "",
                "Arguments:",
                "",
                _fenced(
                    json.dumps(record.get("arguments", {}), ensure_ascii=False, indent=2),
                    "json",
                ),
                "",
            ]
        )
        if record.get("error"):
            lines.extend(["Error:", "", _fenced(str(record["error"])), ""])
        if full_output is None:
            lines.extend(["Full output is unavailable in the exported state database.", ""])
            continue
        output_file = str(record["full_output_file"])
        lines.extend([f"Full output: [{output_file}]({output_file})", ""])
        if len(full_output) <= inline_output_chars:
            lines.extend([_fenced(full_output), ""])
        else:
            preview = full_output[:inline_output_chars]
            lines.extend(
                [
                    _fenced(
                        preview
                        + f"\n\n… {len(full_output) - inline_output_chars} characters omitted "
                        "from Markdown; open the linked blob for the complete output."
                    ),
                    "",
                ]
            )

    terminal = manifest.get("terminal_event")
    if terminal:
        lines.extend(
            [
                "## Terminal state",
                "",
                _fenced(json.dumps(terminal, ensure_ascii=False, indent=2), "json"),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _fenced(content: str, language: str = "text") -> str:
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", content)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{content}\n{fence}"


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-.")
    return cleaned[:80] or "unknown"


def _clear_regular_files(directory: Path) -> None:
    for child in directory.iterdir():
        if child.is_file():
            child.unlink()


def _copy_private(source: Path, destination: Path) -> str:
    shutil.copy2(source, destination)
    destination.chmod(0o600)
    return str(destination.name)


def _copy_optional(source: Path | None, destination: Path) -> str | None:
    if source is None or not source.is_file():
        return None
    return _copy_private(source.resolve(), destination)


def _write_json(path: Path, value: Any) -> None:
    _write_private(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _write_private(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
