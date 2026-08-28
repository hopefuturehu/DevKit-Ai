from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from bot.evals.context_full_stack_soak import (
    context_full_stack_profile,
    run_context_full_stack_soak,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="运行 AGENTS、memory、skill、schema、压缩、外置、resume 和 fork 组合评测。"
    )
    parser.add_argument("--suite", choices=("fast", "soak"), default="fast")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".bot/benchmarks/context-full-stack"),
    )
    parser.add_argument("--turns", type=int)
    parser.add_argument("--tool-output-chars", type=int)
    parser.add_argument("--dummy-tools", type=int)
    return parser


async def _run(args: argparse.Namespace):
    profile = context_full_stack_profile(args.suite).with_overrides(
        logical_turns=args.turns,
        tool_output_chars=args.tool_output_chars,
        dummy_tool_count=args.dummy_tools,
    )
    result = await run_context_full_stack_soak(
        profile=profile,
        workspace=args.output / args.suite,
    )
    print(
        json.dumps(
            {
                "suite": args.suite,
                "passed": result.summary["quality"]["passed"],
                "turns": result.summary["metrics"]["logical_turns"],
                "input_tokens": result.summary["metrics"]["input_tokens"],
                "compactions": result.summary["metrics"]["compactions"],
                "unique_context_blobs": result.summary["metrics"]["unique_context_blobs"],
                "artifacts": str(result.workspace / "artifacts"),
            },
            ensure_ascii=False,
        )
    )
    return result


def main() -> int:
    args = _parser().parse_args()
    if args.suite == "soak" and os.getenv("RUN_CONTEXT_FULL_STACK_SOAK") != "1":
        raise SystemExit("soak 需要显式设置 RUN_CONTEXT_FULL_STACK_SOAK=1")
    if args.turns is not None and args.turns < 6:
        raise SystemExit("--turns 至少为 6")
    if args.tool_output_chars is not None and args.tool_output_chars < 512:
        raise SystemExit("--tool-output-chars 至少为 512")
    if args.dummy_tools is not None and args.dummy_tools < 1:
        raise SystemExit("--dummy-tools 必须大于 0")
    result = asyncio.run(_run(args))
    return 0 if result.summary["quality"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
