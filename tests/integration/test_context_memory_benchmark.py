from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from bot.compaction import ContextCompactor
from bot.config.models import AppConfig
from bot.core import AgentRunner, RunRequest
from bot.core.context import ContextAssembler
from bot.core.events import EventBus, EventType, MemoryEventSink
from bot.core.models import ChatMessage, ModelEvent, ModelEventKind, ModelRequest
from bot.evals.context_memory import (
    DeterministicMemoryProvider,
    benchmark_scenarios,
    build_benchmark_config,
    run_benchmark_scenario,
    seed_long_context,
)
from bot.execution import LocalExecutionTarget
from bot.memory import MemoryConsolidator
from bot.policy import DefaultPolicyEngine
from bot.sessions import SQLiteSessionStore
from bot.skills import SkillCatalog, SkillManager
from bot.tools import ToolRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", benchmark_scenarios(), ids=lambda item: item.id)
async def test_long_context_scenarios_preserve_facts_and_reduce_projection(
    tmp_path: Path,
    scenario,
) -> None:
    result = await run_benchmark_scenario(
        scenario,
        workspace=tmp_path / scenario.id,
        provider=DeterministicMemoryProvider(),
        max_episodes_per_run=3,
    )

    assert result.passed, result.failures
    assert result.raw_messages >= 36
    assert result.batches >= 3
    assert result.fact_recall == 1
    assert result.retrieval_recall == 1
    assert result.raw_preserved
    assert result.source_traceable
    assert result.snapshot_free


@pytest.mark.asyncio
async def test_long_context_consolidation_adapts_after_middle_batch_failure(
    tmp_path: Path,
) -> None:
    scenario = benchmark_scenarios()[0]
    workspace = tmp_path / "failure-recovery"
    workspace.mkdir()
    config = build_benchmark_config(
        workspace / "state.db",
        max_episodes_per_run=3,
    )
    store = SQLiteSessionStore(workspace / "state.db")
    provider = DeterministicMemoryProvider(fail_calls={2})
    consolidator = MemoryConsolidator(
        config=config,
        workspace=workspace,
        provider=provider,
        store=store,
        event_bus=EventBus([store]),
    )
    session_id, original_digest = seed_long_context(
        store,
        workspace=workspace,
        scenario=scenario,
    )
    store.seal_memory_episodes(session_id)

    recovered = await consolidator.consolidate_all(session_id, trigger="failure-test")

    assert recovered.consolidated
    assert recovered.reason is None
    assert provider.calls == 6
    assert store.consolidated_memory_cursor(session_id) == 40
    assert not store.list_memory_episodes(session_id, status="pending")
    assert any(item["status"] == "failed" for item in store.list_memory_consolidations(session_id))
    assert original_digest == _digest(store, session_id)
    assert store.latest_context_snapshot(session_id) is None
    store.close()


@pytest.mark.asyncio
async def test_long_context_terminal_failure_reports_partial_run_as_incomplete(
    tmp_path: Path,
) -> None:
    scenario = benchmark_scenarios()[0]
    workspace = tmp_path / "terminal-failure"
    workspace.mkdir()
    config = build_benchmark_config(
        workspace / "state.db",
        max_episodes_per_run=3,
    )
    store = SQLiteSessionStore(workspace / "state.db")
    provider = DeterministicMemoryProvider(fail_calls={2, 3, 4})
    consolidator = MemoryConsolidator(
        config=config,
        workspace=workspace,
        provider=provider,
        store=store,
        event_bus=EventBus([store]),
    )
    session_id, original_digest = seed_long_context(
        store,
        workspace=workspace,
        scenario=scenario,
    )

    result = await consolidator.consolidate_all(session_id, trigger="terminal-failure-test")

    assert not result.consolidated
    assert result.reason == "consolidation_failed"
    assert result.episodes_consolidated == 3
    assert store.consolidated_memory_cursor(session_id) == 12
    assert len(store.list_memory_episodes(session_id, status="pending")) == 7
    assert original_digest == _digest(store, session_id)
    store.close()


class _AgentAndCompactionProvider(DeterministicMemoryProvider):
    def __init__(self) -> None:
        super().__init__()
        self.agent_requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        if request.messages[-1].name == "context_compaction_input":
            payload = json.loads(request.messages[-1].content or "{}")
            start, end = payload["covered_range"]
            messages = payload.get("raw_messages") or payload.get("new_messages") or []
            facts: list[tuple[int, str]] = []
            for item in messages:
                for line in str(item.get("content") or "").splitlines():
                    if line.startswith("[MEMORY_FACT "):
                        facts.append((int(item["position"]), line))
            reference = f"[m:{start}-{end}]"
            critical = (
                "\n".join(f"- {content} [m:{position}]" for position, content in facts)
                or f"- 当前原始范围没有额外事实。 {reference}"
            )
            summary = "\n\n".join(
                [
                    f"# Goal\n- 继续完成长时间迁移任务。 {reference}",
                    f"# Constraints\n- 保留已经确认的约束。 {reference}",
                    f"# Progress\n- 已压缩较早阶段，近期阶段保持原文。 {reference}",
                    f"# Key Decisions\n- 采用单一活动摘要。 {reference}",
                    f"# Relevant Files\n- 迁移产物路径保留在事实清单中。 {reference}",
                    f"# Failures\n- 失败阶段保持原状态，不推断成功。 {reference}",
                    f"# Next Steps\n- 依据近期原文继续下一阶段。 {reference}",
                    f"# Critical Context\n{critical}",
                ]
            )
            yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text=summary)
            yield ModelEvent(
                kind=ModelEventKind.USAGE,
                input_tokens=1_000,
                output_tokens=300,
            )
            yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")
            return
        self.agent_requests.append(request)
        yield ModelEvent(
            kind=ModelEventKind.TEXT_DELTA,
            text="继续执行长任务，已读取可恢复的单摘要上下文。",
        )
        yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")


@pytest.mark.asyncio
async def test_recoverable_compaction_keeps_one_summary_recent_tail_and_raw_source(
    tmp_path: Path,
) -> None:
    scenario = benchmark_scenarios()[0]
    workspace = tmp_path / "recoverable-pressure"
    workspace.mkdir()
    config = AppConfig.model_validate(
        {
            "model": {
                "base_url": "https://unused",
                "name": "benchmark-model",
                "context_window_tokens": 32_000,
            },
            "context": {
                "max_input_tokens": 26_000,
                "auto_compact_threshold": 0.55,
                "recent_conversation_tokens": 5_000,
                "compaction_summary_tokens": 4_000,
                "compaction_max_output_tokens": 4_000,
            },
            "memory": {"auto_consolidate": False},
            "storage": {"state_path": str(workspace / "state.db")},
            "skills": {"path": str(workspace / "skills")},
        }
    )
    provider = _AgentAndCompactionProvider()
    store = SQLiteSessionStore(workspace / "state.db")
    events = MemoryEventSink()
    event_bus = EventBus([store, events])
    session_id, original_digest = seed_long_context(
        store,
        workspace=workspace,
        scenario=scenario,
    )
    seed_latest = store.latest_message_position(session_id)
    catalog = SkillCatalog(workspace / "skills")
    catalog.scan()
    compactor = ContextCompactor(
        config=config,
        provider=provider,
        store=store,
        event_bus=event_bus,
    )
    runner = AgentRunner(
        config=config,
        workspace=workspace,
        provider=provider,
        tool_registry=ToolRegistry(),
        policy=DefaultPolicyEngine(config.permissions, workspace),
        execution_target=LocalExecutionTarget(),
        skills=SkillManager(catalog),
        context=ContextAssembler(workspace=workspace, skill_catalog=catalog),
        store=store,
        event_bus=event_bus,
        context_compactor=compactor,
    )

    result = await runner.run(
        RunRequest(
            prompt="继续迁移，先复述 API 兼容约束，再执行下一阶段。",
            session_id=session_id,
        )
    )

    projection = compactor.projection(session_id)
    cursor = int(projection["cursor_position"])
    assert result.status == "completed"
    assert 0 < cursor < seed_latest
    assert len(store.list_context_compactions(session_id)) == 1
    assert not store.list_memory_episodes(session_id)
    assert any(event.type == EventType.CONTEXT_CONSOLIDATED for event in events.events)
    assert provider.agent_requests
    request = provider.agent_requests[0]
    summaries = [message for message in request.messages if message.name == "context_compaction"]
    assert len(summaries) == 1
    assert "阶段 1/10" in (summaries[0].content or "")
    assert any(
        "阶段 10/10" in (message.content or "")
        for message in request.messages
        if message.name is None
    )
    active = projection["compaction"]
    source = compactor.read_source(
        session_id=session_id,
        compaction_id=active["id"],
    )
    calls = {call.id for item in source["messages"] for call in _message(item).tool_calls}
    results = {
        _message(item).tool_call_id for item in source["messages"] if _message(item).tool_call_id
    }
    assert calls <= results
    assert original_digest == _digest_through(store, session_id, seed_latest)
    assert store.latest_context_snapshot(session_id) is None
    store.close()


def _message(item: dict) -> ChatMessage:
    return ChatMessage.model_validate(item["message"])


def _digest(store: SQLiteSessionStore, session_id: str) -> str:
    digest = hashlib.sha256()
    for entry in store.load_positioned_messages(session_id):
        digest.update(str(entry.position).encode())
        digest.update(b"\0")
        digest.update(entry.message.model_dump_json().encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _digest_through(
    store: SQLiteSessionStore,
    session_id: str,
    through_position: int,
) -> str:
    digest = hashlib.sha256()
    for entry in store.load_positioned_messages(
        session_id,
        through_position=through_position,
    ):
        digest.update(str(entry.position).encode())
        digest.update(b"\0")
        digest.update(entry.message.model_dump_json().encode())
        digest.update(b"\n")
    return digest.hexdigest()
