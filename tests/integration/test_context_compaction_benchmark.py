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
from bot.core.models import (
    ChatMessage,
    ModelCapabilities,
    ModelEvent,
    ModelEventKind,
    ModelRequest,
    Role,
    ToolCall,
)
from bot.execution import LocalExecutionTarget
from bot.policy import DefaultPolicyEngine
from bot.providers import ModelProvider
from bot.sessions import SQLiteSessionStore
from bot.skills import SkillCatalog, SkillManager
from bot.tools import ToolRegistry


class _AgentAndCompactionProvider(ModelProvider):
    def __init__(self) -> None:
        self.agent_requests: list[ModelRequest] = []

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities()

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        if request.messages[-1].name == "context_compaction_input":
            payload = json.loads(request.messages[-1].content or "{}")
            start, end = payload["covered_range"]
            messages = payload.get("raw_messages") or payload.get("new_messages") or []
            facts: list[tuple[int, str]] = []
            for item in messages:
                for line in str(item.get("content") or "").splitlines():
                    if line.startswith("[CONTEXT_FACT] "):
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


def _seed_long_context(store: SQLiteSessionStore, workspace: Path) -> tuple[str, str]:
    session_id = store.create_session(workspace)
    for index in range(10):
        run_id = f"migration-run-{index + 1:02d}"
        tool_call_id = f"migration-tool-{index + 1:02d}"
        label = f"阶段 {index + 1}/10"
        store.start_run(session_id, run_id)
        fact = "\n[CONTEXT_FACT] 公共 API 必须保持 v2 向后兼容。" if index == 0 else ""
        store.append_message(
            session_id,
            run_id,
            ChatMessage(role=Role.USER, content=f"{label}：继续迁移任务。{fact}"),
        )
        store.append_message(
            session_id,
            run_id,
            ChatMessage(
                role=Role.ASSISTANT,
                content=f"正在执行 {label} 的诊断与验证。",
                tool_calls=[
                    ToolCall(
                        id=tool_call_id,
                        name="run_command",
                        arguments={"argv": ["benchmark-step", str(index + 1)]},
                    )
                ],
            ),
        )
        noise = "\n".join(
            f"diagnostic step={index + 1:02d} sample={sample:03d} 阶段采样数据"
            for sample in range(160)
        )
        store.append_message(
            session_id,
            run_id,
            ChatMessage(
                role=Role.TOOL,
                name="run_command",
                tool_call_id=tool_call_id,
                content=f"{label} tool_success=true\n{noise}",
            ),
        )
        store.append_message(
            session_id,
            run_id,
            ChatMessage(role=Role.ASSISTANT, content=f"{label} 已完成。"),
        )
        store.finish_run(run_id, "completed")
    return session_id, _digest(store, session_id)


@pytest.mark.asyncio
async def test_recoverable_compaction_keeps_one_summary_recent_tail_and_raw_source(
    tmp_path: Path,
) -> None:
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
            "storage": {"state_path": str(workspace / "state.db")},
            "skills": {"path": str(workspace / "skills")},
        }
    )
    provider = _AgentAndCompactionProvider()
    store = SQLiteSessionStore(workspace / "state.db")
    events = MemoryEventSink()
    event_bus = EventBus([store, events])
    session_id, original_digest = _seed_long_context(store, workspace)
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
    assert any(event.type == EventType.CONTEXT_CONSOLIDATED for event in events.events)
    assert provider.agent_requests
    request = provider.agent_requests[0]
    summaries = [message for message in request.messages if message.name == "context_compaction"]
    assert len(summaries) == 1
    assert "公共 API 必须保持 v2 向后兼容" in (summaries[0].content or "")
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
    return _digest_through(store, session_id, store.latest_message_position(session_id))


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
