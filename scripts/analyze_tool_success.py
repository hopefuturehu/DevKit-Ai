"""Read-only tool-call accounting, including legacy results and preflight failures.

Raw snapshots and per-call evidence stay under .bot/analysis by default. A success
here is a successful tool result, not proof that the user's task was completed.
Failure root causes require review of arguments/output; this script counts only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

EVENT_TYPES = ("tool.requested", "tool.started", "tool.completed", "tool.result")
FAILURE_PREFIXES = ("工具执行失败:", "工具执行超时:", "工具执行已取消:", "工具未执行:")


def capture(database: Path) -> dict[str, Any]:
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        queries = {
            "tool_runs": "SELECT * FROM tool_runs ORDER BY id",
            "events": "SELECT * FROM events WHERE type IN ("
            + ",".join("?" for _ in EVENT_TYPES)
            + ") ORDER BY timestamp, rowid",
            "runs": "SELECT * FROM runs ORDER BY started_at, id",
            "sessions": "SELECT id, parent_session_id, created_at, updated_at "
            "FROM sessions ORDER BY id",
            "tool_messages": "SELECT session_id, run_id, position, message_json, created_at "
            "FROM messages WHERE role='tool' ORDER BY session_id, position",
        }
        data = {
            table: [
                dict(row)
                for row in connection.execute(sql, EVENT_TYPES if table == "events" else ())
            ]
            for table, sql in queries.items()
        }
        data["captured_at"] = datetime.now(UTC).isoformat()
        data["source"] = str(database)
        return data
    finally:
        connection.close()


def call_key(row: dict[str, Any], call_id: str) -> tuple[str, str, str]:
    return row["session_id"], row["run_id"], call_id


def legacy_result(name: str, content: str) -> dict[str, Any]:
    # Do not infer success merely from the presence of a nonempty message.
    if content.startswith(FAILURE_PREFIXES):
        return {"success": False, "status": "failed", "error": content.splitlines()[0]}
    prefixes = {
        "load_context_reference": (
            '{"reference": "blob:',
            '{"status": "disposable_context_delivery"',
        ),
        "search_session_history": ('{"matches": [',),
        "search_memory": ('{"matches": [',),
    }
    # Some older JSON results are truncated; recognize only these known envelopes.
    if name in prefixes and content.startswith(prefixes[name]):
        return {"success": True, "status": "completed", "error": None}
    return {}


def outcome(result: dict[str, Any]) -> str:
    status = result.get("status")
    if status == "running":
        return "running"
    if status in {"cancelled", "timed_out"}:
        return status
    if result.get("success") is True:
        return "success"
    if result.get("success") is False:
        return "failed"
    return "unknown"


def stats(calls: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(call["outcome"] for call in calls)
    terminal = sum(counts[k] for k in ("success", "failed", "timed_out", "cancelled"))
    return {
        "total": len(calls),
        **{
            k: counts[k]
            for k in ("success", "failed", "timed_out", "cancelled", "running", "unknown")
        },
        "terminal": terminal,
        "success_rate_pct": round(100 * counts["success"] / terminal, 6) if terminal else None,
    }


def analyze(data: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    requests = {}
    results: dict[tuple[str, str, str], dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    event_counts = Counter()
    for event in sorted(data["events"], key=lambda e: (e["timestamp"], e["sequence"])):
        if event["type"] not in EVENT_TYPES:
            continue
        payload = json.loads(event["payload_json"])
        key = call_key(event, payload["tool_call_id"])
        event_counts[event["type"]] += 1
        if event["type"] == "tool.requested":
            if key in requests:
                raise ValueError(f"Repeated request identity: {key}")
            requests[key] = {**event, "payload": payload}
        elif event["type"] in {"tool.result", "tool.completed"}:
            results[key][event["type"]].append(payload)

    executions = {}
    for row in data["tool_runs"]:
        key = call_key(row, row["tool_call_id"])
        if key in executions:
            raise ValueError(f"Repeated execution identity: {key}")
        executions[key] = row
    messages = {}
    for row in data["tool_messages"]:
        message = json.loads(row["message_json"])
        key = call_key(row, message.get("tool_call_id"))
        messages[key] = message.get("content") or ""

    calls = []
    conflicts = []
    for key, request in requests.items():
        payload = request["payload"]
        row = executions.get(key)
        candidates = []
        for event_type in ("tool.result", "tool.completed"):
            candidates.extend((event_type, value) for value in results[key].get(event_type, []))
        if row and row.get("result_json"):
            value = json.loads(row["result_json"])
            value["status"] = value.get("status") or row["status"]
            candidates.append(("tool_runs", value))
        known = {outcome(v) for _, v in candidates} - {"unknown"}
        if len(known) > 1:
            conflicts.append(list(key))
        if candidates:
            evidence, result = candidates[0]
        else:
            result = legacy_result(payload["name"], messages.get(key, ""))
            evidence = "legacy_message" if result else "missing"
        error = result.get("error") or ""
        preflight = bool(
            result.get("executed") is False
            or error.startswith("工具未执行:")
            or any(part in error for part in ("策略拒绝:", "Tool 参数校验失败:", "未知 Tool:"))
        )
        calls.append(
            {
                "session_id": key[0],
                "run_id": key[1],
                "tool_call_id": key[2],
                "tool_run_id": row["id"] if row else None,
                "name": payload["name"],
                "timestamp": request["timestamp"],
                "outcome": outcome(result),
                "evidence": evidence,
                "preflight": preflight,
                "error": error,
            }
        )
    if conflicts:
        raise ValueError(f"Conflicting structured outcomes: {conflicts}")

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    months: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for call in calls:
        groups[call["name"]].append(call)
        month = datetime.fromisoformat(call["timestamp"]).astimezone(ZoneInfo("Asia/Shanghai"))
        months[month.strftime("%Y-%m")].append(call)
    ordinary = [call for call in calls if call["tool_run_id"] is not None]
    summary = {
        "captured_at": data["captured_at"],
        "source": data["source"],
        "period": [min(c["timestamp"] for c in calls), max(c["timestamp"] for c in calls)]
        if calls
        else [],
        "inventory": {
            "sessions": len(data["sessions"]),
            "runs": len(data["runs"]),
            "sessions_with_requests": len({c["session_id"] for c in calls}),
            "runs_with_requests": len({c["run_id"] for c in calls}),
            "tool_run_rows": len(executions),
            "event_counts": dict(event_counts),
            "orphan_executions": len(set(executions) - set(requests)),
            "orphan_results": len(set(results) - set(requests)),
            "structured_conflicts": len(conflicts),
        },
        "all_requests": stats(calls),
        "ordinary_executions": stats(ordinary),
        "preflight": stats([c for c in calls if c["preflight"]]),
        "legacy_messages": stats([c for c in calls if c["evidence"] == "legacy_message"]),
        "by_tool": {name: stats(values) for name, values in sorted(groups.items())},
        "ordinary_by_tool": {
            name: stats([c for c in values if c["tool_run_id"] is not None])
            for name, values in sorted(groups.items())
            if any(c["tool_run_id"] is not None for c in values)
        },
        "by_month_shanghai": {name: stats(values) for name, values in sorted(months.items())},
    }
    return summary, calls


def write_private(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        path.chmod(0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument("--database", type=Path, default=Path(".bot/state.db"))
    sources.add_argument("--snapshot", type=Path)
    parser.add_argument("--output", type=Path, default=Path(".bot/analysis/tool-success"))
    args = parser.parse_args()
    snapshot = args.snapshot or args.output / "source.json"
    if args.snapshot:
        data = json.loads(snapshot.read_text(encoding="utf-8"))
    else:
        data = capture(args.database)
        write_private(snapshot, data)
    summary, calls = analyze(data)
    summary["snapshot_sha256"] = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    write_private(args.output / "summary.json", summary)
    write_private(args.output / "calls.json", calls)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
