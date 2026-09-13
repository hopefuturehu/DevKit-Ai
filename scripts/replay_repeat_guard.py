#!/usr/bin/env python3
"""Read-only replay of repetition decisions; never executes recorded commands."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import types
from collections import Counter
from pathlib import Path
from statistics import median

from bot.config.models import ProgressConfig
from bot.core.progress import ProgressKind, ProgressSignal
from bot.core.termination import ProgressController
from bot.core.termination.identity import call_identity, fingerprint
from bot.core.termination.repetition import RepeatGuard


def benchmark_lookup() -> dict:
    guard = RepeatGuard(ProgressConfig(repeat_guard_mode="enforce"))
    for n in range(128):
        key = call_identity("run_command", {"argv": ["printf", str(n)]}, workspace="/app")
        for _ in range(3):
            guard.observe(
                key,
                fingerprint(str(n)),
                tool_name="run_command",
                eligible=True,
                complete=True,
            )
            guard.finish_step(useful=False)
    samples = []
    for n in range(2200):
        started = time.perf_counter_ns()
        key = call_identity(
            "run_command",
            {"argv": ["printf", "0"], "wait_seconds": 5},
            workspace="/app",
        )
        check = guard.check(key)
        elapsed_us = (time.perf_counter_ns() - started) / 1000
        assert check is not None and check.denied
        if n >= 200:
            samples.append(elapsed_us)
    return {
        "scope": "canonical_identity_and_in_memory_lookup_only",
        "excludes": [
            "file_version_reads",
            "process_scan",
            "checkpoint_serialization",
            "model_calls",
        ],
        "active_records": len(guard.records),
        "samples": len(samples),
        "median_us": median(samples),
        "p95_us": sorted(samples)[int(len(samples) * 0.95)],
        "repeat_checkpoint_bytes": len(json.dumps(guard.snapshot()).encode()),
    }


def legacy_controller(revision: str):
    source = subprocess.check_output(
        ["git", "show", f"{revision}:src/bot/core/termination/controller.py"],
        text=True,
    )
    module = types.ModuleType("_repeat_guard_baseline")
    sys.modules[module.__name__] = module
    exec(compile(source, f"git:{revision}:controller.py", "exec"), module.__dict__)
    return module.ProgressController, hashlib.sha256(source.encode()).hexdigest()


def replay(trace: Path, controller_type, variant: str) -> dict:
    rows_path = trace / "tool-runs.jsonl"
    events_path = trace / "events.jsonl"
    rows = {row["tool_call_id"]: row for row in map(json.loads, rows_path.read_text().splitlines())}
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    controller = controller_type(
        ProgressConfig(
            repeat_guard_mode="enforce" if variant == "enforce" else "observe",
        )
    )
    modern = variant in {"observe", "enforce"}
    decisions = []
    denied = []
    first_finalize = None
    counts: Counter[str] = Counter()
    pids: dict[str, set[str]] = {}
    results: dict[str, set[str]] = {}
    unknown = 0
    step = 1
    for event in events:
        payload = event["payload"]
        if event["type"] == "tool.result":
            row = rows.get(payload["tool_call_id"])
            if row is None or row["tool_name"] not in {"run_command", "run_shell"}:
                unknown += 1
                continue
            result = row["result"]
            if not isinstance(result, dict):
                unknown += 1
                continue
            meta = result.get("metadata", {})
            args = row["arguments"]
            tool = row["tool_name"]
            key = call_identity(tool, args, workspace=meta.get("cwd", "/app"))
            counts[key] += 1
            if meta.get("process_id"):
                pids.setdefault(key, set()).add(meta["process_id"])
            # Historical exports contain combined output, not separate streams.
            # Reconstruct equivalence of complete combined observations only.
            stable = fingerprint(
                "historical-combined",
                result.get("status"),
                meta.get("returncode"),
                result.get("output", ""),
                result.get("error"),
            )
            results.setdefault(key, set()).add(stable)
            if first_finalize is not None:
                continue
            signal_data = result.get("progress")
            signal = ProgressSignal.model_validate(signal_data) if signal_data else None
            complete = (
                result.get("status") != "running"
                and not result.get("truncated", False)
                and signal is not None
                and signal.kind != ProgressKind.WAITING
            )
            if variant != "legacy" and signal is not None and complete:
                signal = signal.model_copy(
                    update={"evidence_key": stable, "evidence_complete": True}
                )
            if modern:
                check = controller.repeat_guard.check(key)
                if check is not None and check.denied:
                    denied.append({"step": step, "call_key": key, "count": check.count})
                    continue
            extra = {"call_signature": key, "repeat_eligible": True} if modern else {}
            controller.observe_tool(
                tool_name=tool,
                arguments=args,
                success=result["success"],
                result_content=result.get("output", ""),
                metadata={**meta, "truncated": result.get("truncated", False)},
                progress_signal=signal,
                read_only=False,
                idempotent=False,
                **extra,
            )
        elif event["type"] == "run.progress" and first_finalize is None:
            report = controller.finish_step()
            current_step = payload.get("step", step)
            if report.action.value != "continue":
                decisions.append(
                    {
                        "step": current_step,
                        "action": report.action.value,
                        "reason": report.reason_code,
                        "no_progress_steps": report.no_progress_steps,
                    }
                )
            if report.action.value == "finalize" and first_finalize is None:
                first_finalize = current_step
            step = current_step + 1
    return {
        "variant": variant,
        "trace": str(trace),
        "events_sha256": hashlib.sha256(events_path.read_bytes()).hexdigest(),
        "tool_runs_sha256": hashlib.sha256(rows_path.read_bytes()).hexdigest(),
        "unknown_or_noncommand_results": unknown,
        "first_finalize_step": first_finalize,
        "first_would_deny": denied[0] if denied else None,
        "decisions": decisions,
        "most_repeated": [
            {
                "call_key": key,
                "historical_executions": count,
                "distinct_process_ids": len(pids.get(key, ())),
                "distinct_combined_observations": len(results[key]),
            }
            for key, count in counts.most_common(3)
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, action="append", required=True)
    parser.add_argument("--baseline", default="1f9053cc48eba26471ab2882aea5e4bdfa1dbc91")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    legacy, legacy_sha = legacy_controller(args.baseline)
    report = {
        "baseline_revision": args.baseline,
        "baseline_controller_sha256": legacy_sha,
        "method": "fixed_trace_command_observations_only",
        "lookup_microbenchmark": benchmark_lookup(),
        "limitations": [
            "Commands are never executed. Decisions stop at first finalization; "
            "historical requests are counted in full.",
            "Non-command results and unavailable results are excluded and counted.",
            "Complete combined output is compared; separate historical stream digests "
            "are unavailable.",
            "This does not measure post-intervention task success or realized cost savings.",
        ],
        "runs": [
            replay(trace, cls, variant)
            for trace in args.trace
            for cls, variant in [
                (legacy, "legacy"),
                (legacy, "stable_evidence_only"),
                (ProgressController, "observe"),
                (ProgressController, "enforce"),
            ]
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            [
                {
                    "variant": row["variant"],
                    "trace": row["trace"],
                    "first_finalize_step": row["first_finalize_step"],
                    "first_would_deny": row["first_would_deny"],
                    "most_repeated": row["most_repeated"][0],
                }
                for row in report["runs"]
            ],
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
