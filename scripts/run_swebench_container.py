#!/usr/bin/env python3

import argparse
from pathlib import Path

from bot.evals.swebench import (
    DEFAULT_SWEBENCH_MAX_COST_USD,
    DEFAULT_SWEBENCH_MAX_STEPS,
    DEFAULT_SWEBENCH_MAX_WALL_TIME_SECONDS,
    load_instance,
    run_container_instance,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one SWE-bench instance inside Docker")
    parser.add_argument("instance_file", type=Path)
    parser.add_argument("--instance-id")
    parser.add_argument("--image", required=True)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-steps", type=int, default=DEFAULT_SWEBENCH_MAX_STEPS)
    parser.add_argument(
        "--max-wall-time-seconds",
        type=float,
        default=DEFAULT_SWEBENCH_MAX_WALL_TIME_SECONDS,
    )
    cost = parser.add_mutually_exclusive_group()
    cost.add_argument("--max-cost-usd", type=float, default=DEFAULT_SWEBENCH_MAX_COST_USD)
    cost.add_argument("--no-cost-limit", dest="max_cost_usd", action="store_const", const=None)
    args = parser.parse_args()

    instance = load_instance(args.instance_file.resolve(), args.instance_id)
    return run_container_instance(
        instance,
        image=args.image,
        project_root=args.project_root.resolve(),
        config_path=args.config.resolve(),
        output_path=args.output.resolve(),
        max_steps=args.max_steps,
        max_wall_time_seconds=args.max_wall_time_seconds,
        max_cost_usd=args.max_cost_usd,
    )


if __name__ == "__main__":
    raise SystemExit(main())
