#!/usr/bin/env python3
"""Run one real Harbor task with 128K/256K/512K bot input budgets."""

from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import tomli_w
from run_context_strategy_task import digest, run, save

from bot.config import load_config
from bot.config.models import AppConfig
from bot.core.context import TokenBudget
from bot.evals.context_strategy_audit import aggregate, analyze_events, duration
from bot.evals.terminalbench import build_project_wheel, model_hostname
from bot.providers.token_counting import TOKENIZER_SHA256, load_tokenizer, tokenizer_path

LIMITS = (128_000, 256_000, 512_000)
PRICING = {
    "as_of": "2026-09-14",
    "source": "https://api-docs.deepseek.com/quick_start/pricing/",
    "usd_per_million_peak": {"hit": 0.006, "miss": 0.30, "output": 1.20},
    "off_peak_multiplier": 0.5,
    "note": "Frozen published Flash rates; usage-derived estimate, not an invoice.",
}


def configurations(root: Path) -> dict[str, AppConfig]:
    local = load_config(root)
    model = local.model.model_dump(mode="json", exclude_none=True, exclude={"api_key"})
    model.update(
        # Keep the accepted alias so the existing runtime uses its pinned counter.
        # Official docs say this alias now routes to V4.1 Flash. Audit actual usage
        # and estimator errors rather than claiming the old tokenizer is exact.
        name="deepseek-v4-flash",
        api_key_ref="env:BOT_MODEL_API_KEY",
        temperature=0,
        thinking="disabled",
        max_output_tokens=32768,
        context_window_tokens=1_000_000,
        input_cost_per_million=PRICING["usd_per_million_peak"]["miss"],
        output_cost_per_million=PRICING["usd_per_million_peak"]["output"],
    )
    baseline = AppConfig.model_validate(
        {
            "model": model,
            "context": {
                "compaction_strategy": "a_fallback",
                "compaction_low_water_tokens": 40_000,
                "recent_conversation_tokens": 20_000,
                "compaction_thinking": "disabled",
                "compaction_model": model["name"],
            },
            "agent": {"finalization": {"model_timeout_seconds": 30}},
            "subagents": {"enabled": False},
            "memory": {"enabled": False, "auto_extract": False},
        }
    )
    result = {}
    for limit in LIMITS:
        cfg = baseline.model_copy(deep=True)
        cfg.context.max_input_tokens = limit
        result[f"{limit // 1000}k"] = cfg
    return result


def verify_configs(configs: dict[str, dict]) -> dict:
    baseline = None
    budgets = {}
    if set(configs) != {f"{limit // 1000}k" for limit in LIMITS}:
        raise ValueError("Expected exactly the three declared input budgets")
    for label, value in configs.items():
        cfg = AppConfig.model_validate(value)
        budget = TokenBudget(
            context_window_tokens=cfg.model.context_window_tokens,
            configured_input_limit=cfg.context.max_input_tokens,
            output_reserve_tokens=max(
                cfg.context.output_reserve_tokens, cfg.model.max_output_tokens or 0
            ),
            protocol_reserve_tokens=cfg.context.protocol_reserve_tokens,
            safety_margin_tokens=cfg.context.safety_margin_tokens,
            target_utilization=cfg.context.auto_compact_threshold,
        )
        expected = int(label.removesuffix("k")) * 1000
        if budget.hard_input_limit != expected or cfg.context.max_input_tokens != expected:
            raise ValueError("Requested input budget is capped by the model window/reserves")
        comparable = cfg.model_dump(mode="json")
        comparable["context"].pop("max_input_tokens")
        if baseline is not None and comparable != baseline:
            raise ValueError("Configurations differ beyond max_input_tokens")
        baseline = comparable
        budgets[label] = budget.as_dict()
    return {"only_input_limit_differs": True, "budgets": budgets}


def verify(manifest: dict) -> dict:
    for asset in [manifest["wheel"], manifest["tokenizer"], *manifest["configs"].values()]:
        if digest(Path(asset["path"])) != asset["sha256"]:
            raise ValueError(f"Frozen asset changed: {asset['path']}")
    for name, expected in manifest["task_files"].items():
        if digest(Path(manifest["task"]) / name) != expected:
            raise ValueError(f"Frozen task changed: {name}")
    return verify_configs(
        {
            label: tomllib.loads(Path(asset["path"]).read_text())
            for label, asset in manifest["configs"].items()
        }
    )


def prepare(root: Path, output: Path, task: Path) -> None:
    if not (task / "task.toml").is_file():
        raise ValueError("task.toml missing")
    configs = configurations(root)
    controls = verify_configs({label: cfg.model_dump() for label, cfg in configs.items()})
    load_tokenizer(tokenizer_path())
    output.mkdir(parents=True, exist_ok=False)
    wheel = build_project_wheel(root, output / "wheel")
    vocabulary = output / "tokenizer.json"
    shutil.copyfile(tokenizer_path(), vocabulary)
    assets = {}
    for label, cfg in configs.items():
        path = output / f"{label}.toml"
        path.write_text(tomli_w.dumps(cfg.model_dump(mode="json", exclude_none=True)))
        assets[label] = {"path": str(path), "sha256": digest(path)}
    order = list(configs)
    random.Random(20260914).shuffle(order)
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "script_sha256": digest(Path(__file__)),
        "runner_script_sha256": digest(Path(__file__).with_name("run_context_strategy_task.py")),
        "task": str(task),
        "task_files": {
            str(p.relative_to(task)): digest(p) for p in sorted(task.rglob("*")) if p.is_file()
        },
        "wheel": {"path": str(wheel), "sha256": digest(wheel)},
        "tokenizer": {"path": str(vocabulary), "sha256": TOKENIZER_SHA256},
        "configs": assets,
        "order": order,
        "seed": 20260914,
        "repeats": 1,
        "concurrency": 1,
        "max_steps": 240,
        "worker_wall_seconds": 1740,
        "max_cost_usd_conservative_per_run": 20,
        "model": configs["128k"].model.name,
        "model_host": model_hostname(configs["128k"].model.base_url),
        "pricing": PRICING,
        "controls": controls,
        "limits": [
            "K means 1000 tokens; these are configured input caps, not observed prompt lengths.",
            "Only max_input_tokens changes. Model window is 1M; pressure ratio is 0.8 in all arms.",
            "One task, one trial per budget: descriptive pilot, not a causal or reliable ranking.",
            "Natural task execution: no artificial warmup, padding or forced continuation.",
            "Cross-run provider cache reuse and stochastic task trajectories remain confounders.",
            "Existing Harbor worker disables skills, subagents and cross-session memory.",
            "Accepted v4-flash alias routes to current Flash per docs; record returned model.",
            "Pinned V4 tokenizer is an estimate; API usage defines measured length and cache rate.",
            "Missing usage and interrupted requests have unknown cost, not zero cost.",
        ],
    }
    verify(manifest)
    save(output / "manifest.json", manifest)
    print(json.dumps({"prepared": str(output), "order": order, "controls": controls}), flush=True)


def report(directory: Path, *, partial: bool = False) -> dict:
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    result = {
        "manifest": manifest,
        "manifest_sha256": digest(manifest_path),
        "controls": verify(manifest),
        "runs": {},
    }
    for label in manifest["order"]:
        trials = list((directory / "jobs" / label).glob("*/result.json"))
        if not trials and partial:
            continue
        if len(trials) != 1:
            raise ValueError(f"Expected one completed trial for {label}, found {len(trials)}")
        trial_path = trials[0]
        trial = json.loads(trial_path.read_text())
        events_path = trial_path.parent / "agent/events.jsonl"
        events = (
            [json.loads(line) for line in events_path.read_text().splitlines()]
            if events_path.exists()
            else []
        )
        info = analyze_events(events, manifest["pricing"])
        main = [row for row in info["requests"] if row["phase"] == "main"]
        info["main_by_input_length"] = {}
        for lower, upper in [(0, 128000), (128000, 256000), (256000, 512000), (512000, 1000000)]:
            rows = [
                row for row in main if lower <= row["raw_usage"].get("prompt_tokens", -1) < upper
            ]
            info["main_by_input_length"][f"{lower}-{upper}"] = aggregate(rows, manifest["pricing"])
        info["main_excluding_first"] = aggregate(main[1:], manifest["pricing"])
        info["first_main_usage"] = main[0]["raw_usage"] if main else None
        info["reported_models"] = sorted(
            {
                e["payload"].get("provider_metadata", {}).get("model")
                for e in events
                if e["type"] == "model.usage"
                and e["payload"].get("provider_metadata", {}).get("model")
            }
        )
        errors = [
            r["input_token_error"]
            for r in info["requests"]
            if isinstance(r.get("input_token_error"), int)
        ]
        info["max_absolute_input_token_error"] = max(map(abs, errors), default=None)
        agent_result = trial_path.parent / "agent/result.json"
        info.update(
            input_limit=int(label.removesuffix("k")) * 1000,
            trial=str(trial_path.relative_to(directory)),
            events_sha256=digest(events_path) if events_path.exists() else None,
            result=json.loads(agent_result.read_text()) if agent_result.exists() else None,
            verifier=trial.get("verifier_result"),
            exception=trial.get("exception_info"),
            wall_seconds=duration(trial),
            agent_seconds=duration(trial.get("agent_execution") or {}),
        )
        result["runs"][label] = info
    result["complete"] = len(result["runs"]) == len(LIMITS)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--report", action="store_true")
    parser.add_argument("--partial", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if args.run:
        verify(json.loads((output / "manifest.json").read_text()))
        run(root, output)
    elif args.report:
        result = report(output, partial=args.partial)
        save(output / "report.json", result)
        print(
            json.dumps(
                {
                    label: {
                        "usage": info["totals"],
                        "peak": info["main_input_peak"],
                        "verifier": info["verifier"],
                    }
                    for label, info in result["runs"].items()
                }
            ),
            flush=True,
        )
    elif args.task:
        prepare(root, output, args.task.resolve())
    else:
        parser.error("--task is required when preparing")


if __name__ == "__main__":
    main()
