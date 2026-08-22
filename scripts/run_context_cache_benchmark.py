from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from bot.evals.context_cache import (
    ContextCacheBenchmarkResult,
    context_cache_profile,
    run_context_cache_benchmark,
    write_context_cache_comparison,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("离线运行长任务上下文缓存 benchmark；不访问网络，也不调用真实模型。")
    )
    parser.add_argument(
        "--suite",
        choices=("fast", "soak", "all"),
        default="fast",
        help="fast 适合本地/CI；soak 使用生产级 131072 token 窗口。",
    )
    parser.add_argument(
        "--variants",
        nargs="+",
        choices=("current", "no-compaction"),
        default=("current", "no-compaction"),
        help="current 运行当前压缩机制；no-compaction 是同工作负载反事实基线。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".bot/benchmarks/context-cache"),
        help="评测工作区和 JSONL/CSV 产物目录。",
    )
    parser.add_argument(
        "--turns",
        type=int,
        help="覆盖逻辑轮数（探索模式会关闭最小压缩次数门禁）。",
    )
    parser.add_argument("--tool-output-chars", type=int, help="覆盖每轮确定性 Tool 输出字符数。")
    parser.add_argument(
        "--stable-memory-chars",
        type=int,
        help="覆盖每次请求都应保留的确定性稳定长期记忆字符数。",
    )
    parser.add_argument("--minimum-cacheable-tokens", type=int, help="覆盖缓存最小前缀 token 数。")
    parser.add_argument("--seed", type=int, help="覆盖确定性工作负载 seed。")
    parser.add_argument(
        "--cached-input-cost-ratio",
        type=float,
        default=0.10,
        help="缓存输入 token 相对普通输入的成本，默认 0.10。",
    )
    parser.add_argument(
        "--output-cost-ratio",
        type=float,
        default=1.0,
        help="输出 token 相对普通输入的成本，默认 1.0。",
    )
    return parser


async def _run(args: argparse.Namespace) -> list[ContextCacheBenchmarkResult]:
    suites = ("fast", "soak") if args.suite == "all" else (args.suite,)
    results: list[ContextCacheBenchmarkResult] = []
    for suite in suites:
        profile = context_cache_profile(suite).with_overrides(
            logical_turns=args.turns,
            tool_output_chars=args.tool_output_chars,
            stable_memory_chars=args.stable_memory_chars,
            minimum_cacheable_tokens=args.minimum_cacheable_tokens,
            seed=args.seed,
        )
        for variant in dict.fromkeys(args.variants):
            workspace = args.output / suite / variant
            result = await run_context_cache_benchmark(
                profile=profile,
                workspace=workspace,
                variant=variant,
                cached_input_cost_ratio=args.cached_input_cost_ratio,
                output_cost_ratio=args.output_cost_ratio,
            )
            results.append(result)
            metrics = result.summary["metrics"]
            compaction = result.summary["compaction"]
            print(
                json.dumps(
                    {
                        "suite": suite,
                        "variant": variant,
                        "passed": result.summary["quality"]["passed"],
                        "compactions": compaction["completed"],
                        "cache_hit_ratio": metrics["weighted_cache_hit_ratio"],
                        "cost_per_turn": metrics["cost_per_logical_turn"],
                        "artifacts": str(workspace / "artifacts"),
                    },
                    ensure_ascii=False,
                )
            )
    write_context_cache_comparison(results, args.output)
    return results


def main() -> int:
    args = _parser().parse_args()
    if args.turns is not None and args.turns < 1:
        raise SystemExit("--turns 必须大于 0")
    if args.tool_output_chars is not None and args.tool_output_chars < 256:
        raise SystemExit("--tool-output-chars 至少为 256")
    if args.stable_memory_chars is not None and args.stable_memory_chars < 0:
        raise SystemExit("--stable-memory-chars 不能小于 0")
    if args.minimum_cacheable_tokens is not None and args.minimum_cacheable_tokens < 0:
        raise SystemExit("--minimum-cacheable-tokens 不能小于 0")
    results = asyncio.run(_run(args))
    return 0 if all(result.summary["quality"]["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
