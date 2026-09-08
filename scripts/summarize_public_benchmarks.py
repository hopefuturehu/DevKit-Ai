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
    }
    events = Counter()
    seen = set()
    incomplete_usage = False
    if not path.exists():
        return {"estimated_cost_usd": None, "events_available": False}
    for line in path.open():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # A live writer can leave one unfinished final line.
        events[event["type"]] += 1
        if event["type"] != "model.usage":
            continue
        payload = event["payload"]
        metadata = payload.get("provider_metadata", {})
        response_id = metadata.get("response_id")
        if response_id and response_id in seen:
            continue
        if response_id:
            seen.add(response_id)
        raw = metadata.get("raw_usage") or payload.get("turn_usage") or {}
        inputs = raw.get("prompt_tokens")
        outputs = raw.get("completion_tokens")
        hit = raw.get("prompt_cache_hit_tokens")
        miss = raw.get("prompt_cache_miss_tokens")
        metrics["model_responses_with_usage"] += 1
        metrics["input_tokens"] += inputs or 0
        metrics["output_tokens"] += outputs or 0
        metrics["cache_hit_tokens"] += hit or 0
        if any(value is None for value in (inputs, outputs, hit, miss)):
            incomplete_usage = True
            continue
        timestamp = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).astimezone(
            UTC
        )
        peak = timestamp.weekday() < 5 and (1 <= timestamp.hour < 4 or 6 <= timestamp.hour < 10)
        factor = 1.0 if peak else 0.5
        metrics["estimated_cost_usd"] += factor * (hit * 0.014 + miss * 0.44 + outputs * 1.32) / 1e6
    if incomplete_usage or metrics["model_responses_with_usage"] == 0:
        metrics["estimated_cost_usd"] = None
    metrics["events"] = dict(events)
    metrics["events_available"] = True
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
                    for result in sorted(job.glob("*/result.json")):
                        official = load(result)
                        if not official.get("finished_at"):
                            continue
                        agent_result = result.parent / "agent/result.json"
                        agent = load(agent_result) if agent_result.exists() else {}
                        reward = (
                            (official.get("verifier_result") or {}).get("rewards", {}).get("reward")
                        )
                        verdict = (
                            "error"
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
                                "steps": agent.get("steps"),
                                "error": agent.get("error"),
                                "exception": official.get("exception_info"),
                                "official_result": str(result),
                                "events_path": str(result.parent / "agent/events.jsonl"),
                                "usage": usage_metrics(result.parent / "agent/events.jsonl"),
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
                    row["attempts"].append(
                        {
                            "attempt": folder.name,
                            "verdict": record.get("verdict", "running"),
                            "run_status": agent.get("status"),
                            "steps": agent.get("steps"),
                            "error": agent.get("error"),
                            "official_result": str(record_path),
                            "events_path": str(events_path),
                            "usage": usage_metrics(events_path),
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
            row["baseline_verdict"] = (
                row["attempts"][0]["verdict"] if row["attempts"] else "not_run"
            )
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
