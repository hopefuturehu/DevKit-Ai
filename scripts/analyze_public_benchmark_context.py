#!/usr/bin/env python3
"""Audit context metrics for the stopped 2026-09-09 public benchmark snapshot.

This is a snapshot-specific analysis, not a general benchmark scorer. Read-only
SQLite access and per-response usage reconciliation guard the report's inputs.
Provider tokens are actual usage; text-size estimates count each stored message
once and must not be interpreted as cumulative API savings or an ablation.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path

from summarize_public_benchmarks import usage_metrics

from bot.core.context import TokenEstimator

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts/public-benchmark/20260909-flash"
est = TokenEstimator()


def load_events(path):
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert len({e["id"] for e in events}) == len(events)
    return events


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def stats(records):
    inputs = [r["input"] for r in records]
    hit = sum(r["hit"] for r in records)
    total = sum(inputs)
    ratios = [r["hit"] / r["input"] for r in records]
    return dict(
        responses=len(records),
        input_tokens=total,
        output_tokens=sum(r["output"] for r in records),
        cache_hit_tokens=hit,
        cache_miss_tokens=sum(r["miss"] for r in records),
        token_cache_hit_rate=hit / total if total else None,
        requests_with_any_cache_hit=sum(r["hit"] > 0 for r in records),
        request_cache_rate_median=statistics.median(ratios) if ratios else None,
        input_p50=statistics.median(inputs) if inputs else None,
        input_p95=sorted(inputs)[math.ceil(len(inputs) * 0.95) - 1] if inputs else None,
        input_max=max(inputs, default=None),
        reasoning_output_tokens=sum(r["reasoning"] for r in records),
    )


def analyze(task, attempt, scope, path, official=None):
    events = load_events(path)
    run_ids = {e["run_id"] for e in events if e["type"] == "run.started"}
    assert len(run_ids) == 1, path
    run_id = next(iter(run_ids))
    counts = Counter(e["type"] for e in events)
    records = []
    seen = set()
    current_phase = "running"
    for e in events:
        p = e["payload"]
        kind = e["type"]
        if kind == "run.finalizing":
            current_phase = "finalizing"
        if kind == "model.usage" and p.get("phase") != "compaction":
            raw = p.get("provider_metadata", {}).get("raw_usage") or p.get("turn_usage")
            key = p.get("provider_metadata", {}).get("response_id")
            assert key and key not in seen, path
            seen.add(key)
            phase = p.get("phase", current_phase)
        elif kind == "context.compaction.request.completed":
            raw = p["raw_usage"]
            phase = "compaction"
        else:
            continue
        assert (
            raw["prompt_cache_hit_tokens"] + raw["prompt_cache_miss_tokens"] == raw["prompt_tokens"]
        )
        records.append(
            dict(
                at=e["timestamp"],
                step=p.get("step"),
                phase=phase,
                input=raw["prompt_tokens"],
                output=raw["completion_tokens"],
                hit=raw["prompt_cache_hit_tokens"],
                miss=raw["prompt_cache_miss_tokens"],
                reasoning=raw.get("completion_tokens_details", {}).get("reasoning_tokens", 0),
            )
        )
    aggregate = stats(records)
    reference = usage_metrics(path)
    for k in ("input_tokens", "output_tokens", "cache_hit_tokens"):
        assert aggregate[k] == reference[k], (task, k)
    assert aggregate["responses"] == reference["model_responses_with_usage"]
    terminal = next(e for e in reversed(events) if e["type"] == "run.finished")
    if path.name == "prediction.events.jsonl":
        trace = path.parent / "prediction.trace"
        result_file = path.parent / "prediction.result.json"
    else:
        trace = path.parent / "trace"
        result_file = path.parent / "result.json"
    db = trace / "state.db"
    if not db.exists():
        db = path.parent / "state.db"
    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    messages = [
        {**dict(m), "message": json.loads(m["message_json"])}
        for m in conn.execute("SELECT * FROM messages WHERE run_id=? ORDER BY position", (run_id,))
    ]
    cuts = []
    ordinary = []
    receipts = []
    reasoning = []
    for row in messages:
        m = row["message"]
        content = m.get("content") or ""
        if m.get("reasoning_content"):
            reasoning.append(
                dict(
                    position=row["position"],
                    chars=len(m["reasoning_content"]),
                    estimate=est.text(m["reasoning_content"]),
                    tool_calls=bool(m.get("tool_calls")),
                    later_responses=sum(
                        timestamp(r["at"]) > timestamp(row["created_at"]) for r in records
                    ),
                )
            )
        if m["role"] != "tool":
            continue
        if content.startswith("{"):
            try:
                body = json.loads(content)
            except json.JSONDecodeError:
                body = {}
            if body.get("status") == "disposable_context_delivery":
                receipts.append(dict(position=row["position"], tool=m.get("name"), body=body))
                continue
        matches = re.findall(r"\[完整内容已持久化；context_ref=(blob:[a-f0-9]{64});", content)
        assert matches, (task, row["position"], content[:100])
        ref = matches[-1]
        blob = conn.execute(
            "SELECT content,sha256 FROM context_blobs WHERE id=?", (ref,)
        ).fetchone()
        raw = blob["content"]
        raw = raw.encode() if isinstance(raw, str) else raw
        assert hashlib.sha256(raw).hexdigest() == blob["sha256"]
        source = raw.decode()
        item = dict(
            position=row["position"],
            tool=m.get("name"),
            tool_call_id=m.get("tool_call_id"),
            raw_chars=len(source),
            inline_chars=len(content),
            raw_estimate=est.text(source),
            inline_estimate=est.text(content),
            ref=ref,
            saved_chars=len(source) - len(content),
            saved_estimate=est.text(source) - est.text(content),
        )
        body = content.rsplit("\n\n[完整内容已持久化；context_ref=", 1)[0]
        if body != source:
            matches = list(re.finditer(r"\n… \[(\d+) chars externalized\] …\n", body))
            valid = [
                cut
                for cut in matches
                if int(cut[1]) == len(source) - (len(body) - len(cut[0]))
                and source.startswith(body[: cut.start()])
                and source.endswith(body[cut.end() :])
            ]
            assert len(valid) == 1, (task, row["position"])
            item["omitted_chars"] = int(valid[0][1])
            cuts.append(item)
        ordinary.append(item)
    for receipt in receipts:
        m = next(row["message"] for row in messages if row["position"] == receipt["position"])
        event = next(
            e["payload"]
            for e in events
            if e["type"] == "tool.result" and e["payload"]["tool_call_id"] == m["tool_call_id"]
        )
        blob = conn.execute(
            "SELECT content,sha256 FROM context_blobs WHERE id=?", (event["context_ref"],)
        ).fetchone()
        raw = blob["content"]
        raw = raw.encode() if isinstance(raw, str) else raw
        assert hashlib.sha256(raw).hexdigest() == blob["sha256"]
        delivered = raw.decode()
        body = json.loads(delivered)
        request = next(
            e["payload"]
            for e in events
            if e["type"] == "tool.requested" and e["payload"]["tool_call_id"] == m["tool_call_id"]
        )
        receipt.update(
            request_arguments=request["arguments"],
            delivered_chars=len(delivered),
            delivered_estimate=est.text(delivered),
            source_slice_chars=len(body.get("content", "")),
            receipt_chars=len(m["content"]),
            receipt_estimate=est.text(m["content"]),
        )
    conn.close()
    requested = Counter(e["payload"]["name"] for e in events if e["type"] == "tool.requested")
    tool_results = [e["payload"] for e in events if e["type"] == "tool.result"]
    compactions = []
    for e in events:
        if e["type"] != "context.compaction.completed":
            continue
        at = e["timestamp"]
        p = e["payload"]
        before = [r for r in records if r["at"] < at and r["phase"] == "running"]
        after = [r for r in records if r["at"] > at and r["phase"] == "running"]
        req = next(
            x["payload"]
            for x in events
            if x["type"] == "context.compaction.request.completed"
            and x["payload"]["compaction_id"] == p["compaction_id"]
        )
        consolidation = next(
            x["payload"]
            for x in events
            if x["type"] == "context.consolidated"
            and x["payload"]["compaction_id"] == p["compaction_id"]
        )
        compactions.append(
            dict(
                **p,
                at=at,
                request=req,
                consolidation=consolidation,
                source_char_reduction=1 - p["summary_chars"] / p["source_chars"],
                before=before[-1] if before else None,
                first_after=after[0] if after else None,
                after_running=stats(after),
                after_first_three=stats(after[:3]),
                after_rest=stats(after[3:]),
            )
        )
    result = json.loads(result_file.read_text()) if result_file.exists() else terminal["payload"]
    official_data = json.loads(official.read_text()) if official and official.exists() else {}
    cutoff = (official_data.get("agent_execution") or {}).get("finished_at")
    after_stop = [
        r
        for r in records
        if cutoff and datetime.fromisoformat(r["at"].replace("Z", "+00:00")) > timestamp(cutoff)
    ]

    def totals(items):
        keys = (
            "raw_chars",
            "inline_chars",
            "raw_estimate",
            "inline_estimate",
            "saved_chars",
            "saved_estimate",
        )
        return {"count": len(items), **{k: sum(i[k] for i in items) for k in keys}}

    data = dict(
        task=task,
        attempt=attempt,
        scope=scope,
        events_path=str(path.relative_to(ROOT)),
        events_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        state_path=str(db.relative_to(ROOT)),
        state_sha256=hashlib.sha256(db.read_bytes()).hexdigest(),
        state_wal_sha256=(
            hashlib.sha256(Path(str(db) + "-wal").read_bytes()).hexdigest()
            if Path(str(db) + "-wal").exists()
            else None
        ),
        official_result_path=str(official.relative_to(ROOT)) if official else None,
        official_result_sha256=(
            hashlib.sha256(official.read_bytes()).hexdigest()
            if official and official.exists()
            else None
        ),
        agent_result_sha256=(
            hashlib.sha256(result_file.read_bytes()).hexdigest() if result_file.exists() else None
        ),
        run_id=run_id,
        aggregate=aggregate,
        by_phase={
            p: stats([r for r in records if r["phase"] == p])
            for p in ("running", "finalizing", "compaction")
        },
        context_events={k: v for k, v in counts.items() if k.startswith("context.")},
        routing=[e["payload"] for e in events if e["type"] == "memory.routing.decided"],
        compactions=compactions,
        tool_requests=dict(requested),
        tool_results=len(tool_results),
        tool_results_truncated=sum(bool(r.get("truncated")) for r in tool_results),
        reference_results=[
            {k: r[k] for k in ("tool_call_id", "success", "status", "truncated")}
            for r in tool_results
            if r["name"] == "load_context_reference"
        ],
        externalized=totals(cuts),
        externalized_records=cuts,
        ordinary_tools=totals(ordinary),
        unshortened_tools=totals([i for i in ordinary if i not in cuts]),
        receipts=receipts,
        reasoning={
            "with_tool_calls": sum(r["tool_calls"] for r in reasoning),
            "without_tool_calls": sum(not r["tool_calls"] for r in reasoning),
            "without_tool_calls_with_later_response": sum(
                not r["tool_calls"] and r["later_responses"] > 0 for r in reasoning
            ),
        },
        status=terminal["payload"]["status"],
        steps=terminal["payload"].get("steps"),
        termination_reason=terminal["payload"].get("termination_reason"),
        run_usage_difference={
            k: aggregate[k] - terminal["payload"].get(k, 0)
            for k in ("input_tokens", "output_tokens")
        },
        result_source="result.json" if result_file.exists() else "events.run.finished",
        result_usage_difference={
            k: aggregate[k] - result.get(k, 0) for k in ("input_tokens", "output_tokens")
        },
        after_official_stop=stats(after_stop),
        responses_finish_length=[
            {
                "step": e["payload"].get("step"),
                "phase": e["payload"].get("phase"),
                "usage": e["payload"].get("turn_usage"),
            }
            for e in events
            if e["type"] == "model.response" and e["payload"].get("finish_reason") == "length"
        ],
    )
    # Keep full request series in the ignored artifact, not the concise tracked metrics.
    return data, records


def main():
    summary = json.loads((ART / "summary.json").read_text())
    runs = []
    series = {}
    for task in summary["tasks"]:
        for a in task["attempts"]:
            if not a.get("model_response_observed"):
                continue
            is_main = a["attempt"] == "1" or (
                task["id"] == "terminal-bench/chess-best-move" and a["attempt"] == "3"
            )
            scope = "main18" if is_main else "supplemental"
            official = Path(a["official_result"])
            data, records = analyze(
                task["id"], a["attempt"], scope, Path(a["events_path"]), official
            )
            runs.append(data)
            series[f"{task['id']}:{a['attempt']}"] = records
    p = next(
        ART.glob("terminalbench/large-scale-text-editing-native-baseline-1/*/agent/events.jsonl")
    )
    data, records = analyze(
        "terminal-bench/large-scale-text-editing",
        "native-1",
        "main18",
        p,
        p.parent.parent / "result.json",
    )
    runs.append(data)
    series["terminal-bench/large-scale-text-editing:native-1"] = records
    output = {
        "schema_version": 1,
        "snapshot": "2026-09-09 stopped batch",
        "source_baseline": "578f660",
        "summary_sha256": hashlib.sha256((ART / "summary.json").read_bytes()).hexdigest(),
        "estimator_file_sha256": hashlib.sha256(
            (ROOT / "src/bot/core/context.py").read_bytes()
        ).hexdigest(),
        "measurement_notes": [
            "main18 = original16 + chess attempt3 + large text native-1; "
            "supplemental = 4 repeated/candidate runs",
            "actual provider usage includes finalization and late eigenval responses; "
            "compaction aggregate events are not additional requests",
            "externalization estimates count each ordinary stored tool message once, "
            "include reference footer, exclude one-shot delivery requests and repeated replay",
            "source blobs preserve what tools returned, which may already be truncated by the tool",
            "no full outgoing request capture: semantic retention, counterfactual cumulative "
            "savings and exact replay tokens are not measured",
        ],
        "runs": runs,
        "groups": {},
    }
    for scope in ("original16", "main18", "supplemental", "all22"):
        selected = [
            r
            for r in runs
            if scope == "all22"
            or r["scope"] == scope
            or (scope == "original16" and r["scope"] == "main18" and r["attempt"] == "1")
        ]
        flat = [v for r in selected for v in series[f"{r['task']}:{r['attempt']}"]]
        totals = stats(flat)
        totals["run_count"] = len(selected)
        totals["by_phase"] = {
            p: stats([r for r in flat if r["phase"] == p])
            for p in ("running", "finalizing", "compaction")
        }
        totals["macro_task_cache_rate"] = statistics.mean(
            r["aggregate"]["token_cache_hit_rate"] for r in selected
        )
        for k in ("externalized", "ordinary_tools", "unshortened_tools"):
            totals[k] = {key: sum(r[k][key] for r in selected) for key in selected[0][k]}
        totals["context_events"] = dict(
            sum((Counter(r["context_events"]) for r in selected), Counter())
        )
        totals["tool_requests"] = dict(
            sum((Counter(r["tool_requests"]) for r in selected), Counter())
        )
        totals["tool_results"] = sum(r["tool_results"] for r in selected)
        totals["tool_results_truncated"] = sum(r["tool_results_truncated"] for r in selected)
        totals["reference_receipts"] = sum(len(r["receipts"]) for r in selected)
        totals["tasks_with_externalization"] = sum(r["externalized"]["count"] > 0 for r in selected)
        totals["output_limit_runs"] = sum(bool(r["responses_finish_length"]) for r in selected)
        output["groups"][scope] = totals
    assert output["groups"]["main18"]["run_count"] == 18
    assert len({r["task"] for r in runs if r["scope"] == "main18"}) == 18
    assert output["groups"]["all22"]["run_count"] == 22
    assert output["groups"]["original16"]["run_count"] == 16
    for run in runs:
        expected = (
            {"input_tokens": 142579, "output_tokens": 8075}
            if run["task"] == "terminal-bench/bn-fit-modify"
            else {"input_tokens": 0, "output_tokens": 0}
        )
        assert run["result_usage_difference"] == expected, run["task"]
        assert run["run_usage_difference"] == expected, run["task"]
    out = ART / "diagnostics/context-analysis"
    out.mkdir(exist_ok=True)
    (out / "request-series.json").write_text(
        json.dumps(series, ensure_ascii=False, indent=2) + "\n"
    )
    (ROOT / "docs/data/public-benchmark-context-metrics.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(output["groups"]["main18"], ensure_ascii=False, indent=2))
    print("\nTASKS")
    for r in runs:
        a = r["aggregate"]
        x = r["externalized"]
        print(
            r["scope"],
            r["task"].split("/")[-1],
            r["attempt"],
            a["responses"],
            a["input_tokens"],
            round(a["token_cache_hit_rate"] * 100, 2),
            a["input_max"],
            x["count"],
            x["raw_chars"],
            x["inline_chars"],
            len(r["receipts"]),
        )


if __name__ == "__main__":
    main()
