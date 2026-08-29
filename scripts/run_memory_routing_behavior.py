from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from statistics import median
from typing import Any

from bot.config import load_config, resolve_model_api_key
from bot.evals.memory_routing_behavior import (
    MEMORY_ROUTING_SCENARIOS,
    MEMORY_ROUTING_VARIANTS,
    MemoryRoutingBehaviorResult,
    MemoryRoutingScenario,
    MemoryRoutingVariant,
    run_memory_routing_behavior_case,
    summarize_memory_routing_results,
    write_memory_routing_comparison,
)
from bot.providers import OpenAICompatibleProvider


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="成对评测 eager 与 on-demand Memory Router 的质量、成本和延迟。"
    )
    parser.add_argument("--provider", choices=("scripted", "live"), default="scripted")
    parser.add_argument(
        "--variants",
        nargs="+",
        choices=MEMORY_ROUTING_VARIANTS,
        default=list(MEMORY_ROUTING_VARIANTS),
    )
    parser.add_argument(
        "--scenarios",
        nargs="+",
        choices=tuple(item.name for item in MEMORY_ROUTING_SCENARIOS),
        default=[item.name for item in MEMORY_ROUTING_SCENARIOS],
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--max-cost-usd", type=float, default=0.50)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".bot/benchmarks/memory-routing-behavior"),
    )
    return parser


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    variants = tuple(dict.fromkeys(args.variants))
    scenario_by_name = {scenario.name: scenario for scenario in MEMORY_ROUTING_SCENARIOS}
    scenarios = tuple(scenario_by_name[name] for name in dict.fromkeys(args.scenarios))
    base_config = (
        load_config(args.workspace, config_path=args.config) if args.provider == "live" else None
    )
    api_key = (
        resolve_model_api_key(base_config.model, workspace=args.workspace)
        if base_config is not None
        else None
    )
    provider = (
        OpenAICompatibleProvider(
            base_url=base_config.model.base_url,
            api_key=api_key or "",
            timeout_seconds=base_config.model.timeout_seconds,
        )
        if base_config is not None
        else None
    )
    attempts: list[dict[str, Any]] = []
    total_cost = 0.0
    for attempt in range(1, args.repeat + 1):
        attempt_dir = args.output if args.repeat == 1 else args.output / f"attempt-{attempt:02d}"
        attempt_results: list[MemoryRoutingBehaviorResult] = []
        variant_order = variants if attempt % 2 else tuple(reversed(variants))
        offset = (attempt - 1) % len(scenarios)
        scenario_order = scenarios[offset:] + scenarios[:offset]
        for variant in variant_order:
            for scenario in scenario_order:
                remaining = args.max_cost_usd - total_cost
                if args.provider == "live" and remaining <= 0:
                    break
                attempt_config = None
                if base_config is not None:
                    attempt_config = base_config.model_copy(deep=True)
                    attempt_config.agent.max_cost_usd = remaining
                result = await run_memory_routing_behavior_case(
                    scenario=scenario,
                    variant=variant,
                    workspace=attempt_dir / variant / scenario.name,
                    provider_kind=args.provider,
                    provider=provider,
                    base_config=attempt_config,
                    require_reported_usage=args.provider == "live",
                )
                attempt_results.append(result)
                total_cost += float(result.summary["metrics"]["cost_usd"])
                print(
                    json.dumps(
                        {
                            "attempt": attempt,
                            "variant": variant,
                            "scenario": scenario.name,
                            "passed": result.summary["quality"]["passed"],
                            "router_decisions": result.summary["observed"]["router_decisions"],
                            "requested_tools": result.summary["observed"]["requested_tool_names"],
                            "input_tokens": result.summary["metrics"]["input_tokens"],
                            "model_requests": result.summary["metrics"]["model_requests"],
                            "cost_usd": result.summary["metrics"]["cost_usd"],
                            "end_to_end_seconds": result.summary["metrics"]["end_to_end_seconds"],
                            "final_text": result.summary["observed"]["final_text"],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            if args.provider == "live" and total_cost >= args.max_cost_usd:
                break
        attempt_summary = summarize_memory_routing_results(
            attempt_results,
            provider_kind=args.provider,
        )
        write_memory_routing_comparison(attempt_results, attempt_summary, attempt_dir)
        attempts.append(
            {
                "attempt": attempt,
                "summary": attempt_summary,
                "cases": {
                    f"{result.variant}/{result.scenario.name}": result.summary
                    for result in attempt_results
                },
            }
        )
        if args.provider == "live" and total_cost >= args.max_cost_usd:
            break

    aggregate = _aggregate(
        provider_kind=args.provider,
        variants=variants,
        scenarios=scenarios,
        attempts=attempts,
        expected_attempts=args.repeat,
        total_cost=total_cost,
    )
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "aggregate.json").write_text(
        json.dumps(aggregate, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return aggregate


def _aggregate(
    *,
    provider_kind: str,
    variants: tuple[MemoryRoutingVariant, ...],
    scenarios: tuple[MemoryRoutingScenario, ...],
    attempts: list[dict[str, Any]],
    expected_attempts: int,
    total_cost: float,
) -> dict[str, Any]:
    expected_keys = {f"{variant}/{scenario.name}" for variant in variants for scenario in scenarios}
    case_metrics: dict[str, dict[str, list[float]]] = {}
    case_passes: dict[str, list[bool]] = {}
    case_gates: dict[str, dict[str, list[bool]]] = {}
    case_diagnostics: dict[str, dict[str, list[bool]]] = {}
    hypothesis_values: dict[str, list[bool]] = {}
    paired_deltas: dict[str, dict[str, list[float]]] = {}
    for attempt in attempts:
        for key, summary in attempt["cases"].items():
            case_passes.setdefault(key, []).append(bool(summary["quality"]["passed"]))
            for name, value in summary["quality"].get("gates", {}).items():
                case_gates.setdefault(key, {}).setdefault(name, []).append(bool(value))
            for name, value in summary["quality"].get("diagnostics", {}).items():
                case_diagnostics.setdefault(key, {}).setdefault(name, []).append(bool(value))
            for metric, value in summary["metrics"].items():
                if isinstance(value, (int, float)):
                    case_metrics.setdefault(key, {}).setdefault(metric, []).append(float(value))
        for name, value in attempt["summary"]["hypotheses"].items():
            hypothesis_values.setdefault(name, []).append(bool(value))
        for scenario, deltas in attempt["summary"]["paired_deltas_on_demand_minus_eager"].items():
            for metric, value in deltas.items():
                paired_deltas.setdefault(scenario, {}).setdefault(metric, []).append(float(value))

    completed = len(attempts) == expected_attempts and all(
        set(attempt["cases"]) == expected_keys for attempt in attempts
    )
    router_keys = {key for key in expected_keys if key.startswith("on_demand/")}
    router_passed = bool(attempts) and all(
        all(case_passes.get(key, [])) and len(case_passes.get(key, [])) == expected_attempts
        for key in router_keys
    )
    hypothesis_rates = {
        name: sum(values) / len(values) for name, values in hypothesis_values.items() if values
    }
    hypotheses_reproduced = bool(hypothesis_rates) and all(
        rate == 1 for rate in hypothesis_rates.values()
    )
    acceptance = {
        "all_attempts_completed": completed,
        "router_quality_passed": router_passed,
        "hypotheses_reproduced_in_every_attempt": hypotheses_reproduced,
    }
    return {
        "schema_version": 1,
        "benchmark": "memory-routing-behavior-live-comparison",
        "provider": provider_kind,
        "attempts": len(attempts),
        "variants": list(variants),
        "scenarios": [scenario.name for scenario in scenarios],
        "median_case_metrics": {
            key: {metric: median(values) for metric, values in metrics.items()}
            for key, metrics in case_metrics.items()
        },
        "case_pass_rates": {
            key: sum(values) / len(values) for key, values in case_passes.items() if values
        },
        "case_gate_rates": {
            key: {name: sum(values) / len(values) for name, values in gates.items() if values}
            for key, gates in case_gates.items()
        },
        "case_diagnostic_rates": {
            key: {name: sum(values) / len(values) for name, values in diagnostics.items() if values}
            for key, diagnostics in case_diagnostics.items()
        },
        "hypothesis_pass_rates": hypothesis_rates,
        "median_paired_deltas_on_demand_minus_eager": {
            scenario: {metric: median(values) for metric, values in metrics.items()}
            for scenario, metrics in paired_deltas.items()
        },
        "total_cost_usd": total_cost,
        "acceptance": {**acceptance, "passed": all(acceptance.values())},
    }


def main() -> int:
    args = _parser().parse_args()
    if args.repeat < 1:
        raise SystemExit("--repeat 必须大于 0")
    if args.max_cost_usd <= 0:
        raise SystemExit("--max-cost-usd 必须大于 0")
    if args.provider == "live":
        if os.getenv("RUN_MEMORY_ROUTING_LIVE") != "1":
            raise SystemExit("真实 Provider 评测需要设置 RUN_MEMORY_ROUTING_LIVE=1")
        if args.repeat < 3:
            raise SystemExit("真实 Provider 评测至少需要 --repeat 3")
        if set(args.variants) != set(MEMORY_ROUTING_VARIANTS):
            raise SystemExit("真实 Provider A/B 必须同时包含 eager 和 on_demand")
        if set(args.scenarios) != {item.name for item in MEMORY_ROUTING_SCENARIOS}:
            raise SystemExit("真实 Provider A/B 必须包含四个预注册 scenario")
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
