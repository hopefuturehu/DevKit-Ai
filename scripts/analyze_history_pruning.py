"""Read-only, offline capacity audit; does not implement production pruning.

No raw prompts, arguments, or outputs are emitted. Token counts use bot's local
estimator, not provider billing. SQLite connections use mode=ro and a read
transaction. Benchmark trace/report replicas are intentionally excluded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot.compaction.service import ContextCompactor
from bot.config.models import AppConfig
from bot.core.context import PositionedMessage, TokenEstimator
from bot.core.models import ChatMessage, Role

EST = TokenEstimator()
REF = re.compile(r"context_ref=(blob:[0-9a-f]{64})")
LARGE = 1024
MIN_SAVING = 256
SKILL_TOOLS = {"activate_skill", "load_skill_resource"}
ORDINARY_TOOLS = {"read_file", "search_text", "run_command", "run_shell", "fetch_url"}


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@dataclass
class Record:
    entry: PositionedMessage
    created_at: str
    tool: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    reference: str | None = None
    raw: bytes | None = None


class ReadOnlyStatuses:
    def __init__(self, statuses: dict[str, str]) -> None:
        self.statuses = statuses

    def run_statuses(self, ids: set[str]) -> dict[str, str]:
        return {key: self.statuses[key] for key in ids if key in self.statuses}


def protected_tail(records: list[Record], tokens: int) -> set[int]:
    """Protect complete assistant/tool groups in a recent-token window."""
    owners = {}
    groups: dict[int, list[Record]] = {}
    for record in records:
        for call in record.entry.message.tool_calls:
            owners[call.id] = record.entry.position
    for record in records:
        entry = record.entry
        owner = owners.get(entry.message.tool_call_id or "", entry.position)
        groups.setdefault(owner, []).append(record)
    positions: set[int] = set()
    used = 0
    for group in reversed(list(groups.values())):
        if used >= tokens:
            break
        positions.update(record.entry.position for record in group)
        used += sum(EST.message(record.entry.message) for record in group)
    return positions


def completed_success(record: Record) -> bool:
    result = record.result or {}
    status = result.get("status") or ("completed" if result.get("success") else "failed")
    return result.get("success") is True and status == "completed"


def duplicate_key(record: Record) -> str | None:
    """Conservative: same run, exact arguments, result identity, full bytes."""
    if (
        not record.tool
        or record.raw is None
        or not completed_success(record)
        or record.tool["tool_name"] in SKILL_TOOLS
    ):
        return None
    if (record.result or {}).get("truncated"):
        return None
    meta = (record.result or {}).get("metadata", {})
    identity = {
        key: meta.get(key)
        for key in (
            "path",
            "start_line",
            "end_line",
            "cwd",
            "argv",
            "returncode",
            "process_id",
            "process_status",
            "result_count",
        )
    }
    return dump(
        [
            record.entry.run_id,
            record.tool["tool_name"],
            json.loads(record.tool["arguments_json"]),
            identity,
            hashlib.sha256(record.raw).hexdigest(),
        ]
    )


def full_body_visible(record: Record) -> bool:
    if not record.raw:
        return False
    try:
        body = record.raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    # The normal inline path appends a reference after an unchanged full body.
    # Merely holding the complete blob is not enough for a retained representative.
    return (record.entry.message.content or "").startswith(body)


def receipt(record: Record, *, duplicate_position: int | None = None) -> str:
    """Keep call arguments unchanged; retain a structured, recoverable result."""
    result = record.result or {}
    meta = result.get("metadata", {})
    payload: dict[str, Any] = {
        "status": "completed",
        "success": True,
        "context_ref": record.reference,
        "body_bytes": len(record.raw or b""),
        "body_externalized": True,
        "source_truncated": bool(result.get("truncated")),
    }
    for key in ("path", "start_line", "end_line", "returncode", "result_count"):
        if key in meta:
            payload[key] = meta[key]
    if duplicate_position is not None:
        payload["same_body_as_position"] = duplicate_position
    else:
        visible = record.entry.message.content or ""
        # This bounds attached data, not a claim to semantic equivalence.
        payload["excerpt_head"] = visible[:512]
        payload["excerpt_tail"] = visible[-256:]
    return dump(payload)


def prune(
    records: list[Record],
    *,
    protected: set[int],
    old_success: bool,
) -> tuple[dict[int, str], Counter[str], list[dict[str, Any]]]:
    selected = {record.entry.position: record for record in records}
    owners = {
        call.id: record.entry.position
        for record in records
        for call in record.entry.message.tool_calls
    }
    groups: dict[str, list[Record]] = defaultdict(list)
    for record in records:
        key = duplicate_key(record)
        if key is not None and record.entry.message.tool_call_id in owners:
            groups[key].append(record)
    replacements: dict[int, str] = {}
    savings: Counter[str] = Counter()
    audit = []
    representatives: set[int] = set()
    for group in groups.values():
        if len(group) < 2:
            continue
        representative = next((r for r in reversed(group) if full_body_visible(r)), None)
        if representative is None:
            continue
        representatives.add(representative.entry.position)
        for record in group:
            if record is representative:
                continue
            if record.raw != representative.raw:
                continue
            before = EST.text(record.entry.message.content or "")
            after = receipt(record, duplicate_position=representative.entry.position)
            saved = before - EST.text(after)
            if before >= LARGE and saved >= MIN_SAVING:
                replacements[record.entry.position] = after
                savings["duplicate"] += saved
                audit.append(
                    {
                        "position": record.entry.position,
                        "tier": "duplicate",
                        "saved": saved,
                        "retained": representative.entry.position,
                        "tool": record.tool["tool_name"],
                    }
                )
    if old_success:
        for record in records:
            position = record.entry.position
            if (
                position in replacements
                or position in representatives
                or position in protected
                or record.raw is None
                or not record.tool
                or not completed_success(record)
                or record.tool["tool_name"] not in ORDINARY_TOOLS
                or record.entry.message.tool_call_id not in owners
            ):
                continue
            before = EST.text(record.entry.message.content or "")
            after = receipt(record)
            saved = before - EST.text(after)
            if before >= LARGE and saved >= MIN_SAVING:
                replacements[position] = after
                savings["old_success"] += saved
                audit.append(
                    {
                        "position": position,
                        "tier": "old_success",
                        "saved": saved,
                        "tool": record.tool["tool_name"],
                    }
                )
    # A duplicate's retained body must remain in this exact target view.
    for item in audit:
        if item["tier"] == "duplicate":
            assert item["retained"] in selected and item["retained"] not in replacements
            assert full_body_visible(selected[item["retained"]])
    assert all(completed_success(selected[position]) for position in replacements)
    return replacements, savings, audit


def distribution(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    return {
        "n": len(values),
        "positive": sum(v > 0 for v in values),
        "mean": round(statistics.mean(values), 2),
        "median": round(statistics.median(values), 2),
        "min": round(ordered[0], 2),
        "max": round(ordered[-1], 2),
        "p90": round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * 0.9))], 2),
    }


def analyze_database(path: Path, cohort: str) -> dict[str, Any]:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro")
    connection.row_factory = sqlite3.Row
    connection.execute("BEGIN")
    try:
        runs = {r["id"]: dict(r) for r in connection.execute("SELECT * FROM runs")}
        tools = {
            (r["session_id"], r["run_id"], r["tool_call_id"]): dict(r)
            for r in connection.execute("SELECT * FROM tool_runs")
        }
        sessions: dict[str, list[Record]] = defaultdict(list)
        blob_cache: dict[tuple[str, str], bytes | None] = {}
        for row in connection.execute("SELECT * FROM messages ORDER BY session_id,position"):
            message = ChatMessage.model_validate_json(row["message_json"])
            entry = PositionedMessage(row["position"], message, row["run_id"])
            tool = tools.get((row["session_id"], row["run_id"], message.tool_call_id))
            result = json.loads(tool["result_json"]) if tool and tool["result_json"] else None
            match = REF.search(message.content or "") if message.role == Role.TOOL else None
            reference = match.group(1) if match else None
            raw = None
            if reference:
                key = (row["session_id"], reference)
                if key not in blob_cache:
                    blob = connection.execute(
                        "SELECT b.content,b.sha256 FROM context_blobs b JOIN context_blob_access a "
                        "ON a.blob_id=b.id WHERE a.session_id=? AND b.id=?",
                        key,
                    ).fetchone()
                    blob_cache[key] = bytes(blob[0]) if blob else None
                    if blob:
                        assert hashlib.sha256(bytes(blob[0])).hexdigest() == blob[1]
                raw = blob_cache[key]
            sessions[row["session_id"]].append(
                Record(entry, row["created_at"], tool, result, reference, raw)
            )
        compactions = [
            dict(r)
            for r in connection.execute("SELECT * FROM context_compactions ORDER BY created_at")
        ]
        compaction_map = {c["id"]: c for c in compactions}
        events = [
            dict(r)
            for r in connection.execute(
                "SELECT * FROM events WHERE type IN "
                "('context.compaction.started','context.packed') ORDER BY sequence"
            )
        ]
        starts = {
            json.loads(e["payload_json"])["compaction_id"]: json.loads(e["payload_json"])
            for e in events
            if e["type"] == "context.compaction.started"
        }
        model_tools = Counter(tool["tool_name"] for tool in tools.values())
        inventory = {
            "path": str(path),
            "cohort": cohort,
            "sessions": connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            "nonempty_sessions": len(sessions),
            "runs": len(runs),
            "messages": sum(map(len, sessions.values())),
            "tool_runs": len(tools),
            "tool_counts": dict(model_tools),
            "compactions": len(compactions),
            "large_visible_tool_results": sum(
                r.entry.message.role == Role.TOOL
                and EST.text(r.entry.message.content or "") >= LARGE
                for records in sessions.values()
                for r in records
            ),
            "large_tool_results_with_verified_blob": sum(
                r.raw is not None and EST.text(r.entry.message.content or "") >= LARGE
                for records in sessions.values()
                for r in records
            ),
            "messages_sha256": hashlib.sha256(
                dump(
                    [
                        [sid, r.entry.position, r.entry.message.model_dump(mode="json")]
                        for sid, records in sessions.items()
                        for r in records
                    ]
                ).encode()
            ).hexdigest(),
        }
        source_rows = []
        for compaction in compactions:
            sid = compaction["session_id"]
            event = starts.get(compaction["id"], {})
            rebuilt = event.get("rebuilt_from_raw", compaction["parent_id"] is None)
            start = (
                compaction["covered_start_position"]
                if rebuilt
                else compaction["delta_start_position"]
            )
            records = [
                r
                for r in sessions[sid]
                if start <= r.entry.position <= compaction["covered_end_position"]
            ]
            closures = event.get("logical_tool_closures", [])
            closed_runs = {item["run_id"] for item in closures}
            statuses = {key: row["status"] for key, row in runs.items()}
            statuses.update({item["run_id"]: item["stored_run_status"] for item in closures})
            active = {
                key
                for key, row in runs.items()
                if row["started_at"] <= compaction["created_at"]
                and (row["completed_at"] is None or row["completed_at"] >= compaction["created_at"])
            } - closed_runs
            compactor = ContextCompactor(
                config=AppConfig(), provider=None, store=ReadOnlyStatuses(statuses), event_bus=None
            )
            payload = compactor._canonical_source_payload(
                [r.entry for r in records],
                active_run_ids=active,
                content_limit=512 if event.get("degraded_source") else None,
                degraded=bool(event.get("degraded_source")),
            )
            source_text = dump(payload)
            source_match = len(source_text) == compaction["source_chars"]
            source_hash_match = (
                compactor._digest(
                    [
                        r.entry
                        for r in sessions[sid]
                        if r.entry.position <= compaction["covered_end_position"]
                    ]
                )
                == compaction["source_sha256"]
            )
            parent = compaction_map.get(compaction["parent_id"])
            cursor = parent["covered_end_position"] if parent else 0
            window = [
                r
                for r in sessions[sid]
                if r.entry.position > cursor and r.created_at <= compaction["created_at"]
            ]
            _, window_duplicates, window_duplicate_audit = prune(
                window, protected=protected_tail(window, 20000), old_success=False
            )
            # Pruning measures the already bounded summary source, never raw blob volume.
            body_by_position = {
                item["position"]: item["content"] for item in payload if not item.get("derived")
            }
            bounded_records = [
                Record(
                    PositionedMessage(
                        r.entry.position,
                        r.entry.message.model_copy(
                            update={"content": body_by_position[r.entry.position]}
                        ),
                        r.entry.run_id,
                    ),
                    r.created_at,
                    r.tool,
                    r.result,
                    r.reference,
                    r.raw,
                )
                for r in records
                if r.entry.position in body_by_position
            ]
            outcome = {
                "cohort": cohort,
                "database": str(path),
                "session": sid[:8],
                "compaction": compaction["id"][:8],
                "trigger": compaction["trigger"],
                "status": compaction["status"],
                "range": [start, compaction["covered_end_position"]],
                "source_chars_match": source_match,
                "recorded_source_chars": compaction["source_chars"],
                "original_source_hash_match": source_hash_match,
                "reconstructed_source_chars": len(source_text),
                "source_tokens": EST.text(source_text),
                "tool_source_tokens": sum(
                    EST.text(r.entry.message.content or "")
                    for r in bounded_records
                    if r.entry.message.role == Role.TOOL
                ),
                "recorded_planned_input_tokens": event.get("planned_input_tokens"),
                "pre_compaction_history_duplicate_tokens": window_duplicates["duplicate"],
                "pre_compaction_history_duplicate_pairs": window_duplicate_audit,
                "variants": {},
            }
            for name, tail, shrink in [
                ("duplicate_only", 20000, False),
                ("old_success_20k", 20000, True),
                ("old_success_8k", 8000, True),
            ]:
                replacements, tier_savings, audit = prune(
                    bounded_records, protected=protected_tail(window, tail), old_success=shrink
                )
                changed = [
                    {**item, "content": replacements[item["position"]]}
                    if not item.get("derived") and item["position"] in replacements
                    else item
                    for item in payload
                ]
                saved = EST.text(source_text) - EST.text(dump(changed))
                outcome["variants"][name] = {
                    "saved_tokens": saved,
                    "source_ratio": round(saved / max(1, EST.text(source_text)), 6),
                    "replaced_results": len(replacements),
                    "tier_body_savings": dict(tier_savings),
                    "audit": audit,
                }
            source_rows.append(outcome)
        packed_rows = []
        for event in events:
            if event["type"] != "context.packed":
                continue
            report = json.loads(event["payload_json"])
            layers = report.get("layers", {})
            if "tool_result" not in layers:
                continue
            sid = event["session_id"]
            previous = [
                c
                for c in compactions
                if c["session_id"] == sid
                and c["ready_at"]
                and c["ready_at"] <= event["timestamp"]
                and (not c["superseded_at"] or c["superseded_at"] > event["timestamp"])
            ]
            active = max(previous, key=lambda c: c["ready_at"], default=None)
            cursor = active["covered_end_position"] if active else 0
            dropped = {
                int(item["id"].split(":")[1])
                for item in report.get("dropped_items", [])
                if item.get("id", "").startswith("message:")
            }
            records = [
                r
                for r in sessions[sid]
                if r.entry.position > cursor
                and r.created_at <= event["timestamp"]
                and r.entry.position not in dropped
                and r.entry.message.assistant_payload_error() is None
            ]
            estimated_tools = sum(
                EST.message(r.entry.message) for r in records if r.entry.message.role == Role.TOOL
            )
            estimated_recent = sum(
                EST.message(r.entry.message) for r in records if r.entry.message.role != Role.TOOL
            )
            dropped_tokens = sum(item["tokens"] for item in report.get("dropped_items", []))
            outcome = {
                "cohort": cohort,
                "database": str(path),
                "session": sid[:8],
                "sequence": event["sequence"],
                "estimated_tokens": report["estimated_tokens"],
                "target_limit": report["target_limit"],
                "recorded_tool_tokens": layers["tool_result"],
                "reconstructed_tool_tokens": estimated_tools,
                "recorded_recent_tokens": layers.get("recent_conversation", 0),
                "reconstructed_recent_tokens": estimated_recent,
                "recent_layer_match": estimated_recent == layers.get("recent_conversation", 0),
                "dropped_tokens": dropped_tokens,
                "tool_layer_match": estimated_tools == layers["tool_result"],
                "variants": {},
            }
            for name, tail, shrink in [
                ("duplicate_only", 20000, False),
                ("old_success_20k", 20000, True),
                ("old_success_8k", 8000, True),
            ]:
                replacements, tier_savings, audit = prune(
                    records, protected=protected_tail(records, tail), old_success=shrink
                )
                saved = sum(tier_savings.values())
                outcome["variants"][name] = {
                    "saved_tokens": saved,
                    "request_ratio": round(saved / max(1, report["estimated_tokens"]), 6),
                    "could_fit_original_candidates": report["estimated_tokens"]
                    + dropped_tokens
                    - saved
                    <= report["target_limit"],
                    "replaced_results": len(replacements),
                    "tier_body_savings": dict(tier_savings),
                    "audit": audit,
                }
            packed_rows.append(outcome)
        # End-of-session capacity is a reference cohort, not a compression trigger.
        endings = []
        for sid, records in sessions.items():
            replacements, savings, audit = prune(
                records, protected=protected_tail(records, 20000), old_success=True
            )
            endings.append(
                {
                    "session": sid[:8],
                    "messages": len(records),
                    "saved_tokens": sum(savings.values()),
                    "duplicate_tokens": savings["duplicate"],
                    "old_success_tokens": savings["old_success"],
                    "replaced_results": len(replacements),
                    "duplicate_pairs": [item for item in audit if item["tier"] == "duplicate"],
                }
            )
        return {
            "inventory": inventory,
            "compaction_sources": source_rows,
            "packed_windows": packed_rows,
            "session_end_reference": endings,
        }
    finally:
        connection.close()


def summarize(rows: list[dict[str, Any]], ratio_key: str) -> dict[str, Any]:
    return {
        name: {
            "saved_tokens": distribution([row["variants"][name]["saved_tokens"] for row in rows]),
            "saved_percent": distribution([100 * row["variants"][name][ratio_key] for row in rows]),
            "replaced_results": distribution(
                [row["variants"][name]["replaced_results"] for row in rows]
            ),
        }
        for name in ("duplicate_only", "old_success_20k", "old_success_8k")
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--output", type=Path, default=Path(".bot/analysis/history-pruning/results.json")
    )
    parser.add_argument("--summary-output", type=Path)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    candidates = [(workspace / ".bot/state.db", "main")]
    candidates += [
        (p, "terminalbench")
        for p in sorted((workspace / "artifacts/terminalbench/jobs").glob("*/*/agent/state.db"))
    ]
    datasets = []
    excluded = []
    seen = set()
    for path, cohort in candidates:
        if not path.exists():
            continue
        try:
            result = analyze_database(path, cohort)
        except sqlite3.DatabaseError as exc:
            excluded.append({"path": str(path.relative_to(workspace)), "reason": str(exc)})
            continue
        fingerprint = result["inventory"]["messages_sha256"]
        if fingerprint in seen:
            excluded.append(
                {
                    "path": str(path.relative_to(workspace)),
                    "reason": "duplicate transcript fingerprint",
                }
            )
            continue
        seen.add(fingerprint)
        # Make checked-in aggregates portable without exposing home paths.
        result["inventory"]["path"] = str(path.relative_to(workspace))
        for row in result["compaction_sources"] + result["packed_windows"]:
            row["database"] = str(path.relative_to(workspace))
        datasets.append(result)
        print(
            f"scanned {cohort}: {result['inventory']['messages']} messages, "
            f"{len(result['compaction_sources'])} compactions",
            flush=True,
        )
    sources = [row for dataset in datasets for row in dataset["compaction_sources"]]
    packed = [row for dataset in datasets for row in dataset["packed_windows"]]
    summary = {}
    for cohort in ("main", "terminalbench", "all"):
        source_rows = [row for row in sources if cohort == "all" or row["cohort"] == cohort]
        packed_rows = [
            row
            for row in packed
            if (cohort == "all" or row["cohort"] == cohort)
            and row["tool_layer_match"]
            and row["recent_layer_match"]
        ]
        unique = {}
        for row in source_rows:
            if row["source_chars_match"] and row["original_source_hash_match"]:
                unique[(row["database"], row["session"], tuple(row["range"]))] = row
        summary[cohort] = {
            "source_observations": len(source_rows),
            "distinct_source_ranges": len(unique),
            "source_chars_matched": sum(row["source_chars_match"] for row in source_rows),
            "original_source_hash_matched": sum(
                row["original_source_hash_match"] for row in source_rows
            ),
            "source_capacity": summarize(list(unique.values()), "source_ratio"),
            "pre_compaction_history_duplicate_tokens": distribution(
                [row["pre_compaction_history_duplicate_tokens"] for row in unique.values()]
            ),
            "matched_packed_windows": len(packed_rows),
            "matched_packed_sessions": len({row["session"] for row in packed_rows}),
            "original_candidates_could_fit": {
                variant: sum(
                    row["variants"][variant]["could_fit_original_candidates"] for row in packed_rows
                )
                for variant in ("duplicate_only", "old_success_20k", "old_success_8k")
            },
            "packed_capacity": summarize(packed_rows, "request_ratio"),
        }
    output = {
        "schema_version": 1,
        "estimator": "bot.core.context.TokenEstimator; not provider billing tokens",
        "analysis_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "parameters": {
            "large_visible_tokens": LARGE,
            "min_net_saving": MIN_SAVING,
            "receipt_head_chars": 512,
            "receipt_tail_chars": 256,
        },
        "summary": summary,
        "excluded": excluded,
        "datasets": datasets,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    if args.summary_output:
        compact = {key: value for key, value in output.items() if key != "datasets"}
        compact["inventories"] = [dataset["inventory"] for dataset in datasets]
        compact["compaction_sources"] = [
            {
                **row,
                "variants": {
                    name: {key: value for key, value in variant.items() if key != "audit"}
                    for name, variant in row["variants"].items()
                },
            }
            for row in sources
        ]
        args.summary_output.parent.mkdir(parents=True, exist_ok=True)
        args.summary_output.write_text(json.dumps(compact, ensure_ascii=False, indent=2) + "\n")
    print(f"Wrote {args.output}; {len(sources)} source observations, {len(packed)} packed windows")


if __name__ == "__main__":
    main()
