from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from bot.config import load_config, resolve_model_api_key
from bot.evals.compaction_effectiveness import (
    ScriptedCompactionProvider,
    load_replay_entries,
    run_compaction_effectiveness,
    synthetic_replay_entries,
)
from bot.providers import OpenAICompatibleProvider


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="回放上下文压缩并输出可靠性、质量、延迟和请求级产物。",
    )
    parser.add_argument(
        "--provider",
        choices=("scripted", "live"),
        default="scripted",
    )
    parser.add_argument(
        "--scenario",
        choices=(
            "success",
            "length",
            "format",
            "rate-limit",
            "context-overflow",
            "authentication",
        ),
        default="success",
    )
    parser.add_argument(
        "--variant",
        choices=("a0", "a1", "a2a", "a2b", "hybrid-20k", "hybrid-24k", "hybrid-48k"),
        default="a2a",
    )
    parser.add_argument("--summary-model", default="deepseek-v4-flash")
    parser.add_argument("--transcript", type=Path)
    parser.add_argument("--through-position", type=int)
    parser.add_argument("--turns", type=int, default=12)
    parser.add_argument("--tool-output-chars", type=int, default=14_000)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--max-cost-usd", type=float, default=0.50)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".bot/benchmarks/compaction-effectiveness"),
    )
    return parser


def _variant_overrides(variant: str, summary_model: str) -> dict:
    context: dict[str, object] = {
        "compaction_transport_retry_backoff_seconds": 0,
        "compaction_command_max_cost_usd": None,
    }
    if variant == "a0":
        context.update(
            {
                "compaction_model": None,
                "compaction_summary_target_tokens": 8_000,
                "compaction_summary_tokens": 8_000,
                "compaction_max_output_tokens": 8_192,
                "compaction_source_refs": "item",
                "compaction_thinking": "provider_default",
            }
        )
    elif variant == "a1":
        context.update(
            {
                "compaction_model": summary_model,
                "compaction_summary_target_tokens": 8_000,
                "compaction_summary_tokens": 8_000,
                "compaction_max_output_tokens": 8_192,
                "compaction_source_refs": "item",
                "compaction_thinking": "provider_default",
            }
        )
    elif variant == "a2a":
        context.update(
            {
                "compaction_model": summary_model,
                "compaction_summary_target_tokens": 3_000,
                "compaction_summary_tokens": 4_000,
                "compaction_max_output_tokens": 8_192,
                "compaction_source_refs": "item",
                "compaction_thinking": "provider_default",
            }
        )
    elif variant == "a2b":
        context.update(
            {
                "compaction_model": summary_model,
                "compaction_summary_target_tokens": 4_000,
                "compaction_summary_tokens": 6_000,
                "compaction_max_output_tokens": 8_192,
                "compaction_source_refs": "item",
                "compaction_thinking": "provider_default",
            }
        )
    else:
        tail = int(variant.removeprefix("hybrid-").removesuffix("k")) * 1_000
        context.update(
            {
                "compaction_model": summary_model,
                "compaction_summary_target_tokens": 3_000,
                "compaction_summary_tokens": 4_000,
                "compaction_max_output_tokens": 8_192,
                "compaction_source_refs": "range",
                "compaction_thinking": "disabled",
                "recent_conversation_tokens": tail,
                "compaction_min_recent_user_turns": 3,
            }
        )
    return {"context": context}


async def _run(args: argparse.Namespace) -> int:
    if args.repeat < 1:
        raise SystemExit("--repeat 必须大于 0")
    if args.max_cost_usd <= 0:
        raise SystemExit("--max-cost-usd 必须大于 0")
    if args.provider == "live" and os.getenv("RUN_CONTEXT_COMPACTION_LIVE") != "1":
        raise SystemExit("真实 Provider 评测需要显式设置 RUN_CONTEXT_COMPACTION_LIVE=1")
    config = load_config(
        args.workspace,
        config_path=args.config,
        overrides=_variant_overrides(args.variant, args.summary_model),
    )
    if not config.model.name:
        config.model.name = "scripted-agent-model"
    if args.provider == "live" and (
        config.model.input_cost_per_million is None
        or config.model.output_cost_per_million is None
    ):
        raise SystemExit(
            "真实 Provider 评测必须配置 model.input_cost_per_million 和 "
            "model.output_cost_per_million"
        )
    entries = (
        load_replay_entries(args.transcript)
        if args.transcript is not None
        else synthetic_replay_entries(
            turns=args.turns,
            tool_output_chars=args.tool_output_chars,
        )
    )
    results = []
    total_cost = 0.0
    for attempt in range(1, args.repeat + 1):
        remaining_cost = args.max_cost_usd - total_cost
        if remaining_cost <= 0:
            break
        attempt_config = config.model_copy(deep=True)
        if args.provider == "live":
            attempt_config.context.compaction_command_max_cost_usd = remaining_cost
        if args.provider == "live":
            api_key = resolve_model_api_key(config.model, workspace=args.workspace)
            provider = OpenAICompatibleProvider(
                base_url=config.model.base_url,
                api_key=api_key,
                timeout_seconds=config.model.timeout_seconds,
            )
        else:
            provider = ScriptedCompactionProvider(args.scenario)
        result = await run_compaction_effectiveness(
            config=attempt_config,
            provider=provider,
            entries=entries,
            workspace=args.output / args.variant / f"attempt-{attempt:02d}",
            scenario=args.scenario,
            through_position=args.through_position,
            expect_success=args.scenario != "authentication",
            preserve_recent_tail=args.variant.startswith("hybrid-"),
        )
        results.append(result.summary)
        request_cost = sum(
            float(row.get("payload", {}).get("cost_usd") or 0)
            for row in _read_jsonl(result.artifacts / "requests.jsonl")
            if row.get("type")
            in {
                "context.compaction.request.completed",
                "context.compaction.request.failed",
            }
        )
        total_cost += request_cost
        print(
            json.dumps(
                {
                    "attempt": attempt,
                    "variant": args.variant,
                    "scenario": args.scenario,
                    "passed": result.summary["quality"]["passed"],
                    "requests": result.summary["requests"]["total"],
                    "duration_ms": result.summary["result"]["duration_ms"],
                    "cost_usd": request_cost,
                    "artifacts": str(result.artifacts),
                },
                ensure_ascii=False,
            )
        )
        if total_cost >= args.max_cost_usd:
            break
    aggregate = {
        "schema_version": 1,
        "provider": args.provider,
        "variant": args.variant,
        "scenario": args.scenario,
        "attempts": len(results),
        "passed": sum(item["quality"]["passed"] for item in results),
        "success_rate": sum(item["result"]["compacted"] for item in results)
        / max(1, len(results)),
        "duration_ms": {
            "p50": _percentile(
                [float(item["result"]["duration_ms"]) for item in results],
                0.50,
            ),
            "p95": _percentile(
                [float(item["result"]["duration_ms"]) for item in results],
                0.95,
            ),
            "max": max(
                (float(item["result"]["duration_ms"]) for item in results),
                default=0,
            ),
        },
        "completion_to_visible_ratio_p95": _completion_ratio_p95(results),
        "total_cost_usd": total_cost,
        "all_quality_gates_passed": all(item["quality"]["passed"] for item in results),
    }
    aggregate["acceptance"] = {
        "quality": aggregate["all_quality_gates_passed"],
        "success_rate_95pct": (
            aggregate["success_rate"] >= 0.95 if len(results) >= 20 else None
        ),
        "p50_at_most_30s": (
            aggregate["duration_ms"]["p50"] <= 30_000
            if args.provider == "live" and len(results) >= 3
            else None
        ),
        "p95_at_most_60s": (
            aggregate["duration_ms"]["p95"] <= 60_000
            if args.provider == "live" and len(results) >= 20
            else None
        ),
        "hard_limit_90s": (
            aggregate["duration_ms"]["max"] <= 90_000
            if args.provider == "live"
            else None
        ),
        "completion_ratio_at_most_2": (
            aggregate["completion_to_visible_ratio_p95"] <= 2
            if args.provider == "live" and len(results) >= 3
            else None
        ),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / f"{args.variant}-{args.scenario}-summary.json").write_text(
        json.dumps(aggregate, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    required_gates = [
        value for value in aggregate["acceptance"].values() if value is not None
    ]
    return 0 if all(required_gates) else 1


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * quantile + 0.999999)))
    return ordered[index]


def _completion_ratio_p95(results: list[dict]) -> float:
    ratios: list[float] = []
    for result in results:
        output = float(result["result"]["output_tokens"])
        visible = float(result["result"]["summary_tokens"])
        ratios.append(output / max(1, visible))
    return _percentile(ratios, 0.95)


def main() -> int:
    return asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
