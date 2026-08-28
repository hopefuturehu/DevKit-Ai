from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from statistics import median

from bot.config import load_config, resolve_model_api_key
from bot.evals.context_efficiency import (
    CONTEXT_EFFICIENCY_VARIANTS,
    FAST_CONTEXT_EFFICIENCY_PROFILE,
    ContextEfficiencyResult,
    ContextEfficiencyVariant,
    run_context_efficiency_benchmark,
    write_context_efficiency_comparison,
)
from bot.providers import OpenAICompatibleProvider


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="运行压缩、大结果外置、引用检索和一次性交付综合评测。",
    )
    parser.add_argument(
        "--provider",
        choices=("scripted", "live"),
        default="scripted",
    )
    parser.add_argument(
        "--variants",
        nargs="+",
        choices=CONTEXT_EFFICIENCY_VARIANTS,
        help="默认 scripted 运行全部变体，live 运行 raw/current。",
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--max-cost-usd", type=float, default=0.50)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".bot/benchmarks/context-efficiency"),
    )
    return parser


async def _run(args: argparse.Namespace) -> dict:
    profile = FAST_CONTEXT_EFFICIENCY_PROFILE.with_overrides(seed=args.seed)
    variants: tuple[ContextEfficiencyVariant, ...] = tuple(
        dict.fromkeys(
            args.variants
            or (("raw", "current") if args.provider == "live" else CONTEXT_EFFICIENCY_VARIANTS)
        )
    )
    base_config = (
        load_config(args.workspace, config_path=args.config) if args.provider == "live" else None
    )
    api_key = (
        resolve_model_api_key(base_config.model, workspace=args.workspace)
        if base_config is not None
        else None
    )
    attempts: list[dict] = []
    total_cost = 0.0
    for attempt in range(1, args.repeat + 1):
        results: list[ContextEfficiencyResult] = []
        attempt_dir = args.output if args.repeat == 1 else args.output / f"attempt-{attempt:02d}"
        for variant in variants:
            remaining = args.max_cost_usd - total_cost
            if args.provider == "live" and remaining <= 0:
                break
            provider = None
            attempt_config = None
            if base_config is not None:
                attempt_config = base_config.model_copy(deep=True)
                attempt_config.agent.max_cost_usd = remaining
                provider = OpenAICompatibleProvider(
                    base_url=attempt_config.model.base_url,
                    api_key=api_key or "",
                    timeout_seconds=attempt_config.model.timeout_seconds,
                )
            result = await run_context_efficiency_benchmark(
                profile=profile,
                workspace=attempt_dir / variant,
                variant=variant,
                provider=provider,
                base_config=attempt_config,
                require_reported_usage=args.provider == "live",
            )
            results.append(result)
            total_cost += float(result.summary["metrics"]["cost_usd"])
            print(
                json.dumps(
                    {
                        "attempt": attempt,
                        "variant": variant,
                        "passed": result.summary["quality"]["passed"],
                        "input_tokens": result.summary["metrics"]["input_tokens"],
                        "model_requests": result.summary["metrics"]["model_requests"],
                        "reference_tool_calls": result.summary["metrics"]["reference_tool_calls"],
                        "cost_usd": result.summary["metrics"]["cost_usd"],
                        "model_latency_seconds": result.summary["metrics"][
                            "model_latency_seconds"
                        ],
                        "artifacts": str(attempt_dir / variant / "artifacts"),
                    },
                    ensure_ascii=False,
                )
            )
        if not results:
            break
        comparison = write_context_efficiency_comparison(results, attempt_dir)
        attempts.append(
            {
                "attempt": attempt,
                "comparison": comparison,
                "variants": {result.variant: result.summary for result in results},
            }
        )
        if args.provider == "live" and total_cost >= args.max_cost_usd:
            break

    aggregate = _aggregate(args.provider, variants, attempts, total_cost, args.repeat)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "aggregate.json").write_text(
        json.dumps(aggregate, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return aggregate


def _aggregate(
    provider: str,
    variants: tuple[ContextEfficiencyVariant, ...],
    attempts: list[dict],
    total_cost: float,
    expected_attempts: int,
) -> dict:
    variant_inputs: dict[str, list[float]] = {variant: [] for variant in variants}
    variant_costs: dict[str, list[float]] = {variant: [] for variant in variants}
    variant_model_latencies: dict[str, list[float]] = {variant: [] for variant in variants}
    variant_p95_latencies: dict[str, list[float]] = {variant: [] for variant in variants}
    all_quality = True
    all_offline_acceptance = True
    for attempt in attempts:
        all_offline_acceptance &= bool(attempt["comparison"]["acceptance"]["passed"])
        for variant, summary in attempt["variants"].items():
            variant_inputs[variant].append(float(summary["metrics"]["input_tokens"]))
            variant_costs[variant].append(float(summary["metrics"]["cost_usd"]))
            variant_model_latencies[variant].append(
                float(summary["metrics"]["model_latency_seconds"])
            )
            variant_p95_latencies[variant].append(
                float(summary["metrics"]["model_request_latency_p95_seconds"])
            )
            all_quality &= bool(summary["quality"]["passed"])
    medians = {variant: median(values) for variant, values in variant_inputs.items() if values}
    median_costs = {
        variant: median(values) for variant, values in variant_costs.items() if values
    }
    median_model_latencies = {
        variant: median(values) for variant, values in variant_model_latencies.items() if values
    }
    median_p95_latencies = {
        variant: median(values) for variant, values in variant_p95_latencies.items() if values
    }
    live_token_savings = (
        medians.get("current", float("inf")) < medians.get("raw", float("-inf"))
        if {"raw", "current"} <= medians.keys()
        else None
    )
    acceptance = {
        "all_attempts_completed": len(attempts) == expected_attempts
        and all(set(attempt["variants"]) == set(variants) for attempt in attempts),
        "quality": all_quality and bool(attempts),
        "scripted_comparison": all_offline_acceptance if provider == "scripted" else None,
        "live_current_uses_fewer_input_tokens": (
            live_token_savings if provider == "live" else None
        ),
    }
    required = [value for value in acceptance.values() if value is not None]
    return {
        "schema_version": 1,
        "provider": provider,
        "attempts": len(attempts),
        "variants": list(variants),
        "median_input_tokens": medians,
        "median_cost_usd": median_costs,
        "median_model_latency_seconds": median_model_latencies,
        "median_model_request_p95_seconds": median_p95_latencies,
        "observed_effects": {
            "current_input_tokens_saved_vs_raw": (
                medians["raw"] - medians["current"]
                if {"raw", "current"} <= medians.keys()
                else None
            ),
            "current_cost_usd_saved_vs_raw": (
                median_costs["raw"] - median_costs["current"]
                if {"raw", "current"} <= median_costs.keys()
                else None
            ),
            "current_model_latency_delta_vs_raw_seconds": (
                median_model_latencies["current"] - median_model_latencies["raw"]
                if {"raw", "current"} <= median_model_latencies.keys()
                else None
            ),
        },
        "total_cost_usd": total_cost,
        "acceptance": {**acceptance, "passed": all(required)},
    }


def main() -> int:
    args = _parser().parse_args()
    if args.repeat < 1:
        raise SystemExit("--repeat 必须大于 0")
    if args.max_cost_usd <= 0:
        raise SystemExit("--max-cost-usd 必须大于 0")
    if args.provider == "live":
        if os.getenv("RUN_CONTEXT_EFFICIENCY_LIVE") != "1":
            raise SystemExit("真实 Provider 评测需要设置 RUN_CONTEXT_EFFICIENCY_LIVE=1")
        if args.repeat < 3:
            raise SystemExit("真实 Provider 评测至少需要 --repeat 3")
        selected = set(args.variants or ("raw", "current"))
        if not {"raw", "current"} <= selected:
            raise SystemExit("真实 Provider 评测必须同时包含 raw 和 current")
        config = load_config(args.workspace, config_path=args.config)
        if (
            config.model.input_cost_per_million is None
            or config.model.output_cost_per_million is None
        ):
            raise SystemExit("真实 Provider 评测必须配置模型输入和输出价格")
    aggregate = asyncio.run(_run(args))
    return 0 if aggregate["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
