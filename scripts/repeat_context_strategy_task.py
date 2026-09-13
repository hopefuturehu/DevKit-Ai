#!/usr/bin/env python3
"""Repeat completed strategy trials with their exact frozen runtime and configurations."""

from __future__ import annotations

import argparse
import copy
import json
from datetime import UTC, datetime
from pathlib import Path

from run_context_strategy_task import digest, run, save

from bot.evals.context_strategy_audit import verify_manifest


def prepare(baseline: Path, output: Path, additional_repeats: int) -> None:
    if additional_repeats < 1:
        raise ValueError("additional_repeats must be positive")
    original_path = baseline / "manifest.json"
    original = json.loads(original_path.read_text())
    verify_manifest(original)
    order = original["order"]
    for strategy in order:
        status = json.loads((baseline / f"{strategy}-status.json").read_text())
        if not status.get("ended_at"):
            raise ValueError(f"Baseline trial has not ended: {strategy}")
    output.mkdir(parents=True, exist_ok=False)
    source = {"path": str(original_path), "sha256": digest(original_path)}
    blocks = []
    for index in range(1, additional_repeats + 1):
        block = output / f"repeat-{index + 1}"
        block.mkdir()
        manifest = copy.deepcopy(original)
        offset = index % len(order)
        manifest.update(
            created_at=datetime.now(UTC).isoformat(),
            baseline_manifest=source,
            repetition_index=index + 1,
            order=order[offset:] + order[:offset],
            order_method="Predeclared cyclic rotation of the baseline order; not model seeds.",
            seed=None,
            script_sha256=digest(Path(__file__).with_name("run_context_strategy_task.py")),
            repeat_script_sha256=digest(Path(__file__)),
        )
        verify_manifest(manifest)
        save(block / "manifest.json", manifest)
        blocks.append(
            {
                "directory": str(block),
                "manifest_sha256": digest(block / "manifest.json"),
                "repetition_index": index + 1,
                "order": manifest["order"],
            }
        )
    campaign = {
        "created_at": datetime.now(UTC).isoformat(),
        "baseline_manifest": source,
        "additional_repeats_per_strategy": additional_repeats,
        "additional_trials": len(order) * additional_repeats,
        "concurrency": 1,
        "blocks": blocks,
        "notes": [
            "Reuse baseline wheel, tokenizer, configurations and task files by verified hash.",
            "No runtime rebuild and no changes to task, model, budgets or compaction behavior.",
            "Order is frozen before requests; fewer than four blocks are not fully balanced.",
            "Same task repeated, not independent task coverage or a stable success-rate estimate.",
            "Follow-up selected after inspecting the pilot; report new trials separately too.",
            "STOP in the campaign directory prevents the next trial from starting.",
        ],
    }
    save(output / "campaign.json", campaign)
    print(json.dumps(campaign, ensure_ascii=False), flush=True)


def run_campaign(root: Path, output: Path) -> None:
    campaign = json.loads((output / "campaign.json").read_text())
    source = campaign["baseline_manifest"]
    if digest(Path(source["path"])) != source["sha256"]:
        raise ValueError("Baseline manifest changed")
    original = json.loads(Path(source["path"]).read_text())
    verify_manifest(original)
    frozen_fields = (
        "revision",
        "wheel",
        "tokenizer",
        "configs",
        "task",
        "task_files",
        "model",
        "main_max_output_tokens",
        "summary_max_output_tokens",
        "max_steps",
        "worker_wall_seconds",
        "max_cost_usd_conservative_per_run",
        "pricing",
    )
    for block in campaign["blocks"]:
        path = Path(block["directory"]) / "manifest.json"
        if digest(path) != block["manifest_sha256"]:
            raise ValueError(f"Repeat manifest changed: {path}")
        manifest = json.loads(path.read_text())
        if any(manifest[field] != original[field] for field in frozen_fields):
            raise ValueError(f"Repeat differs from frozen baseline: {path}")
        verify_manifest(manifest)
    for block in campaign["blocks"]:
        if (output / "STOP").exists():
            print("Campaign STOP present; no more trials will start.", flush=True)
            return
        print(f"Starting repetition {block['repetition_index']}: {block['order']}", flush=True)
        run(root, Path(block["directory"]), stop_path=output / "STOP")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--additional-repeats", type=int, default=2)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if args.run:
        run_campaign(Path(__file__).resolve().parents[1], args.output.resolve())
    elif args.baseline:
        prepare(args.baseline.resolve(), args.output.resolve(), args.additional_repeats)
    else:
        parser.error("--baseline is required when preparing")


if __name__ == "__main__":
    main()
