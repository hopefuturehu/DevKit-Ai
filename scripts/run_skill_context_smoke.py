"""Bounded live behavior smoke; synthetic fixtures only, not a cost benchmark."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from bot.compaction import ContextCompactor
from bot.config import load_config, resolve_model_api_key
from bot.config.models import AppConfig
from bot.core.agent import AgentRunner
from bot.core.context import CORE_POLICY, ContextAssembler
from bot.core.events import EventBus
from bot.core.models import ModelEventKind, RunRequest
from bot.execution.local import LocalExecutionTarget
from bot.policy import DefaultPolicyEngine
from bot.providers import OpenAICompatibleProvider
from bot.sessions import SQLiteSessionStore
from bot.skills import SkillCatalog, SkillManager
from bot.tools import ToolRegistry
from bot.tools.builtins import ReadFileTool


class RecordingProvider(OpenAICompatibleProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.records = []
        self.after_first = None

    async def stream(self, request):
        if len(self.records) >= 32:
            raise RuntimeError("smoke request cap reached")
        # Fix reasoning mode for both layouts and keep the smoke bounded.
        request = request.model_copy(update={"thinking": "disabled"})
        messages = self.serialized_messages(request)
        is_agent = request.messages[0].content == CORE_POLICY
        record = {
            "phase": "agent" if is_agent else "compaction",
            "tail_marker_messages": sum(
                "RULE_TAIL_947" in (m.get("content") or "") for m in messages
            ),
            "active_layer_messages": sum(m.name == "active_skill" for m in request.messages),
            "tool_names": [tool.name for tool in request.tools],
            "raw_usage": [],
        }
        self.records.append(record)
        if is_agent and self.after_first is not None:
            callback, self.after_first = self.after_first, None
            callback()
        start = monotonic()
        try:
            async for event in super().stream(request):
                if event.kind == ModelEventKind.USAGE:
                    record["raw_usage"].append(event.provider_metadata.get("raw_usage", {}))
                yield event
        finally:
            record["latency_seconds"] = round(monotonic() - start, 3)


async def run_case(base, provider, *, mode, scenario):
    with tempfile.TemporaryDirectory(prefix="bot-skill-smoke-") as temporary:
        root = Path(temporary)
        directory = root / "skills" / "audit"
        directory.mkdir(parents=True)
        filler = "核对每个来源，只报告已读取的事实，不编造缺失值。\n" * 450
        body = (
            "本 Skill 用于完成测试文档审计。先 read_file evidence.txt；"
            "读取成功后才能调用 read_file proof-947.txt，不要在同一批并行读取。\n"
            + (filler if scenario != "short_auto" else "")
            + "\nRULE_TAIL_947：最终答案必须恰好为 AUDIT_OK_947: <evidence值>/<proof值>。"
        )
        (directory / "SKILL.md").write_text(
            "---\nname: audit\ndescription: 完成测试文档审计并验证文件证据。\n---\n" + body
        )
        (root / "evidence.txt").write_text("evidence值 = cedar\n")
        (root / "proof-947.txt").write_text("proof值 = amber\n")
        config = AppConfig()
        config.model = base.model.model_copy(deep=True)
        config.model.temperature = 0
        config.model.max_output_tokens = 2048
        config.agent.max_steps = 6
        config.agent.max_wall_time_seconds = 180
        config.agent.model_request_retries = 0
        config.context.recent_conversation_tokens = 100
        config.context.compaction_max_output_tokens = 2048
        config.context.compaction_request_timeout_seconds = 45
        config.skills.context_mode = mode
        config.skills.path = str(root / "skills")
        catalog = SkillCatalog(root / "skills")
        catalog.scan()
        store = SQLiteSessionStore(root / "state.db")
        bus = EventBus([store])
        registry = ToolRegistry()
        registry.register(ReadFileTool())
        runner = AgentRunner(
            config=config,
            workspace=root,
            provider=provider,
            tool_registry=registry,
            policy=DefaultPolicyEngine(config.permissions, root),
            execution_target=LocalExecutionTarget(),
            skills=SkillManager(catalog),
            context=ContextAssembler(workspace=root, skill_catalog=catalog),
            store=store,
            event_bus=bus,
            context_compactor=ContextCompactor(
                config=config, provider=provider, store=store, event_bus=bus
            ),
        )
        session = store.create_session(root)
        start = len(provider.records)
        if scenario == "long_compaction":
            provider.after_first = lambda: runner.request_compaction(session)
        try:
            result = await runner.run(
                RunRequest(
                    session_id=session,
                    prompt="请使用 audit Skill 完成测试文档审计，按其要求读取证据并输出结果。",
                    explicit_skills=[] if scenario == "short_auto" else ["audit"],
                )
            )
            entries = store.load_positioned_messages(session)
            reads = [
                call.arguments.get("path")
                for entry in entries
                for call in entry.message.tool_calls
                if call.name == "read_file"
            ]
            restored = sum(
                bool(e.skill_delivery and e.skill_delivery.kind == "restored_body") for e in entries
            )
            task_passed = (
                result.status == "completed"
                and result.final_text.strip() == "AUDIT_OK_947: cedar/amber"
                and "evidence.txt" in reads
                and "proof-947.txt" in reads
            )
            recovery_passed = scenario != "long_compaction" or (
                bool(store.list_context_compactions(session)) and (mode == "legacy" or restored > 0)
            )
            continuation = await runner.run(
                RunRequest(
                    session_id=session, prompt="审计已结束。这是无关的新任务，请只回复 PLAIN_852。"
                )
            )
            records = provider.records[start:]
            outcome = {
                "mode": mode,
                "scenario": scenario,
                "fixture_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "task_passed": task_passed,
                "recovery_passed": recovery_passed,
                "unrelated_passed": continuation.final_text.strip() == "PLAIN_852",
                "final_text": result.final_text,
                "status": result.status,
                "restored_bodies": restored,
                "compactions": len(store.list_context_compactions(session)),
                "read_paths": reads,
                "requests": records,
            }
            outcome["passed"] = all(
                outcome[key] for key in ("task_passed", "recovery_passed", "unrelated_passed")
            )
            usages = [usage for record in records for usage in record["raw_usage"]]
            outcome["totals"] = {
                "input_tokens": sum(u.get("prompt_tokens", 0) for u in usages),
                "output_tokens": sum(u.get("completion_tokens", 0) for u in usages),
                "request_latency_seconds": round(sum(r["latency_seconds"] for r in records), 3),
            }
            for metric, field in [
                ("cached_input_tokens", "prompt_cache_hit_tokens"),
                ("uncached_input_tokens", "prompt_cache_miss_tokens"),
            ]:
                outcome["totals"][metric] = (
                    sum(u[field] for u in usages)
                    if usages and all(field in u for u in usages)
                    else None
                )
            return outcome
        finally:
            provider.after_first = None
            store.close()


async def run(args):
    base = load_config(workspace=args.workspace)
    provider = RecordingProvider(
        base_url=base.model.base_url,
        api_key=resolve_model_api_key(base.model, workspace=args.workspace),
        timeout_seconds=45,
    )
    report = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "model": base.model.name,
        "scope": "single-sample behavior smoke; cache warmth uncontrolled; no price claims",
        "cases": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for scenario in ["short_auto", "long_explicit", "long_compaction"]:
        for mode in ["legacy", "history"]:
            outcome = await run_case(base, provider, mode=mode, scenario=scenario)
            report["cases"].append(outcome)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(
                json.dumps({k: outcome[k] for k in ["mode", "scenario", "passed", "status"]}),
                flush=True,
            )
    return all(case["passed"] for case in report["cases"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="use the configured paid model API")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--output", type=Path, default=Path(".bot/benchmarks/skill-context-smoke.json")
    )
    args = parser.parse_args()
    if not args.live:
        parser.error("pass --live to execute the bounded API smoke")
    return 0 if asyncio.run(run(args)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
