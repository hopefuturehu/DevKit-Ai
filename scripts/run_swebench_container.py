#!/usr/bin/env python3

import argparse
from pathlib import Path

from bot.evals.swebench import load_instance, run_container_instance


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one SWE-bench instance inside Docker")
    parser.add_argument("instance_file", type=Path)
    parser.add_argument("--instance-id")
    parser.add_argument("--image", required=True)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    instance = load_instance(args.instance_file.resolve(), args.instance_id)
    return run_container_instance(
        instance,
        image=args.image,
        project_root=args.project_root.resolve(),
        config_path=args.config.resolve(),
        output_path=args.output.resolve(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
