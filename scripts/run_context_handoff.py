#!/usr/bin/env python3
"""Freeze and run L0/L1 only; no full public-task suite is launched."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from bot.config import load_config, resolve_model_api_key
from bot.core.models import ChatMessage
from bot.evals.context_handoff import experiment_config, experiment_order, run_segment
from bot.evals.handoff_fixtures import Checkpoint, build_checkpoints
from bot.evals.handoff_recording import (
    PRICE_CHECKED,
    PRICE_SOURCE,
    ExperimentLedger,
    aggregate_requests,
)
from bot.providers import OpenAICompatibleProvider

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = [
    "src/bot/compaction/handoff.py",
    "src/bot/compaction/service.py",
    "src/bot/core/agent.py",
    "src/bot/core/models.py",
    "src/bot/sessions/store.py",
    "src/bot/evals/context_handoff.py",
    "src/bot/evals/handoff_fixtures.py",
    "src/bot/evals/handoff_recording.py",
    "src/bot/providers/openai_compatible.py",
    "scripts/run_context_handoff.py",
]


def source_hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def freeze(directory):
    manifest_file = directory / "manifest.json"
    if manifest_file.exists():
        raise ValueError("Manifest already frozen; use another experiment directory for changes")
    checkpoints = build_checkpoints(ROOT)
    fixtures = directory / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    for checkpoint in checkpoints:
        (fixtures / f"{checkpoint.name}.json").write_text(
            json.dumps(checkpoint.serializable(), ensure_ascii=False, indent=2)
        )
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    manifest = {
        "schema_version": 1,
        "rubric_version": 2,
        "warmup": "unmodified source prefix twice; outputs discarded, usage included",
        "commit": commit,
        "source_hashes": source_hashes(),
        "scope": "L0/L1 controlled continuation; not autonomous handoff or public-task completion",
        "model": "deepseek-v4-flash",
        "thinking": "disabled",
        "context_window_configured": 131072,
        "summary_max_output": 8192,
        "summary_target": 3000,
        "summary_hard_limit": 4000,
        "continuation_max_output": 2048,
        "recent_tail_target": 20000,
        "high_water": 90000,
        "low_water": 40000,
        "summary_input_limit_A_D": 114688,
        "current_input_limit": 60000,
        "repeats": 2,
        "probes_per_segment": 8,
        "pricing": {
            "url": PRICE_SOURCE,
            "checked": PRICE_CHECKED,
            "off_peak": {"hit": 0.007, "miss": 0.22, "output": 0.66},
            "peak_multiplier": 2,
            "peak_utc": "Monday-Friday 01:00-04:00 and 06:00-10:00",
            "billing_status": "usage-times-published-price, not invoice reconciled",
        },
        "checkpoints": [
            {"name": cp.name, "sha256": cp.sha256, "kind": cp.kind, "provenance": cp.provenance}
            for cp in checkpoints
        ],
        "order": [
            {"checkpoint": cp.name, "strategy": strategy, "repeat": repeat}
            for cp, strategy, repeat in experiment_order(checkpoints)
        ],
    }
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(
        json.dumps({"frozen": str(manifest_file), "segments": len(manifest["order"])}), flush=True
    )


def l0(directory):
    directory.mkdir(parents=True, exist_ok=True)
    xml = directory / "l0-junit.xml"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "tests/integration/test_context_handoff.py",
        "tests/unit/test_handoff_evaluation.py",
        "tests/integration/test_handoff_l1_runner.py",
        f"--junitxml={xml}",
    ]
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    (directory / "l0-output.txt").write_text(completed.stdout + completed.stderr)
    cases = []
    if xml.exists():
        for node in ET.parse(xml).getroot().iter("testcase"):
            cases.append(
                {
                    "name": node.attrib["name"],
                    "seconds": float(node.attrib.get("time", 0)),
                    "passed": not any(
                        node.find(key) is not None for key in ("failure", "error", "skipped")
                    ),
                }
            )
    summary = {
        "exit_code": completed.returncode,
        "tests": len(cases),
        "passed": sum(case["passed"] for case in cases),
        "cases": cases,
        "source_hashes": source_hashes(),
    }
    (directory / "l0.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: v for k, v in summary.items() if k not in {"cases", "source_hashes"}}))
    return completed.returncode


def analyze(directory):
    rows = (
        [json.loads(line) for line in (directory / "requests.jsonl").read_text().splitlines()]
        if (directory / "requests.jsonl").exists()
        else []
    )
    results = [
        json.loads(path.read_text())
        for path in sorted((directory / "segments").glob("*/result.json"))
    ]
    by_strategy = {}
    for strategy in ("CURRENT", "A", "D"):
        selected = [r for r in results if r["strategy"] == strategy]
        requests = [r for r in rows if r["strategy"] == strategy]
        by_strategy[strategy] = {
            "segments": len(selected),
            "published": sum(r.get("published", False) for r in selected),
            "all_probes_passed": sum(r["all_probes_passed"] for r in selected),
            "passed_probes": sum(r["passed_probes"] for r in selected),
            "strict_format_probes": sum(r.get("strict_format_probes", 0) for r in selected),
            "expected_probes": 8 * len(selected),
            "first_tool_roundtrip_ok": sum(r["first_tool_roundtrip_ok"] for r in selected),
            "transcript_immutable": sum(r["transcript_immutable"] for r in selected),
            "metrics_all": aggregate_requests(requests),
            "metrics_without_warmup": aggregate_requests(
                [r for r in requests if r["phase"] != "warmup"]
            ),
            "metrics_by_phase": {
                phase: aggregate_requests([r for r in requests if r["phase"] == phase])
                for phase in sorted({r["phase"] for r in requests})
            },
        }
    report = {
        "segments": len(results),
        "planned_segments": 36,
        "by_strategy": by_strategy,
        "results": results,
    }
    (directory / "comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    brief = {
        name: {
            "segments": row["segments"],
            "published": row["published"],
            "passed_probes": row["passed_probes"],
            "cost_usd": row["metrics_all"]["cost_usd_at_start_rate"],
        }
        for name, row in by_strategy.items()
    }
    print(json.dumps({"segments": len(results), "groups": brief}, ensure_ascii=False), flush=True)
    return report


async def live(args):
    if os.environ.get("RUN_CONTEXT_HANDOFF_LIVE") != "1":
        raise ValueError("Set RUN_CONTEXT_HANDOFF_LIVE=1 for real Provider calls")
    manifest = json.loads((args.output / "manifest.json").read_text())
    if manifest["source_hashes"] != source_hashes():
        raise ValueError(
            "Source changed after freeze; preserve this campaign and freeze a new version"
        )
    config = experiment_config(load_config(ROOT))
    if config.model.base_url.rstrip("/") not in {
        "https://api.deepseek.com",
        "https://api.deepseek.com/v1",
    }:
        raise ValueError("Frozen pricing requires the official DeepSeek endpoint")
    provider = OpenAICompatibleProvider(
        base_url=config.model.base_url,
        api_key=resolve_model_api_key(config.model, workspace=ROOT),
        timeout_seconds=90,
    )
    ledger = ExperimentLedger(args.output / "requests.jsonl", max_cost_usd=args.max_cost_usd)
    checkpoints = {}
    for record in manifest["checkpoints"]:
        payload = json.loads((args.output / "fixtures" / f"{record['name']}.json").read_text())
        payload["messages"] = [
            ChatMessage.model_validate(message) for message in payload["messages"]
        ]
        checkpoint = Checkpoint(**payload)
        if checkpoint.sha256 != record["sha256"]:
            raise ValueError("Frozen fixture changed")
        checkpoints[checkpoint.name] = checkpoint
    launched = 0
    for item in manifest["order"]:
        name = f"{item['checkpoint']}-{item['strategy']}-{item['repeat']}"
        target = args.output / "segments" / name
        if (target / "result.json").exists():
            continue
        if args.limit_segments is not None and launched >= args.limit_segments:
            break
        print(json.dumps({"starting": name, "budget_used_usd": ledger.reserved_cost}), flush=True)
        result = await run_segment(
            checkpoints[item["checkpoint"]],
            item["strategy"],
            item["repeat"],
            directory=target,
            config=config,
            provider=provider,
            ledger=ledger,
        )
        launched += 1
        analyze(args.output)
        if result["error"] or len(result["probes"]) != 8:
            print(
                json.dumps({"stopped_for_inspection": name, "error": result["error"]}), flush=True
            )
            return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["freeze", "l0", "l1", "analyze"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-cost-usd", type=float, default=10.0)
    parser.add_argument("--limit-segments", type=int)
    args = parser.parse_args()
    if args.max_cost_usd <= 0:
        parser.error("max-cost-usd must be positive")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "execution.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.phase == "freeze":
            freeze(args.output)
        elif args.phase == "l0":
            return l0(args.output)
        elif args.phase == "l1":
            return asyncio.run(live(args))
        else:
            analyze(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
