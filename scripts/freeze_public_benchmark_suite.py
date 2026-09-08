#!/usr/bin/env python3
"""Freeze a stratified public suite from cached, unmodified upstream datasets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tomllib
from collections import defaultdict
from pathlib import Path


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def select(rows: list[dict], *, stratum: str, count: int, seed: str) -> list[dict]:
    """One per stratum, then a second from the largest strata; stable under input reorder."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row[stratum]].append(row)
    if not len(groups) <= count <= 2 * len(groups):
        raise ValueError("This sampling policy requires one or two cases per stratum")
    extras = set(sorted(groups, key=lambda key: (-len(groups[key]), key))[: count - len(groups)])
    result = []
    for key in sorted(groups):
        ordered = sorted(
            groups[key], key=lambda row: hashlib.sha256(f"{seed}:{row['id']}".encode()).hexdigest()
        )
        result.extend(ordered[: 2 if key in extras else 1])
    if len(result) != count:
        raise ValueError("Insufficient cases in a selected stratum")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--terminal-cache", type=Path, required=True)
    parser.add_argument("--swe-arrow", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evaluator-output", type=Path, required=True)
    parser.add_argument("--seed", default="bot-public-regression-v1")
    args = parser.parse_args()
    if args.output.exists() or args.evaluator_output.exists():
        raise FileExistsError("Refusing to overwrite a frozen suite or evaluator dataset")
    # Import only in the independent SWE-bench environment; project dependencies stay unchanged.
    for name in ("ALL_PROXY", "all_proxy"):
        os.environ.pop(name, None)
    from datasets import Dataset

    terminal = []
    for path in sorted(args.terminal_cache.glob("*/*/task.toml")):
        config = tomllib.loads(path.read_text())
        terminal.append(
            {
                "id": config["task"]["name"],
                "category": config["metadata"]["category"],
                "difficulty": config["metadata"].get("difficulty"),
                "package_hash": path.parent.name,
                "task_toml_sha256": sha256(path),
                "image": config["environment"]["docker_image"],
                "cpus": config["environment"]["cpus"],
                "memory_mb": config["environment"]["memory_mb"],
                "agent_timeout_sec": config["agent"]["timeout_sec"],
                "verifier_timeout_sec": config["verifier"]["timeout_sec"],
            }
        )
    if len({r["id"] for r in terminal}) != len(terminal):
        raise ValueError("Multiple cached revisions of a task; resolve dataset revision first")
    raw = list(Dataset.from_file(str(args.swe_arrow)))
    swe = [
        {
            "id": r["instance_id"],
            "repo": r["repo"],
            "version": r["version"],
            "base_commit": r["base_commit"],
            "problem_statement_sha256": hashlib.sha256(r["problem_statement"].encode()).hexdigest(),
            "issue_title": r["problem_statement"].splitlines()[0],
            "image": "swebench/sweb.eval.x86_64."
            + r["instance_id"].lower().replace("__", "_1776_")
            + ":latest",
        }
        for r in raw
    ]
    chosen_terminal = select(terminal, stratum="category", count=20, seed=args.seed)
    chosen_swe = select(swe, stratum="repo", count=20, seed=args.seed)
    manifest = {
        "schema_version": 1,
        "name": "public-regression-40-v1",
        "seed": args.seed,
        "selection": (
            "One per stratum, then one additional case from largest strata; "
            "ties by stratum name; cases ordered by SHA256(seed:id). "
            "No outcome-based replacement."
        ),
        "terminalbench_dataset": "terminal-bench/terminal-bench-2-1",
        "terminalbench_population": len(terminal),
        "swebench_dataset": "SWE-bench/SWE-bench_Lite",
        "swebench_population": len(swe),
        "swebench_arrow_sha256": sha256(args.swe_arrow),
        "terminalbench": chosen_terminal,
        "swebench": chosen_swe,
    }
    chosen_ids = {r["id"] for r in chosen_swe}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.evaluator_output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    args.evaluator_output.write_text(json.dumps([r for r in raw if r["instance_id"] in chosen_ids]))
    args.evaluator_output.chmod(0o600)
    print(json.dumps({"terminalbench": 20, "swebench": 20, "manifest_sha256": sha256(args.output)}))


if __name__ == "__main__":
    main()
