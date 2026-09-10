#!/usr/bin/env python3
"""Freeze and run CURRENT/A/B through Harbor's actual installed AgentRunner."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import tomli_w

from bot.config.loader import load_config, resolve_model_api_key
from bot.config.models import AppConfig
from bot.evals.terminalbench import (
    HARBOR_AGENT_IMPORT_PATH,
    HARBOR_VERSION,
    _harbor_subprocess_env,
    build_project_wheel,
    model_hostname,
)
from bot.providers.token_counting import TOKENIZER_SHA256, load_tokenizer, tokenizer_path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def prepare(root: Path, output: Path, task: Path, input_limit: int) -> None:
    if (output / "manifest.json").exists():
        raise ValueError("manifest already exists; use a new output directory for a new experiment")
    if not (task / "task.toml").is_file():
        raise ValueError("task.toml missing")
    local = load_config(root)
    if local.model.name != "deepseek-v4-pro":
        raise ValueError("This frozen price/tokenizer protocol requires deepseek-v4-pro")
    source_tokenizer = tokenizer_path()
    load_tokenizer(source_tokenizer)
    output.mkdir(parents=True, exist_ok=True)
    wheel = build_project_wheel(root, output / "wheel")
    vocab = output / "tokenizer.json"
    shutil.copyfile(source_tokenizer, vocab)
    model = local.model.model_dump(mode="json", exclude_none=True, exclude={"api_key"})
    model.update(
        api_key_ref="env:BOT_MODEL_API_KEY",
        temperature=0,
        thinking="disabled",
        max_output_tokens=8192,
        context_window_tokens=131072,
        input_cost_per_million=1.32,
        output_cost_per_million=3.96,
    )
    cfg = AppConfig.model_validate(
        {
            "model": model,
            "context": {
                "max_input_tokens": input_limit,
                "compaction_low_water_tokens": min(40000, input_limit // 3),
                "recent_conversation_tokens": min(20000, input_limit // 6),
                "compaction_thinking": "disabled",
            },
            "agent": {"finalization": {"model_timeout_seconds": 30}},
            "subagents": {"enabled": False},
            "memory": {"enabled": False, "auto_extract": False},
        }
    )
    configs = {}
    for strategy in ("current", "a", "b"):
        cfg.context.compaction_strategy = strategy
        path = output / f"{strategy}.toml"
        path.write_text(tomli_w.dumps(cfg.model_dump(mode="json", exclude_none=True)))
        configs[strategy] = {"path": str(path), "sha256": digest(path)}
    order = ["current", "a", "b"]
    random.Random(20260910).shuffle(order)
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "task": str(task),
        "task_files": {
            str(p.relative_to(task)): digest(p) for p in sorted(task.rglob("*")) if p.is_file()
        },
        "wheel": {"path": str(wheel), "sha256": digest(wheel)},
        "tokenizer": {"path": str(vocab), "sha256": TOKENIZER_SHA256},
        "configs": configs,
        "order": order,
        "seed": 20260910,
        "repeats": 1,
        "concurrency": 1,
        "max_steps": 240,
        "worker_wall_seconds": 1740,
        "max_cost_usd_conservative_per_run": 10,
        "model": model["name"],
        "model_host": model_hostname(model["base_url"]),
        "pricing": {
            "source": "https://api-docs.deepseek.com/quick_start/pricing/",
            "as_of": "2026-09-10",
            "usd_per_million_peak": {"hit": 0.044, "miss": 1.32, "output": 3.96},
            "off_peak_multiplier": 0.5,
            "peak_utc": "Monday-Friday [01:00,04:00), [06:00,10:00)",
        },
        "limits": [
            "One task and one repeat: pilot, not a statistically reliable strategy ranking.",
            "No artificial cache warmup; cross-run provider cache effects cannot be eliminated.",
            "Skills are disabled by the existing Harbor worker; tested separately in regression.",
            "All groups use explicit thinking=disabled; enabled reasoning is outside this pilot.",
            "Missing usage, including cancelled background calls, has unknown cost, not zero.",
        ],
    }
    save(output / "manifest.json", manifest)
    print(json.dumps({"prepared": str(output), "order": order}, ensure_ascii=False), flush=True)


def run(root: Path, output: Path) -> None:
    manifest = json.loads((output / "manifest.json").read_text())
    for asset in [manifest["wheel"], manifest["tokenizer"], *manifest["configs"].values()]:
        if digest(Path(asset["path"])) != asset["sha256"]:
            raise ValueError(f"frozen asset changed: {asset['path']}")
    task = Path(manifest["task"])
    for name, expected in manifest["task_files"].items():
        if digest(task / name) != expected:
            raise ValueError(f"frozen task changed: {name}")
    env = _harbor_subprocess_env()
    local = load_config(root)
    env["BOT_MODEL_API_KEY"] = resolve_model_api_key(local.model, workspace=root)
    uvx = shutil.which("uvx")
    if uvx is None:
        raise ValueError("uvx missing")
    for strategy in manifest["order"]:
        status_path = output / f"{strategy}-status.json"
        if status_path.exists():
            if json.loads(status_path.read_text()).get("ended_at"):
                continue
            raise ValueError(f"{strategy} already started; inspect that job before resuming")
        command = [
            uvx,
            "--from",
            f"harbor=={HARBOR_VERSION}",
            "--with",
            manifest["wheel"]["path"],
            "harbor",
            "run",
            "--path",
            manifest["task"],
            "--agent",
            HARBOR_AGENT_IMPORT_PATH,
            "--model",
            f"openai-compatible/{manifest['model']}",
            "--agent-env",
            "BOT_MODEL_API_KEY=${BOT_MODEL_API_KEY}",
            "--agent-env",
            "BOT_MODEL_API_KEY_REF=env:BOT_MODEL_API_KEY",
            "--allow-agent-host",
            manifest["model_host"],
            "--n-concurrent",
            "1",
            "--n-attempts",
            "1",
            "--agent-setup-timeout-multiplier",
            "2",
            "--jobs-dir",
            str(output / "jobs"),
            "--job-name",
            strategy,
            "--quiet",
        ]
        kwargs = {
            "package_path": manifest["wheel"]["path"],
            "config_path": manifest["configs"][strategy]["path"],
            "tokenizer_path": manifest["tokenizer"]["path"],
            "max_steps": manifest["max_steps"],
            "max_wall_time_seconds": manifest["worker_wall_seconds"],
            "max_cost_usd": manifest["max_cost_usd_conservative_per_run"],
            "subagents_enabled": "false",
        }
        for key, value in kwargs.items():
            command += ["--agent-kwarg", f"{key}={value}"]
        status = {
            "strategy": strategy,
            "started_at": datetime.now(UTC).isoformat(),
            "command": command,
        }
        save(status_path, status)
        print(f"{strategy}: started", flush=True)
        with (output / f"{strategy}.log").open("w") as log:
            completed = subprocess.run(
                command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT
            )
        status.update(ended_at=datetime.now(UTC).isoformat(), returncode=completed.returncode)
        save(status_path, status)
        print(f"{strategy}: Harbor exit={completed.returncode}", flush=True)
    print(
        "All planned jobs ended. Inspect verifier rewards, compression coverage and raw usage.",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task", type=Path)
    parser.add_argument("--input-limit", type=int, default=120000)
    parser.add_argument("--run", action="store_true", help="Run the previously frozen paid trials")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.run:
        run(root, args.output.resolve())
    elif args.task:
        prepare(root, args.output.resolve(), args.task.resolve(), args.input_limit)
    else:
        parser.error("--task is required when preparing")


if __name__ == "__main__":
    main()
