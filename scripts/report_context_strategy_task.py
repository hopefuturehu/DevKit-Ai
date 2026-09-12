#!/usr/bin/env python3
"""Audit frozen task trials, persisted summaries, and repeated tool activity."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

from bot.compaction.service import ContextCompactor
from bot.core.context import PositionedMessage, TokenEstimator
from bot.core.models import ChatMessage
from bot.evals.context_strategy_audit import aggregate, audit


def reference(path: Path, root: Path) -> dict:
    return {
        "path": str(path.relative_to(root)),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def stagnation(events: list[dict], switches: list[dict]) -> dict:
    calls = [e["payload"] for e in events if e["type"] == "tool.requested"]

    def signature(call: dict) -> tuple[str, str]:
        return call["name"], json.dumps(call["arguments"], sort_keys=True)

    frequencies = Counter(signature(call) for call in calls)
    dominant, count = frequencies.most_common(1)[0] if frequencies else (("", "{}"), 0)
    call_ids = {call["tool_call_id"] for call in calls if signature(call) == dominant}
    results = [
        e["payload"]
        for e in events
        if e["type"] == "tool.result" and e["payload"]["tool_call_id"] in call_ids
    ]
    progress = [e["payload"] for e in events if e["type"] == "run.progress"]
    first_step = None
    step = None
    for event in events:
        if event["type"] == "model.response":
            step = event["payload"].get("step")
        if event["type"] == "tool.requested" and signature(event["payload"]) == dominant:
            first_step = step
            break
    return {
        "tool_calls": len(calls),
        "distinct_call_signatures": len(frequencies),
        "dominant_command": {
            "name": dominant[0],
            "arguments": json.loads(dominant[1]),
            "count": count,
            "first_step": first_step,
        },
        "dominant_successful_results": sum(r.get("success", False) for r in results),
        "dominant_result_context_refs": dict(Counter(r.get("context_ref") for r in results)),
        "dominant_distinct_process_ids": len(
            {r["process_id"] for r in results if r.get("process_id")}
        ),
        "progress_kinds": dict(Counter(p.get("progress") for p in progress)),
        "progress_actions": dict(Counter(p.get("action") for p in progress)),
        "max_no_progress_steps": max((p.get("no_progress_steps", 0) for p in progress), default=0),
        "first_publication_step": switches[0].get("next_response_step") if switches else None,
    }


def checkpoints(database: Path, model: str, requests: list[dict], pricing: dict) -> dict:
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        entries = [
            PositionedMessage(
                position=r["position"],
                run_id=r["run_id"],
                message=ChatMessage.model_validate_json(r["message_json"]),
            )
            for r in db.execute(
                "SELECT position,run_id,message_json FROM messages ORDER BY position"
            )
        ]
        published = [
            dict(r)
            for r in db.execute("SELECT * FROM context_compactions ORDER BY created_at")
            if r["ready_at"]
        ]
        nodes = {
            r["id"]: json.loads(r["content"])
            for r in db.execute(
                "SELECT id,content FROM context_blobs "
                "WHERE media_type='application/vnd.bot.summary-node+json'"
            )
        }
        blob_ids = {r["id"] for r in db.execute("SELECT id FROM context_blobs")}
        response_checks = []
        for request in requests:
            if not request.get("response_ref"):
                continue
            row = db.execute(
                "SELECT content FROM context_blobs WHERE id=?", (request["response_ref"],)
            ).fetchone()
            response = json.loads(row["content"])
            text = response["text"]
            response_checks.append(
                {
                    "response_ref": request["response_ref"],
                    "phase": request["phase"],
                    "status": request["status"],
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "text_token_estimate": TokenEstimator().text(text),
                    "heading_lines": re.findall(r"^## .+$", text, re.MULTILINE),
                    "tool_call_count": len(response.get("tool_calls", [])),
                }
            )
    ids = {r["id"] for r in published}
    users = {e.position for e in entries if e.is_real_user}
    for record in published:
        covered = [e for e in entries if e.position <= record["covered_end_position"]]
        if ContextCompactor._digest(covered) != record["source_sha256"]:
            raise ValueError(f"Published source hash mismatch: {record['id']}")
        if record["parent_id"] is not None and record["parent_id"] not in ids:
            raise ValueError(f"Missing published parent: {record['id']}")
        if not set(json.loads(record["anchor_positions_json"])) <= users:
            raise ValueError(f"Non-user anchor: {record['id']}")
        if record["model"] != model:
            raise ValueError(f"Unexpected compaction model: {record['model']}")
    for key, node in nodes.items():
        covered = [e for e in entries if node["start"] <= e.position <= node["end"]]
        if ContextCompactor._digest(covered) != node["source_digest"]:
            raise ValueError(f"Summary node source hash mismatch: {key}")
    references = {f"compaction:{r['id']}": json.loads(r["source_refs_json"]) for r in published}
    seen = set()

    def visit(key: str) -> None:
        if key in seen:
            return
        seen.add(key)
        for child in references[key] if key in references else nodes[key]["children"]:
            visit(child)

    for key in references:
        visit(key)
    unused = {key: value for key, value in nodes.items() if key not in seen}
    unused_ranges = {(n["start"], n["end"]) for n in unused.values()}
    unused_requests = [
        r
        for r in requests
        if r["phase"] == "b_leaf" and tuple(r.get("source_range") or ()) in unused_ranges
    ]
    if hashlib.sha256(database.read_bytes()).hexdigest() != before:
        raise ValueError("Database changed during read-only audit")
    fields = (
        "id",
        "parent_id",
        "status",
        "covered_start_position",
        "covered_end_position",
        "summary_token_estimate",
        "source_sha256",
        "source_refs_json",
        "duration_ms",
        "created_at",
        "ready_at",
        "superseded_at",
    )
    return {
        "published": [{k: r[k] for k in fields} for r in published],
        "all_source_hashes_verified": True,
        "all_anchors_real_user": True,
        "all_node_source_hashes_verified": True,
        "database_unchanged": True,
        "unsuperseded_ready_count": sum(r["superseded_at"] is None for r in published),
        "summary_nodes": len(nodes),
        "used_summary_nodes": len(set(nodes) & seen),
        "unused_summary_nodes": [
            {"id": key, "start": n["start"], "end": n["end"], "children": n["children"]}
            for key, n in unused.items()
        ],
        "unused_leaf_request_usage": aggregate(unused_requests, pricing),
        "unused_cost_note": "Range-matched leaf attempts; already included in total costs.",
        "saved_response_checks": response_checks,
        "summary_text_checks": [
            {
                "id": r["id"],
                "sha256": hashlib.sha256(r["summary_text"].encode()).hexdigest(),
                "heading_lines": re.findall(r"^## .+$", r["summary_text"], re.MULTILINE),
                "missing_blob_references": sorted(
                    set(re.findall(r"blob:[a-f0-9]{64}", r["summary_text"])) - blob_ids
                ),
            }
            for r in published
        ],
    }


def build_report(base: Path, *, partial: bool = False) -> dict:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((base / "manifest.json").read_text())
    report = audit(base, partial=partial)
    report.update(
        schema_version=1,
        complete=len(report["strategies"]) == len(manifest["order"]),
        manifest=manifest,
        analysis_script=reference(Path(__file__).resolve(), root),
        comparison_scope=(
            "One real task, one repeat per strategy; not a reliable success-rate estimate."
        ),
    )
    for strategy, info in report["strategies"].items():
        trial = (base / info["trial"]).parent
        events = [
            json.loads(line) for line in (trial / "agent/events.jsonl").read_text().splitlines()
        ]
        info["stagnation"] = stagnation(events, info["switches"])
        database = (trial / "agent/trace/state.db").resolve()
        info["database"] = reference(database, root)
        info["checkpoint_checks"] = checkpoints(
            database, manifest["model"], info["requests"], manifest["pricing"]
        )
        source = base / "live" / strategy / "mystery.c"
        info["captured_source"] = reference(source, root) if source.exists() else None
        observation = base / "live" / strategy / "observation.json"
        info["source_observation"] = (
            json.loads(observation.read_text()) if observation.exists() else None
        )
        info["verifier_artifacts"] = [
            reference(p, root) for p in sorted((trial / "verifier").glob("*")) if p.is_file()
        ]
        info["official_test_summary"] = (
            (info.get("artifact_check") or {}).get("results", {}).get("summary")
        )
        info["main_output_peak"] = max(
            (
                r["raw_usage"].get("completion_tokens", 0)
                for r in info["requests"]
                if r["phase"] == "main"
            ),
            default=0,
        )
        info["model_output_limit_events"] = [
            {
                k: e["payload"].get(k)
                for k in ("step", "phase", "finish_reason", "tool_call_count", "turn_usage")
            }
            for e in events
            if e["type"] == "model.response"
            and e["payload"].get("finish_reason") in ("length", "max_tokens")
        ]
        info["invalid_tool_argument_results"] = [
            {k: e["payload"].get(k) for k in ("tool_call_id", "name", "error", "process_id")}
            for e in events
            if e["type"] == "tool.result" and "参数校验失败" in (e["payload"].get("error") or "")
        ]
        errors = [
            r["input_token_error"]
            for r in info["requests"]
            if isinstance(r.get("input_token_error"), int)
        ]
        info["input_estimate_error"] = {
            "count": len(errors),
            "min": min(errors, default=None),
            "max": max(errors, default=None),
        }
        summary_requests = [r for r in info["requests"] if r["phase"] not in ("main", "finalizing")]
        info["summary_usage"] = aggregate(summary_requests, manifest["pricing"])
        info["summary_request_limits"] = dict(
            Counter(str(r.get("max_output_tokens")) for r in summary_requests)
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--partial", action="store_true")
    args = parser.parse_args()
    report = build_report(args.directory.resolve(), partial=args.partial)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                s: {
                    "cost": r["totals"]["normalized_peak_cost_usd"],
                    "published": r["publications"],
                    "reward": r["verifier"],
                    "repeat_count": r["stagnation"]["dominant_command"]["count"],
                }
                for s, r in report["strategies"].items()
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
