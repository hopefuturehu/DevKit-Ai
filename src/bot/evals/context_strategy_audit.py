"""Read-only accounting for real AgentRunner CURRENT/A/B Harbor trials."""

from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def moment(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def is_peak(value: str) -> bool:
    at = moment(value)
    return at.weekday() < 5 and (1 <= at.hour < 4 or 6 <= at.hour < 10)


def request_rows(events: list[dict]) -> list[dict]:
    rows = []
    pending = {}
    for event in events:
        kind, payload = event["type"], event["payload"]
        if kind.startswith("context.compaction.request."):
            key = (
                payload.get("compaction_id"),
                payload.get("phase"),
                payload.get("range_attempt"),
                payload.get("request_attempt"),
                payload.get("request_sequence"),
            )
            if kind.endswith(".started"):
                pending[key] = event
                continue
            start = pending.pop(key, None)
            raw = payload.get("raw_usage") or {}
            phase = payload.get("phase", "summary")
            status = kind.rsplit(".", 1)[-1]
        elif kind == "model.usage" and payload.get("phase") != "compaction":
            start = None
            raw = payload.get("provider_metadata", {}).get("raw_usage") or {}
            phase = payload.get("phase", "main")
            status = "usage_received"
        elif kind == "model.request.retry":
            start = None
            raw = {}
            phase = "main"
            status = "retry_without_usage"
        else:
            continue
        rows.append(
            {
                "event_id": event["id"],
                "timestamp": event["timestamp"],
                "started_at": start["timestamp"] if start else None,
                "phase": phase,
                "status": status,
                "step": payload.get("step"),
                "duration_ms": payload.get("duration_ms"),
                "raw_usage": raw,
                "error": payload.get("error"),
                "source_range": payload.get("source_range"),
                "input_token_error": payload.get(
                    "input_token_error",
                    payload.get("provider_metadata", {}).get("input_token_error"),
                ),
                "response_id": payload.get("response_id")
                or payload.get("provider_metadata", {}).get("response_id"),
                "input_token_estimate": payload.get("input_token_estimate")
                or payload.get("provider_metadata", {}).get("input_token_estimate"),
            }
        )
        if start and payload.get("duration_ms") is not None:
            elapsed = (moment(event["timestamp"]) - moment(start["timestamp"])).total_seconds()
            rows[-1]["utc_duration_ms"] = elapsed * 1000
            rows[-1]["clock_gap_ms"] = elapsed * 1000 - payload["duration_ms"]
    for event in pending.values():
        rows.append(
            {
                "event_id": event["id"],
                "timestamp": event["timestamp"],
                "phase": event["payload"].get("phase", "summary"),
                "status": "no_terminal_event",
                "raw_usage": {},
            }
        )
    return rows


def aggregate(rows: list[dict], pricing: dict) -> dict:
    totals: dict[str, Any] = dict.fromkeys(("input", "hit", "miss", "output"), 0)
    totals.update(
        requests=len(rows),
        usage_complete=0,
        usage_missing=0,
        normalized_peak_cost_usd=0.0,
        timestamp_priced_cost_usd=0.0,
    )
    rates = pricing["usd_per_million_peak"]
    for row in rows:
        raw = row["raw_usage"]
        fields = (
            "prompt_tokens",
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
            "completion_tokens",
        )
        if not all(isinstance(raw.get(k), int) and raw[k] >= 0 for k in fields):
            totals["usage_missing"] += 1
            continue
        inp, hit, miss, out = (raw[k] for k in fields)
        if hit + miss != inp:
            raise ValueError("Provider input cache buckets do not sum to prompt_tokens")
        totals["usage_complete"] += 1
        for key, value in zip(
            ("input", "hit", "miss", "output"), (inp, hit, miss, out), strict=True
        ):
            totals[key] += value
        cost = (hit * rates["hit"] + miss * rates["miss"] + out * rates["output"]) / 1e6
        multiplier = 1 if is_peak(row["timestamp"]) else pricing["off_peak_multiplier"]
        totals["normalized_peak_cost_usd"] += cost
        totals["timestamp_priced_cost_usd"] += cost * multiplier
    totals["cache_hit_rate"] = totals["hit"] / totals["input"] if totals["input"] else None
    totals["usage_coverage"] = totals["usage_complete"] / len(rows) if rows else None
    totals["cost_is_lower_bound"] = bool(totals["usage_missing"])
    return totals


def duration(record: dict) -> float | None:
    if not record.get("started_at") or not record.get("finished_at"):
        return None
    return (moment(record["finished_at"]) - moment(record["started_at"])).total_seconds()


def analyze_events(events: list[dict], pricing: dict) -> dict:
    rows = request_rows(events)
    kinds = Counter(e["type"] for e in events)
    totals = aggregate(rows, pricing)
    # A cancelled main stream has no terminal-usage event to count as a row.
    # Keep the measured subtotal, and expose this accounting uncertainty.
    totals["interruption_or_retry_may_hide_usage"] = bool(
        kinds["run.cancelled"]
        or kinds["model.request.retry"]
        or any(
            e["payload"].get("termination_reason") == "max_wall_time_seconds"
            or e["payload"].get("finalization_error")
            for e in events
        )
    )
    totals["cost_is_lower_bound"] |= totals["interruption_or_retry_may_hide_usage"]
    phases = sorted({r["phase"] for r in rows})
    main = [r for r in rows if r["phase"] == "main" and r["raw_usage"]]
    switches = []
    for index, event in enumerate(events):
        if event["type"] != "context.compaction.completed":
            continue
        later = events[index + 1 :]
        response = next(
            (
                e
                for e in later
                if e["type"] == "model.response" and e["payload"].get("phase") != "finalizing"
            ),
            None,
        )
        usage = next((r for r in main if moment(r["timestamp"]) > moment(event["timestamp"])), None)
        previous = [r for r in main if moment(r["timestamp"]) < moment(event["timestamp"])]
        tool_results = []
        if response:
            response_index = later.index(response)
            for following in later[response_index + 1 :]:
                if following["type"] == "model.response":
                    break
                if following["type"] == "tool.result":
                    tool_results.append(following)
        expected_tools = response["payload"].get("tool_call_count", 0) if response else 0
        switches.append(
            {
                "timestamp": event["timestamp"],
                "publication": event["payload"],
                "prior_main_usage": previous[-1]["raw_usage"] if previous else None,
                "next_main_usage": usage["raw_usage"] if usage else None,
                "next_response_step": response["payload"].get("step") if response else None,
                "first_continuation_completed": bool(
                    response
                    and response["payload"].get("finish_reason") in {"stop", "tool_calls"}
                    and not response["payload"].get("empty")
                    and len(tool_results) >= expected_tools
                ),
            }
        )
    return {
        "totals": totals,
        "by_phase": {
            phase: aggregate([r for r in rows if r["phase"] == phase], pricing) for phase in phases
        },
        "main_input_peak": max((r["raw_usage"].get("prompt_tokens", 0) for r in main), default=0),
        "publications": len(switches),
        "compaction_triggers": kinds["context.compaction.started"],
        "compaction_failures": [
            e["payload"] for e in events if e["type"] == "context.compaction.failed"
        ],
        "background_failures": [
            e["payload"]
            for e in events
            if e["type"] == "context.compaction.blocked"
            and e["payload"].get("phase") == "background"
        ],
        "retry_events": [e["payload"] for e in events if e["type"] == "model.request.retry"],
        "retry_cost_note": (
            "Transport retries without raw usage can add unknown charges beyond measured totals."
        ),
        "switches": switches,
        "event_counts": dict(kinds),
        "requests": rows,
        "clock_discrepancies": [
            {
                k: r.get(k)
                for k in ("event_id", "phase", "duration_ms", "utc_duration_ms", "clock_gap_ms")
            }
            for r in rows
            if abs(r.get("clock_gap_ms", 0)) > 5000
        ],
    }


def verify_manifest(manifest: dict) -> dict:
    assets = [manifest["wheel"], manifest["tokenizer"], *manifest["configs"].values()]
    for asset in assets:
        if hashlib.sha256(Path(asset["path"]).read_bytes()).hexdigest() != asset["sha256"]:
            raise ValueError(f"Frozen asset hash mismatch: {asset['path']}")
    task = Path(manifest["task"])
    for name, expected in manifest["task_files"].items():
        if hashlib.sha256((task / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Frozen task hash mismatch: {name}")
    baseline = None
    for strategy, asset in manifest["configs"].items():
        config = tomllib.loads(Path(asset["path"]).read_text())
        if config["context"].pop("compaction_strategy") != strategy:
            raise ValueError("Strategy/config mismatch")
        if baseline is not None and config != baseline:
            raise ValueError("Comparison configurations differ beyond compaction_strategy")
        baseline = config
    return {
        "asset_hashes": len(assets),
        "task_hashes": len(manifest["task_files"]),
        "only_strategy_differs": True,
    }


def audit(directory: Path, *, partial: bool = False) -> dict:
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    report = {
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "revision": manifest["revision"],
        "pricing": manifest["pricing"],
        "limits": manifest["limits"],
        "frozen_inputs_verified": verify_manifest(manifest),
        "strategies": {},
    }
    for strategy in manifest["order"]:
        trials = list((directory / "jobs" / strategy).glob("*/result.json"))
        if not trials:
            if partial:
                continue
            raise ValueError(f"No completed Harbor trial for {strategy}")
        if len(trials) != 1:
            raise ValueError(f"Expected one trial, found {len(trials)} for {strategy}")
        trial_path = trials[0]
        trial = json.loads(trial_path.read_text())
        events_path = trial_path.parent / "agent" / "events.jsonl"
        events = (
            [json.loads(line) for line in events_path.read_text().splitlines()]
            if events_path.exists()
            else []
        )
        result_path = trial_path.parent / "agent" / "result.json"
        result = json.loads(result_path.read_text()) if result_path.exists() else None
        info = analyze_events(events, manifest["pricing"])
        info.update(
            trial=str(trial_path.relative_to(directory)),
            result=result,
            verifier=trial.get("verifier_result"),
            exception=trial.get("exception_info"),
            wall_seconds=duration(trial),
            agent_seconds=duration(trial.get("agent_execution") or {}),
            events_sha256=hashlib.sha256(events_path.read_bytes()).hexdigest()
            if events_path.exists()
            else None,
            artifact_check=(
                json.loads((trial_path.parent / "verifier" / "ctrf.json").read_text())
                if (trial_path.parent / "verifier" / "ctrf.json").exists()
                else None
            ),
        )
        report["strategies"][strategy] = info
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--partial", action="store_true")
    args = parser.parse_args()
    result = audit(args.directory.resolve(), partial=args.partial)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                s: {
                    "usage": r["totals"],
                    "publications": r["publications"],
                    "verifier": r["verifier"],
                }
                for s, r in result["strategies"].items()
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
