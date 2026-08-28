from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from statistics import median

from bot.config import load_config, resolve_model_api_key
from bot.evals.context_retrieval_behavior import run_context_retrieval_behavior_suite
from bot.providers import OpenAICompatibleProvider


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="评测模型面对外置大结果时是否检索、检索对象与调用次数。"
    )
    parser.add_argument("--provider", choices=("scripted", "live"), default="scripted")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--max-cost-usd", type=float, default=0.50)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".bot/benchmarks/context-retrieval-behavior"),
    )
    return parser


async def _run(args: argparse.Namespace) -> dict:
    base_config = (
        load_config(args.workspace, config_path=args.config) if args.provider == "live" else None
    )
    api_key = (
        resolve_model_api_key(base_config.model, workspace=args.workspace)
        if base_config is not None
        else None
    )
    attempts = []
    total_cost = 0.0
    for attempt in range(1, args.repeat + 1):
        attempt_dir = args.output if args.repeat == 1 else args.output / f"attempt-{attempt:02d}"
        provider = None
        attempt_config = None
        if base_config is not None:
            remaining = args.max_cost_usd - total_cost
            if remaining <= 0:
                break
            attempt_config = base_config.model_copy(deep=True)
            attempt_config.agent.max_cost_usd = remaining
            provider = OpenAICompatibleProvider(
                base_url=attempt_config.model.base_url,
                api_key=api_key or "",
                timeout_seconds=attempt_config.model.timeout_seconds,
            )
        _, summary = await run_context_retrieval_behavior_suite(
            workspace=attempt_dir,
            provider_kind=args.provider,
            provider=provider,
            base_config=attempt_config,
            require_reported_usage=args.provider == "live",
        )
        attempts.append(summary)
        total_cost += float(summary["metrics"]["cost_usd"])
        print(
            json.dumps(
                {
                    "attempt": attempt,
                    "passed": summary["quality"]["passed"],
                    "input_tokens": summary["metrics"]["input_tokens"],
                    "reference_tool_calls": summary["metrics"]["reference_tool_calls"],
                    "empty_query_results": summary["metrics"]["empty_query_results"],
                    "cost_usd": summary["metrics"]["cost_usd"],
                    "artifacts": str(attempt_dir),
                },
                ensure_ascii=False,
            )
        )
    aggregates = {
        key: median(float(item["metrics"][key]) for item in attempts)
        for key in ("input_tokens", "reference_tool_calls", "model_requests")
    } if attempts else {}
    acceptance = {
        "all_attempts_completed": len(attempts) == args.repeat,
        "all_cases_passed": bool(attempts)
        and all(item["quality"]["passed"] for item in attempts),
    }
    result = {
        "schema_version": 1,
        "provider": args.provider,
        "attempts": len(attempts),
        "median_metrics": aggregates,
        "total_cost_usd": total_cost,
        "acceptance": {**acceptance, "passed": all(acceptance.values())},
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "aggregate.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def main() -> int:
    args = _parser().parse_args()
    if args.repeat < 1:
        raise SystemExit("--repeat 必须大于 0")
    if args.max_cost_usd <= 0:
        raise SystemExit("--max-cost-usd 必须大于 0")
    if args.provider == "live":
        if os.getenv("RUN_CONTEXT_RETRIEVAL_LIVE") != "1":
            raise SystemExit("真实 Provider 评测需要设置 RUN_CONTEXT_RETRIEVAL_LIVE=1")
        if args.repeat < 3:
            raise SystemExit("真实 Provider 评测至少需要 --repeat 3")
        config = load_config(args.workspace, config_path=args.config)
        if (
            config.model.input_cost_per_million is None
            or config.model.output_cost_per_million is None
        ):
            raise SystemExit("真实 Provider 评测必须配置模型输入和输出价格")
    result = asyncio.run(_run(args))
    return 0 if result["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
