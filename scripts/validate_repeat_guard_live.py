#!/usr/bin/env python3
"""Bounded live continuation after a scripted repeated-read prefix."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from bot.config import load_config, resolve_model_api_key
from bot.core import AgentRunner, RunRequest
from bot.core.context import ContextAssembler
from bot.core.events import EventBus
from bot.core.models import ModelEvent, ModelEventKind
from bot.execution import LocalExecutionTarget
from bot.policy import DefaultPolicyEngine
from bot.providers import ModelProvider, OpenAICompatibleProvider
from bot.providers.base import estimate_input_tokens
from bot.sessions import SQLiteSessionStore
from bot.skills import SkillCatalog, SkillManager
from bot.tools import ToolRegistry
from bot.tools.builtins import ReadFileTool


class SeededProvider(ModelProvider):
    def __init__(self, provider, budget, input_rate, output_rate):
        self.provider = provider
        self.budget = budget
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.turns = 0
        self.live_requests = 0

    def capabilities(self, model):
        return self.provider.capabilities(model)

    def estimate_input_tokens(self, request):
        return self.provider.estimate_input_tokens(request)

    async def stream(self, request):
        self.turns += 1
        if self.turns <= 4:
            yield ModelEvent(
                kind=ModelEventKind.TOOL_CALL_DELTA,
                tool_index=0,
                tool_call_id=f"seed-{self.turns}",
                tool_name="read_file",
                arguments_delta='{"path":"decoy.txt"}',
            )
            yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="tool_calls")
            return
        estimate = estimate_input_tokens(self.provider, request)
        if estimate is None:
            raise RuntimeError("Live validation requires an input token estimate")
        reservation = (
            max(10_000, estimate.budget_tokens) * self.input_rate
            + (request.max_output_tokens or 512) * self.output_rate
        ) / 1_000_000
        if self.budget["reserved_usd"] + reservation > self.budget["limit_usd"]:
            raise RuntimeError("Live continuation reservation budget exhausted")
        self.budget["reserved_usd"] += reservation
        self.live_requests += 1
        async for event in self.provider.stream(request):
            yield event


async def run(args):
    workspace = Path.cwd()
    config = load_config(workspace)
    if not config.model.input_cost_per_million or not config.model.output_cost_per_million:
        raise SystemExit("Configure input/output cost rates before live validation")
    key = resolve_model_api_key(config.model, workspace=workspace)
    if len(key) > 1 and key[0] == key[-1] and key[0] in "\"'":
        key = key[1:-1]
    provider = OpenAICompatibleProvider(
        base_url=config.model.base_url,
        api_key=key,
        timeout_seconds=30,
    )
    budget = {"limit_usd": args.max_cost_usd, "reserved_usd": 0.0}
    report = {
        "model": config.model.name,
        "method": "four_scripted_reads_then_live_continuation",
        "rates_from_local_config": {
            "input_per_million": config.model.input_cost_per_million,
            "output_per_million": config.model.output_cost_per_million,
        },
        "budget": budget,
        "limitations": [
            "The first four model responses are scripted; subsequent responses use the live model.",
            "These are small fixture continuations, "
            "not public benchmark success-rate measurements.",
            "Cost uses frozen local config rates, not a verified provider invoice.",
        ],
        "trials": [],
    }
    args.output.mkdir(parents=True, exist_ok=False)
    try:
        for repetition in range(args.repeat):
            for mode in ("observe", "enforce") if repetition % 2 == 0 else ("enforce", "observe"):
                root = (args.output / f"{repetition + 1}-{mode}").resolve()
                root.mkdir()
                answer = hashlib.sha256(f"repeat-guard-{repetition}".encode()).hexdigest()[:16]
                (root / "decoy.txt").write_text("No answer in this file.\n")
                (root / "answer.txt").write_text(answer + "\n")
                trial_config = config.model_copy(deep=True)
                trial_config.model.thinking = "disabled"
                trial_config.model.temperature = 0
                trial_config.model.max_output_tokens = 512
                trial_config.agent.progress.repeat_guard_mode = mode
                trial_config.agent.max_steps = 9
                trial_config.agent.max_wall_time_seconds = 120
                trial_config.agent.max_cost_usd = args.max_cost_usd
                trial_config.agent.model_request_retries = 0
                trial_config.memory.enabled = False
                trial_config.skills.auto_activate = False
                trial_config.subagents.enabled = False
                seeded = SeededProvider(
                    provider,
                    budget,
                    config.model.input_cost_per_million,
                    config.model.output_cost_per_million,
                )
                store = SQLiteSessionStore(root / "state.db")
                catalog = SkillCatalog(root / "skills")
                catalog.scan()
                registry = ToolRegistry()
                registry.register(ReadFileTool())
                target = LocalExecutionTarget()
                runner = AgentRunner(
                    config=trial_config,
                    workspace=root,
                    provider=seeded,
                    policy=DefaultPolicyEngine(trial_config.permissions, root),
                    tool_registry=registry,
                    execution_target=target,
                    skills=SkillManager(catalog),
                    store=store,
                    event_bus=EventBus([store]),
                    context=ContextAssembler(workspace=root, skill_catalog=catalog),
                )
                try:
                    result = await runner.run(
                        RunRequest(
                            prompt="读取工作区 answer.txt 中的校验码，最终只返回校验码。",
                        )
                    )
                    events = store.list_events(result.session_id)
                    calls = [
                        event["payload"] for event in events if event["type"] == "tool.requested"
                    ]
                    seed_ids = {
                        call["tool_call_id"]
                        for call in calls
                        if call.get("arguments", {}).get("path") == "decoy.txt"
                    }
                    results = [
                        event["payload"] for event in events if event["type"] == "tool.result"
                    ]
                    row = {
                        "mode": mode,
                        "repetition": repetition + 1,
                        "status": result.status,
                        "passed": result.final_text.strip() == answer,
                        "decoy_executions": sum(
                            row["tool_call_id"] in seed_ids and row.get("executed", True)
                            for row in results
                        ),
                        "blocked_calls": sum(row.get("executed") is False for row in results),
                        "live_requests": seeded.live_requests,
                        "input_tokens": result.input_tokens,
                        "output_tokens": result.output_tokens,
                        "estimated_cost_usd": result.cost_usd,
                        "termination_reason": result.termination_reason,
                    }
                    report["trials"].append(row)
                    (root / "events.json").write_text(
                        json.dumps(events, ensure_ascii=False, indent=2)
                    )
                    print(json.dumps(row, ensure_ascii=False), flush=True)
                    if result.status == "failed":
                        return report
                finally:
                    await target.aclose()
                    store.close()
    finally:
        (args.output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--repeat", type=int, default=2, choices=range(1, 4))
    parser.add_argument("--max-cost-usd", type=float, default=0.10)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 0 < args.max_cost_usd <= 0.50:
        parser.error("max-cost-usd must be in (0, 0.50]")
    report = asyncio.run(run(args))
    if len(report["trials"]) != args.repeat * 2 or not all(
        row["passed"] for row in report["trials"]
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
