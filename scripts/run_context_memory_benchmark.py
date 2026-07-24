from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from bot.config import load_config, resolve_api_key
from bot.evals.context_memory import (
    DeterministicMemoryProvider,
    benchmark_scenarios,
    run_benchmark_scenario,
)
from bot.providers import OpenAICompatibleProvider


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行长上下文 Episode 记忆压缩评测")
    parser.add_argument(
        "--provider",
        choices=("deterministic", "live"),
        default="deterministic",
        help="确定性 LLM double 或当前项目配置的真实模型",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(".bot/config.toml"),
        help="live 模式使用的配置文件",
    )
    parser.add_argument(
        "--max-episodes",
        type=int,
        help="每次 LLM 整合的最大 Episode 数；默认 deterministic=3，live=配置值",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        help="只运行指定场景 ID；可重复传入",
    )
    parser.add_argument("--output", type=Path, help="可选 JSON 结果文件")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    base_config = None
    if args.provider == "live":
        base_config = load_config(Path.cwd(), config_path=args.config)
        provider = OpenAICompatibleProvider(
            base_url=base_config.model.base_url,
            api_key=resolve_api_key(base_config.model.api_key_ref),
            timeout_seconds=base_config.model.timeout_seconds,
        )
        max_episodes = args.max_episodes or base_config.memory.max_episodes_per_run
    else:
        provider = DeterministicMemoryProvider()
        max_episodes = args.max_episodes or 3

    with tempfile.TemporaryDirectory(prefix="bot-context-memory-benchmark-") as raw_directory:
        root = Path(raw_directory)
        results = []
        scenarios = benchmark_scenarios()
        if args.scenario:
            requested = set(args.scenario)
            scenarios = tuple(item for item in scenarios if item.id in requested)
            missing = requested - {item.id for item in scenarios}
            if missing:
                raise ValueError(f"未知场景: {', '.join(sorted(missing))}")
        for scenario in scenarios:
            result = await run_benchmark_scenario(
                scenario,
                workspace=root / scenario.id,
                provider=provider,
                base_config=base_config,
                max_episodes_per_run=max_episodes,
            )
            results.append(result)
            print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False))

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                [result.model_dump(mode="json") for result in results],
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
