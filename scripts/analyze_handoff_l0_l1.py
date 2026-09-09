#!/usr/bin/env python3
"""Read-only audit of completed L0/L1 artifacts; export compact quantitative evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from collections import Counter
from pathlib import Path

from bot.compaction.handoff import protocol_closed
from bot.core.context import TokenEstimator
from bot.core.models import ChatMessage, ModelRequest
from bot.evals.handoff_recording import aggregate_requests as recorded_aggregate
from bot.evals.handoff_recording import usage_cost


def aggregate_requests(rows):
    """Keep unpriced failures visible beside sums of measured usage."""
    result = recorded_aggregate(rows)
    result["unknown_budget_reserve_usd"] = sum(
        row["budget_charge_usd"] for row in rows if not row["usage_complete"]
    )
    result["budget_reserved_usd"] = sum(row["budget_charge_usd"] for row in rows)
    return result


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(directory: Path, *, pilot: Path | None = None, require_complete=True):
    manifest = json.loads((directory / "manifest.json").read_text())
    l0 = json.loads((directory / "l0.json").read_text())
    rows = [json.loads(line) for line in (directory / "requests.jsonl").read_text().splitlines()]
    interrupted_folders = sorted((directory / "interrupted_attempts").glob("*/started.json"))
    interrupted = sorted((directory / "interrupted_attempts").glob("*/result.json"))
    resume = (
        json.loads((directory / "resume.json").read_text())
        if (directory / "resume.json").exists()
        else {}
    )
    stop = (
        json.loads((directory / "stop.json").read_text())
        if (directory / "stop.json").exists()
        else {}
    )
    excluded = set(stop.get("excluded_from_quality_comparison", []))
    results = [
        json.loads(path.read_text())
        for path in sorted((directory / "segments").glob("*/result.json"))
    ]
    expected = {(r["checkpoint"], r["strategy"], r["repeat"]) for r in manifest["order"]}
    actual = {(r["checkpoint"], r["strategy"], r["repeat"]) for r in results}
    assert len(actual) == len(results) and actual <= expected
    if require_complete:
        assert actual == expected, f"Incomplete campaign: {len(actual)}/{len(expected)}"
        assert all(len(result["probes"]) == 8 for result in results)
    assert len({row["request_id"] for row in rows}) == len(rows)
    assert l0["source_hashes"] == manifest["source_hashes"]
    checks = Counter()
    estimator = TokenEstimator()
    unlogged = []
    request_ids = {row["request_id"] for row in rows}
    request_paths = {}
    for path in [
        *directory.glob("segments/*/requests/*.request.json"),
        *directory.glob("interrupted_attempts/*/requests/*.request.json"),
    ]:
        request_id = path.name.removesuffix(".request.json")
        assert request_id not in request_paths
        request_paths[request_id] = path
        if path.name.removesuffix(".request.json") not in request_ids:
            request = ModelRequest.model_validate_json(path.read_text())
            assert protocol_closed([m for m in request.messages if m.role != "system"])
            checks["unlogged_request_protocol"] += 1
            estimate = estimator.request(request.messages, request.tools)
            unlogged.append(
                {
                    "path": str(path.relative_to(directory)),
                    "sha256": digest(path),
                    "archived": "interrupted_attempts" in path.parts,
                    "cost_status": "unknown: process stopped before usage ledger was written",
                    "conservative_reserve_usd": (
                        max(estimate * 2, 131072) * 0.44
                        + (request.max_output_tokens or 8192) * 1.32
                    )
                    / 1_000_000,
                }
            )
    if require_complete:
        assert not excluded and not any(not r["archived"] for r in unlogged)
    for row in rows:
        path = request_paths[row["request_id"]]
        assert digest(path) == row["request_sha256"]
        request = ModelRequest.model_validate_json(path.read_text())
        assert protocol_closed([m for m in request.messages if m.role != "system"])
        checks["request_hash_and_protocol"] += 1
        if row["usage_complete"]:
            cost = usage_cost(row["raw_usage"], {"hit": 0.014, "miss": 0.44, "output": 1.32})
            assert math.isclose(cost, row["normalized_peak_cost_usd"], abs_tol=1e-12)
            checks["usage_and_pricing"] += 1
    by_strategy = {}
    case_rows = []
    failures = []
    files = {
        "manifest.json": digest(directory / "manifest.json"),
        "l0.json": digest(directory / "l0.json"),
        "requests.jsonl": digest(directory / "requests.jsonl"),
    }
    for checkpoint in manifest["checkpoints"]:
        path = directory / "fixtures" / f"{checkpoint['name']}.json"
        files[str(path.relative_to(directory))] = digest(path)
        if "sha256" in checkpoint:
            fixture_digest = hashlib.sha256(
                json.dumps(
                    json.loads(path.read_text()), sort_keys=True, ensure_ascii=False
                ).encode()
            ).hexdigest()
            assert fixture_digest == checkpoint["sha256"]
            checks["frozen_fixture_hash"] += 1
    for path in interrupted:
        files[str(path.relative_to(directory))] = digest(path)
    if (directory / "recovery.json").exists():
        files["recovery.json"] = digest(directory / "recovery.json")
    if stop:
        files["stop.json"] = digest(directory / "stop.json")
    if resume:
        files["resume.json"] = digest(directory / "resume.json")
    for path in (directory / "administrative").glob("*.json"):
        files[str(path.relative_to(directory))] = digest(path)
    final_request_ids = set()
    for result in results:
        name = f"{result['checkpoint']}-{result['strategy']}-{result['repeat']}"
        folder = directory / "segments" / name
        files[f"segments/{name}/result.json"] = digest(folder / "result.json")
        fixture = json.loads((directory / "fixtures" / f"{result['checkpoint']}.json").read_text())
        with sqlite3.connect((folder / "state.db").as_uri() + "?mode=ro", uri=True) as db:
            source = db.execute(
                "SELECT message_json FROM messages WHERE position<=? ORDER BY position",
                (len(fixture["messages"]),),
            ).fetchall()
            assert [
                ChatMessage.model_validate_json(r[0]).model_dump(mode="json") for r in source
            ] == fixture["messages"]
            active = db.execute(
                "SELECT summary_text FROM context_compactions WHERE status='ready'"
            ).fetchall()
            assert len(active) <= 1
            checks["independent_transcript_immutability"] += 1
            checks["single_active_summary"] += 1
        selected = [r for r in rows if request_paths[r["request_id"]].parent == folder / "requests"]
        final_request_ids.update(r["request_id"] for r in selected)
        without_warm = [r for r in selected if r["phase"] != "warmup"]
        warm = [r for r in selected if r["phase"] == "warmup"]
        generation = [r for r in without_warm if r["phase"] != "continuation"]
        continuation = [r for r in selected if r["phase"] == "continuation"]
        if warm and result["strategy"] in {"A", "D"}:
            source_request = ModelRequest.model_validate_json(
                request_paths[warm[-1]["request_id"]].read_text()
            )
            for row in generation:
                generated = ModelRequest.model_validate_json(
                    request_paths[row["request_id"]].read_text()
                )
                assert generated.messages[:-1] == source_request.messages
                assert generated.tools == source_request.tools
                assert generated.model == source_request.model
                checks["generation_reuses_complete_source_prefix"] += 1
        control_summary_tokens = 0
        if continuation:
            first_request = ModelRequest.model_validate_json(
                request_paths[continuation[0]["request_id"]].read_text()
            )
            control_summary_tokens = sum(
                estimator.text(call.arguments.get("summary", ""))
                for message in first_request.messages
                for call in message.tool_calls
                if call.name == "request_handoff"
            )
        cumulative = []
        for turn in range(0, 9):
            considered = generation + [
                r
                for r in without_warm
                if r["phase"] == "continuation" and r["logical_turn"] <= turn
            ]
            cumulative.append(aggregate_requests(considered)["normalized_peak_cost_usd"])
        before = result.get("capacity", {}).get("before_estimated_tokens")
        after = result.get("capacity", {}).get("after_estimated_tokens")
        reads = sum(len(check["reads"]) for check in result["probes"])
        case_rows.append(
            {
                "checkpoint": result["checkpoint"],
                "kind": result["kind"],
                "strategy": result["strategy"],
                "repeat": result["repeat"],
                "comparison_eligible": name not in excluded and len(result["probes"]) == 8,
                "published": result.get("published", False),
                "passed_probes": result["passed_probes"],
                "completed_probes": sum(q.get("status") == "completed" for q in result["probes"]),
                "semantic_evaluable_probes": sum(
                    q["score"].get("semantic_evaluable", False) for q in result["probes"]
                ),
                "strict_format_probes": result.get("strict_format_probes", 0),
                "all_probes_passed": result["all_probes_passed"],
                "attempted_probes": len(result["probes"]),
                "first_tool_roundtrip_ok": result["first_tool_roundtrip_ok"],
                "before_estimated_tokens": before,
                "after_estimated_tokens": after,
                "estimated_release_tokens": before - after
                if before is not None and after is not None
                else None,
                "raw_prefill_api_input_tokens": warm[-1]["raw_usage"].get("prompt_tokens")
                if warm
                else None,
                "source_cursor": result.get("capacity", {}).get("published_cursor"),
                "generation": aggregate_requests(generation),
                "continuation": aggregate_requests(continuation),
                "continuation_by_turn": [
                    aggregate_requests([r for r in continuation if r["logical_turn"] == turn])
                    for turn in range(1, 9)
                ],
                "control_summary_estimated_tokens": control_summary_tokens,
                "first_continuation_request": aggregate_requests(
                    [r for r in selected if r["phase"] == "continuation"][:1]
                ),
                "all": aggregate_requests(selected),
                "without_warmup": aggregate_requests(without_warm),
                "cumulative_normalized_cost": cumulative,
                "evidence_reads": reads,
                "summary_estimated_tokens": estimator.text(active[0][0]) if active else None,
                "publication": result["publication"],
                "error": result["error"],
                "warmup_second_hit_ratio": (
                    warm[-1]["raw_usage"].get("prompt_cache_hit_tokens", 0)
                    / warm[-1]["raw_usage"].get("prompt_tokens", 1)
                )
                if warm
                else None,
            }
        )
        for check in result["probes"]:
            if not check["passed"] or not check["score"]["format_ok"]:
                failures.append(
                    {
                        "checkpoint": result["checkpoint"],
                        "strategy": result["strategy"],
                        "repeat": result["repeat"],
                        "turn": check["logical_turn"],
                        "factual_passed": check["score"]["passed"],
                        "format_ok": check["score"]["format_ok"],
                        "status": check["status"],
                        "fields": check["score"]["fields"],
                        "required_read_ok": check["required_read_ok"],
                    }
                )
    for strategy in ("CURRENT", "A", "D"):
        chosen = [r for r in case_rows if r["strategy"] == strategy]
        requests = [
            r for r in rows if r["strategy"] == strategy and r["request_id"] in final_request_ids
        ]
        by_strategy[strategy] = {
            "segments": len(chosen),
            "published": sum(r["published"] for r in chosen),
            "all_probes_passed": sum(r["all_probes_passed"] for r in chosen),
            "passed_probes": sum(r["passed_probes"] for r in chosen),
            "completed_probes": sum(r["completed_probes"] for r in chosen),
            "semantic_evaluable_probes": sum(r["semantic_evaluable_probes"] for r in chosen),
            "expected_probes": len(chosen) * 8,
            "strict_format_probes": sum(r["strict_format_probes"] for r in chosen),
            "first_tool_roundtrip_ok": sum(r["first_tool_roundtrip_ok"] for r in chosen),
            "evidence_reads": sum(r["evidence_reads"] for r in chosen),
            "all": aggregate_requests(requests),
            "all_attempts": aggregate_requests([r for r in rows if r["strategy"] == strategy]),
            "without_warmup": aggregate_requests([r for r in requests if r["phase"] != "warmup"]),
            "generation": aggregate_requests(
                [r for r in requests if r["phase"] not in {"warmup", "continuation"}]
            ),
            "continuation": aggregate_requests(
                [r for r in requests if r["phase"] == "continuation"]
            ),
            "finish_reasons_by_phase": {
                phase: dict(Counter(r["finish_reason"] for r in requests if r["phase"] == phase))
                for phase in sorted({r["phase"] for r in requests})
            },
        }
    pairs = []
    for cp in manifest["checkpoints"]:
        for repeat in (1, 2):
            lookup = {
                r["strategy"]: r
                for r in case_rows
                if r["checkpoint"] == cp["name"]
                and r["repeat"] == repeat
                and r["comparison_eligible"]
            }
            for left, right in (("A", "CURRENT"), ("D", "CURRENT"), ("D", "A")):
                if left not in lookup or right not in lookup:
                    continue
                lhs, rhs = lookup[left], lookup[right]
                changes = [
                    a - b
                    for a, b in zip(
                        lhs["cumulative_normalized_cost"],
                        rhs["cumulative_normalized_cost"],
                        strict=True,
                    )
                ]
                stable_break_even = next(
                    (i for i in range(9) if all(value < 0 for value in changes[i:])), None
                )
                left_cost = lhs["without_warmup"]["normalized_peak_cost_usd"]
                right_cost = rhs["without_warmup"]["normalized_peak_cost_usd"]
                pairs.append(
                    {
                        "checkpoint": cp["name"],
                        "repeat": repeat,
                        "left": left,
                        "right": right,
                        "cost_delta_usd": left_cost - right_cost,
                        "cost_saving_ratio": 1 - left_cost / right_cost if right_cost else None,
                        "both_all_probes_passed": lhs["all_probes_passed"]
                        and rhs["all_probes_passed"],
                        "stable_break_even_checkpoint": stable_break_even,
                        "cumulative_cost_delta": changes,
                    }
                )
    pilot_metrics = None
    if pilot and (pilot / "requests.jsonl").exists():
        pilot_rows = [
            json.loads(line) for line in (pilot / "requests.jsonl").read_text().splitlines()
        ]
        pilot_metrics = aggregate_requests(pilot_rows)
    eligible = [r for r in case_rows if r["comparison_eligible"]]
    balanced_keys = {
        (r["checkpoint"], r["repeat"])
        for r in eligible
        if {
            c["strategy"]
            for c in eligible
            if (c["checkpoint"], c["repeat"]) == (r["checkpoint"], r["repeat"])
        }
        == {"CURRENT", "A", "D"}
    }
    balanced = {}
    for strategy in ("CURRENT", "A", "D"):
        chosen = [
            r
            for r in eligible
            if r["strategy"] == strategy and (r["checkpoint"], r["repeat"]) in balanced_keys
        ]
        selected = [
            r
            for r in rows
            if r["strategy"] == strategy
            and (r["checkpoint"], r["repeat"]) in balanced_keys
            and r["request_id"] in final_request_ids
        ]
        balanced[strategy] = {
            "segments": len(chosen),
            "passed_probes": sum(r["passed_probes"] for r in chosen),
            "published": sum(r["published"] for r in chosen),
            "strict_format_probes": sum(r["strict_format_probes"] for r in chosen),
            "first_tool_roundtrip_ok": sum(r["first_tool_roundtrip_ok"] for r in chosen),
            "all": aggregate_requests(selected),
            "without_warmup": aggregate_requests([r for r in selected if r["phase"] != "warmup"]),
            "generation": aggregate_requests(
                [r for r in selected if r["phase"] not in {"warmup", "continuation"}]
            ),
            "continuation": aggregate_requests(
                [r for r in selected if r["phase"] == "continuation"]
            ),
        }
    generation_by_hash = {}
    for row in rows:
        if row["phase"] not in {"warmup", "continuation"}:
            usage = row["raw_usage"]
            generation_by_hash.setdefault(row["request_sha256"], []).append(
                {
                    "checkpoint": row["checkpoint"],
                    "strategy": row["strategy"],
                    "repeat": row["repeat"],
                    "request_id": row["request_id"],
                    "archived": "interrupted_attempts" in request_paths[row["request_id"]].parts,
                    "cache_hit_ratio": usage.get("prompt_cache_hit_tokens", 0)
                    / usage["prompt_tokens"]
                    if usage.get("prompt_tokens")
                    else None,
                }
            )
    return {
        "schema_version": 2,
        "scope": "L0 and L1 only; forced D, not autonomous AD or task success rate",
        "manifest": manifest,
        "l0": l0,
        "audit_checks": dict(checks),
        "completed_segments": len(results),
        "uninterrupted_completed_segments": len(eligible),
        "started_unique_segments": len(list((directory / "segments").glob("*/started.json"))),
        "planned_segments": len(expected),
        "groups": by_strategy,
        "balanced_comparison": balanced,
        "cases": case_rows,
        "paired_comparisons": pairs,
        "identical_generation_requests": {
            key: values for key, values in generation_by_hash.items() if len(values) > 1
        },
        "quality_or_format_failures": failures,
        "pilot_metrics": pilot_metrics,
        "campaign_metrics": aggregate_requests(rows),
        "interrupted_attempts": [json.loads(path.read_text()) for path in interrupted],
        "interrupted_attempt_count": len(interrupted_folders),
        "interrupted_attempt_metrics": aggregate_requests(
            [r for r in rows if "interrupted_attempts" in request_paths[r["request_id"]].parts]
        ),
        "budget_reserved_usd": sum(row["budget_charge_usd"] for row in rows),
        "unlogged_requests": unlogged,
        "stop": stop,
        "resume": resume,
        "price_boundary_crossed_requests": sum(row["price_boundary_crossed"] for row in rows),
        "artifact_hashes": files,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--pilot", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args()
    report = audit(args.input.resolve(), pilot=args.pilot, require_complete=not args.allow_partial)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"segments": report["completed_segments"], "audit": report["audit_checks"]}))


if __name__ == "__main__":
    main()
