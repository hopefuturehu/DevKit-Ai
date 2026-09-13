#!/usr/bin/env python3
"""Combine audited repetitions without averaging cache percentages or hiding failed runs."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from statistics import median

from bot.evals.context_strategy_audit import aggregate


def distribution(values: list[int | float]) -> dict:
    return {
        "count": len(values),
        "median": median(values) if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def comparable(manifest: dict) -> dict:
    fields = (
        "revision",
        "task_files",
        "model",
        "main_max_output_tokens",
        "summary_max_output_tokens",
        "max_steps",
        "worker_wall_seconds",
        "max_cost_usd_conservative_per_run",
        "pricing",
        "concurrency",
        "model_host",
    )
    return {
        **{key: manifest[key] for key in fields},
        "assets": {key: manifest[key]["sha256"] for key in ("wheel", "tokenizer")},
        "configs": {key: asset["sha256"] for key, asset in manifest["configs"].items()},
    }


def succeeded(info: dict) -> bool:
    official = info["official_test_summary"] or {}
    reward = ((info["verifier"] or {}).get("rewards") or {}).get("reward")
    return (
        info["exception"] is None
        and reward == 1.0
        and official.get("tests", 0) > 0
        and official.get("passed") == official["tests"]
    )


def combined_usage(infos: list[dict], pricing: dict, *, summaries: bool = False) -> dict:
    rows = [
        row
        for info in infos
        for row in info["requests"]
        if not summaries or row["phase"] not in ("main", "finalizing")
    ]
    result = aggregate(rows, pricing)
    key = "summary_usage" if summaries else "totals"
    result["cost_is_lower_bound"] |= any(info[key]["cost_is_lower_bound"] for info in infos)
    return result


def group(infos: list[dict], pricing: dict) -> dict:
    successes = [info for info in infos if succeeded(info)]
    total_usage = combined_usage(infos, pricing)
    switches = [switch for info in infos for switch in info["switches"]]
    summary_rows = [
        row
        for info in infos
        for row in info["requests"]
        if row["phase"] not in ("main", "finalizing")
    ]
    next_inputs = [s["next_main_usage"]["prompt_tokens"] for s in switches if s["next_main_usage"]]
    release = [
        s["publication"]["net_release_tokens"] / s["publication"]["before_tokens"]
        for s in switches
        if s["publication"].get("before_tokens")
    ]
    return {
        "trials": len(infos),
        "official_successes": len(successes),
        "infrastructure_exceptions": sum(info["exception"] is not None for info in infos),
        "normal_completions": sum(
            (info["result"] or {}).get("status") == "completed"
            and (info["result"] or {}).get("termination_reason") is None
            for info in infos
        ),
        "termination_reasons": dict(
            Counter(
                (info["result"] or {}).get("termination_reason")
                or (
                    "normal_completion"
                    if (info["result"] or {}).get("status") == "completed"
                    else "unknown"
                )
                for info in infos
            )
        ),
        "totals": total_usage,
        "observed_total_cost_per_success_usd": (
            total_usage["normalized_peak_cost_usd"] / len(successes) if successes else None
        ),
        "summary_usage": combined_usage(infos, pricing, summaries=True),
        "summary_by_phase": {
            phase: aggregate([row for row in summary_rows if row["phase"] == phase], pricing)
            for phase in sorted({row["phase"] for row in summary_rows})
        },
        "cost_usd": distribution([info["totals"]["normalized_peak_cost_usd"] for info in infos]),
        "cost_distribution_contains_lower_bounds": any(
            info["totals"]["cost_is_lower_bound"] for info in infos
        ),
        "successful_run_cost_usd": distribution(
            [info["totals"]["normalized_peak_cost_usd"] for info in successes]
        ),
        "agent_minutes": distribution(
            [info["agent_seconds"] / 60 for info in infos if info["agent_seconds"] is not None]
        ),
        "trial_minutes": distribution(
            [info["wall_seconds"] / 60 for info in infos if info["wall_seconds"] is not None]
        ),
        "steps": distribution([(info["result"] or {}).get("steps", 0) for info in infos]),
        "publications": sum(info["publications"] for info in infos),
        "compaction_triggers": sum(info["compaction_triggers"] for info in infos),
        "compaction_failures": sum(len(info["compaction_failures"]) for info in infos),
        "summary_requests_by_phase_and_status": dict(
            Counter(f"{r['phase']}:{r['status']}" for r in summary_rows)
        ),
        "publication_paths": dict(
            Counter(
                s["publication"].get("adopted_path") or s["publication"].get("strategy", "current")
                for s in switches
            )
        ),
        "first_continuation_completed": sum(s["first_continuation_completed"] for s in switches),
        "post_publication_input_tokens": distribution(next_inputs),
        "net_release_rate_same_budget": distribution(release),
        "runs_with_output_limit_response": sum(
            bool(info["model_output_limit_events"]) for info in infos
        ),
        "dominant_identical_command_count": distribution(
            [info["stagnation"]["dominant_command"]["count"] for info in infos]
        ),
    }


def combine(reports: list[dict], *, replacements: list[dict] | None = None) -> dict:
    if not reports:
        raise ValueError("At least one report is required")
    reference = comparable(reports[0]["manifest"])
    seen = set()
    for report in reports:
        if not report["complete"]:
            raise ValueError("Incomplete report; audit all planned trials before combining")
        if comparable(report["manifest"]) != reference:
            raise ValueError("Reports differ in frozen runtime or experiment controls")
        index = report["manifest"].get("repetition_index", 1)
        if index in seen:
            raise ValueError(f"Duplicate repetition index: {index}")
        seen.add(index)
        if set(report["strategies"]) != set(reference["configs"]):
            raise ValueError("Strategy coverage differs")
    originals = {r["manifest"].get("repetition_index", 1): r for r in reports}
    replacement_map = {}
    excluded = []
    for replacement in replacements or []:
        manifest = replacement["manifest"]
        index = manifest.get("repetition_index", 1)
        if (
            not replacement["complete"]
            or comparable(manifest) != reference
            or index not in originals
            or not manifest.get("replacement_reason")
            or manifest.get("replacement_for_manifest_sha256")
            != originals[index]["manifest_sha256"]
        ):
            raise ValueError(
                "Replacement must identify a same-control original and exclusion reason"
            )
        for strategy in replacement["strategies"]:
            key = (index, strategy)
            if key in replacement_map or strategy not in originals[index]["strategies"]:
                raise ValueError("Duplicate or unknown replacement target")
            replacement_map[key] = replacement
            original = originals[index]["strategies"][strategy]
            excluded.append(
                {
                    "strategy": strategy,
                    "repetition_index": index,
                    "reason": manifest["replacement_reason"],
                    "original_manifest_sha256": originals[index]["manifest_sha256"],
                    "replacement_manifest_sha256": replacement["manifest_sha256"],
                    "official_success": succeeded(original),
                    **{
                        field: original[field]
                        for field in (
                            "official_test_summary",
                            "totals",
                            "agent_seconds",
                            "wall_seconds",
                            "database",
                            "events_sha256",
                            "stagnation",
                        )
                    },
                    "termination_reason": (original["result"] or {}).get("termination_reason"),
                }
            )
    pricing = reports[0]["pricing"]
    result = {
        "schema_version": 1,
        "frozen_controls": reference,
        "strategies": {},
        "runs": [],
        "excluded_runs": excluded,
    }
    for strategy in reference["configs"]:
        selected = [
            replacement_map.get((report["manifest"].get("repetition_index", 1), strategy), report)
            for report in reports
        ]
        infos = [report["strategies"][strategy] for report in selected]
        new_infos = [
            report["strategies"][strategy]
            for report in selected
            if report["manifest"].get("repetition_index", 1) > 1
        ]
        result["strategies"][strategy] = {
            "all_trials": group(infos, pricing),
            "follow_up_only": group(new_infos, pricing),
        }
        for report, info in zip(selected, infos, strict=True):
            result["runs"].append(
                {
                    "strategy": strategy,
                    "repetition_index": report["manifest"].get("repetition_index", 1),
                    "manifest_sha256": report["manifest_sha256"],
                    "events_sha256": info["events_sha256"],
                    "database": info["database"],
                    "official_success": succeeded(info),
                    "official_test_summary": info["official_test_summary"],
                    "exception": info["exception"],
                    "status": (info["result"] or {}).get("status"),
                    "termination_reason": (info["result"] or {}).get("termination_reason"),
                    "steps": (info["result"] or {}).get("steps"),
                    "agent_minutes": info["agent_seconds"] / 60
                    if info["agent_seconds"] is not None
                    else None,
                    "trial_minutes": info["wall_seconds"] / 60
                    if info["wall_seconds"] is not None
                    else None,
                    "totals": info["totals"],
                    "summary_usage": info["summary_usage"],
                    "publications": info["publications"],
                    "compaction_triggers": info["compaction_triggers"],
                    "compaction_failures": info["compaction_failures"],
                    "summary_request_failures": [
                        {
                            key: row.get(key)
                            for key in ("phase", "status", "error", "finish_reason", "source_range")
                        }
                        for row in info["requests"]
                        if row["phase"] not in ("main", "finalizing")
                        and row["status"] != "completed"
                    ],
                    "stagnation": info["stagnation"],
                    "main_output_peak": info["main_output_peak"],
                    "model_output_limit_events": info["model_output_limit_events"],
                    "captured_source": info["captured_source"],
                }
            )
    result["notes"] = [
        "Official task success is primary; low cost of failed early stops is not a win.",
        "Cache rate is sum(cache_hit_tokens) / sum(prompt_tokens), not a mean of rates.",
        "Unpriced requests remain unknown; cost distributions of lower bounds are flagged.",
        "Pilot and follow-up are shown separately; one repeated task is not task generalization.",
        "Total cost / observed successes includes failed runs; it is not an expected future cost.",
        "Environmental exclusions remain in excluded_runs; replacement controls are checked.",
    ]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replacement", action="append", type=Path, default=[])
    args = parser.parse_args()
    result = combine(
        [json.loads(path.read_text()) for path in args.reports],
        replacements=[json.loads(path.read_text()) for path in args.replacement],
    )
    result["source_reports"] = [
        {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in args.reports
    ]
    result["replacement_reports"] = [
        {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in args.replacement
    ]
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result["strategies"], ensure_ascii=False))


if __name__ == "__main__":
    main()
