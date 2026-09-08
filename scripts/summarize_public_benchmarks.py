#!/usr/bin/env python3
"""Summarize official results and actual model usage without inventing causal attribution."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def usage_metrics(path: Path) -> dict:
    metrics = {
        "model_responses_with_usage": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_hit_tokens": 0,
        "estimated_cost_usd": 0.0,
        "model_response_observed": False,
        "agent_started": False,
        "compaction_aggregate_fallbacks": 0,
    }
    events = Counter()
    seen = set()
    usage_records = []
    detailed_compactions = set()
    compaction_aggregates = {}
    incomplete_usage = False
    if not path.exists():
        return {**metrics, "estimated_cost_usd": None, "events_available": False}
    with path.open() as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # A live writer can leave one unfinished final line.
            kind = event["type"]
            events[kind] += 1
            payload = event["payload"]
            if kind in {"model.response", "assistant.delta", "assistant.reasoning.delta"}:
                metrics["model_response_observed"] = True
            if kind == "context.compaction.request.completed":
                compaction_id = payload["compaction_id"]
                identity = (
                    "compaction",
                    compaction_id,
                    payload.get("request_sequence", event.get("id", event["timestamp"])),
                )
                if identity in seen:
                    continue
                seen.add(identity)
                detailed_compactions.add(compaction_id)
                raw = payload.get("raw_usage") or {
                    "prompt_tokens": payload.get("input_tokens"),
                    "completion_tokens": payload.get("output_tokens"),
                }
                usage_records.append((raw, event["timestamp"], True))
            elif kind == "model.usage":
                if payload.get("phase") == "compaction":
                    # This event includes cumulative run totals, not another API response.
                    compaction_aggregates[payload["compaction_id"]] = event
                    continue
                metadata = payload.get("provider_metadata", {})
                response_id = metadata.get("response_id")
                identity = ("agent", response_id)
                if response_id and identity in seen:
                    continue
                if response_id:
                    seen.add(identity)
                metrics["model_response_observed"] = True
                raw = metadata.get("raw_usage") or payload.get("turn_usage") or {}
                usage_records.append((raw, event["timestamp"], True))
    for compaction_id, event in compaction_aggregates.items():
        if compaction_id in detailed_compactions:
            continue
        usage = event["payload"].get("compaction_usage", {})
        raw = {
            "prompt_tokens": usage.get("input_tokens"),
            "completion_tokens": usage.get("output_tokens"),
        }
        # Older aggregates preserve totals but cannot prove request count/cache pricing.
        metrics["compaction_aggregate_fallbacks"] += 1
        usage_records.append((raw, event["timestamp"], False))
    for raw, at, individual_response in usage_records:
        inputs = raw.get("prompt_tokens")
        outputs = raw.get("completion_tokens")
        hit = raw.get("prompt_cache_hit_tokens")
        miss = raw.get("prompt_cache_miss_tokens")
        metrics["model_responses_with_usage"] += int(individual_response)
        metrics["input_tokens"] += inputs or 0
        metrics["output_tokens"] += outputs or 0
        metrics["cache_hit_tokens"] += hit or 0
        if any(value is None for value in (inputs, outputs, hit, miss)):
            incomplete_usage = True
            continue
        timestamp = datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(UTC)
        peak = timestamp.weekday() < 5 and (1 <= timestamp.hour < 4 or 6 <= timestamp.hour < 10)
        factor = 1.0 if peak else 0.5
        metrics["estimated_cost_usd"] += factor * (hit * 0.014 + miss * 0.44 + outputs * 1.32) / 1e6
    if incomplete_usage or metrics["model_responses_with_usage"] == 0:
        metrics["estimated_cost_usd"] = None
    metrics["events"] = dict(events)
    metrics["events_available"] = True
    metrics["agent_started"] = events["run.started"] > 0
    return metrics


def summarize(root: Path, suite: dict) -> dict:
    rows = []
    for kind in ("terminalbench", "swebench"):
        for task in suite[kind]:
            short = task["id"].split("/")[-1]
            row = {"id": task["id"], "benchmark": kind, "attempts": []}
            records = root / "cases" / kind / short
            if kind == "terminalbench":
                for job in sorted((root / "terminalbench").glob(f"{short}-baseline-*")):
                    for trial in sorted(p for p in job.iterdir() if p.is_dir()):
                        result = trial / "result.json"
                        events_path = trial / "agent/events.jsonl"
                        agent_result = trial / "agent/result.json"
                        if not any(p.exists() for p in (result, events_path, agent_result)):
                            continue
                        official = load(result) if result.exists() else {}
                        agent = load(agent_result) if agent_result.exists() else {}
                        usage = usage_metrics(events_path)
                        reward = (
                            (official.get("verifier_result") or {}).get("rewards", {}).get("reward")
                        )
                        verdict = (
                            "running"
                            if not official.get("finished_at")
                            else "error"
                            if official.get("exception_info") or reward is None
                            else "pass"
                            if reward == 1
                            else "fail"
                        )
                        row["attempts"].append(
                            {
                                "attempt": job.name.rsplit("-", 1)[-1],
                                "verdict": verdict,
                                "reward": reward,
                                "run_status": agent.get("status"),
                                "agent_result_available": agent_result.exists(),
                                "model_response_observed": usage["model_response_observed"],
                                "steps": agent.get("steps"),
                                "error": agent.get("error"),
                                "exception": official.get("exception_info"),
                                "official_result": str(result),
                                "events_path": str(events_path),
                                "usage": usage,
                            }
                        )
            else:
                for folder in sorted(records.glob("*")):
                    record_path = folder / "result.json"
                    result_path = folder / "prediction.result.json"
                    events_path = folder / "prediction.events.jsonl"
                    if not events_path.exists() and not result_path.exists():
                        continue
                    record = load(record_path) if record_path.exists() else {}
                    agent = load(result_path) if result_path.exists() else {}
                    usage = usage_metrics(events_path)
                    row["attempts"].append(
                        {
                            "attempt": folder.name,
                            "verdict": record.get("verdict", "running"),
                            "run_status": agent.get("status"),
                            "agent_result_available": result_path.exists(),
                            "model_response_observed": usage["model_response_observed"],
                            "steps": agent.get("steps"),
                            "error": agent.get("error"),
                            "official_result": str(record_path),
                            "events_path": str(events_path),
                            "usage": usage,
                        }
                    )
            row["environment_attempts"] = [
                {
                    "attempt": p.parent.name,
                    "verdict": load(p).get("verdict"),
                    "error": load(p).get("error"),
                    "record": str(p),
                }
                for p in sorted(records.glob("*/result.json"))
            ]
            # Keep first baseline and later experiments separate; never replace failures by reruns.
            row["attempts"].sort(
                key=lambda attempt: (
                    int(attempt["attempt"]) if attempt["attempt"].isdigit() else float("inf"),
                    attempt["attempt"],
                )
            )
            baseline = next(
                (attempt for attempt in row["attempts"] if attempt["attempt"] == "1"), None
            )
            row["baseline_verdict"] = baseline["verdict"] if baseline else "not_run"
            row["attribution"] = (
                "not_applicable" if row["baseline_verdict"] == "pass" else "needs_review"
            )
            rows.append(row)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "model": "deepseek-v4-flash",
        "suite_size": len(rows),
        "baseline_counts": dict(Counter(r["baseline_verdict"] for r in rows)),
        "pricing": {
            "source": "https://api-docs.deepseek.com/quick_start/pricing/",
            "observed_on": "2026-09-09",
            "kind": "timestamp/cache-aware estimate, not invoice",
        },
        "tasks": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    args = parser.parse_args()
    report = summarize(args.run_root.resolve(), load(args.suite))
    output = args.run_root / "summary.json"
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(output)
    print(
        json.dumps(
            {"suite_size": report["suite_size"], "baseline_counts": report["baseline_counts"]}
        )
    )


if __name__ == "__main__":
    main()
