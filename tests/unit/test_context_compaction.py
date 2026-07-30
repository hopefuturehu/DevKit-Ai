import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from bot.compaction import ContextCompactor
from bot.config.models import AppConfig
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
from bot.providers import ModelProvider
from bot.sessions import SQLiteSessionStore


class CompactionProvider(ModelProvider):
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []
        self.invalid = False

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities()

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        if self.invalid:
            text = "没有结构和来源的无效摘要"
        else:
            payload = json.loads(request.messages[-1].content or "{}")
            start, end = payload["covered_range"]
            text = self._summary(start, end)
        yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text=text)
        yield ModelEvent(
            kind=ModelEventKind.USAGE,
            input_tokens=222,
            output_tokens=111,
        )
        yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")

    @staticmethod
    def _summary(start: int, end: int) -> str:
        reference = f"[m:{start}-{end}]" if start != end else f"[m:{start}]"
        return "\n\n".join(
            [
                f"# Goal\n- 保持初始任务目标并继续执行。 {reference}",
                f"# Constraints\n- 遵循用户约束并用测试验证。 {reference}",
                f"# Progress\n- 已保存当前工作进度。 {reference}",
                f"# Key Decisions\n- 使用可恢复的单摘要压缩。 {reference}",
                f"# Relevant Files\n- 当前改动来源于原始 Transcript。 {reference}",
                f"# Failures\n- 暂无已确认失败。 {reference}",
                f"# Next Steps\n- 继续处理未完成任务。 {reference}",
                f"# Critical Context\n- 原始消息仍可按位置读取。 {reference}",
            ]
        )


def make_compactor(
    tmp_path: Path,
) -> tuple[ContextCompactor, CompactionProvider, SQLiteSessionStore, MemoryEventSink]:
    config = AppConfig.model_validate(
        {
            "model": {
                "base_url": "https://unused",
                "name": "compaction-model",
                "context_window_tokens": 32_000,
            },
            "context": {
                "compaction_summary_tokens": 2_048,
                "compaction_max_output_tokens": 2_048,
                "compaction_rebuild_every": 2,
            },
            "storage": {"state_path": str(tmp_path / "state.db")},
        }
    )
    provider = CompactionProvider()
    store = SQLiteSessionStore(tmp_path / "state.db")
    events = MemoryEventSink()
    compactor = ContextCompactor(
        config=config,
        provider=provider,
        store=store,
        event_bus=EventBus([store, events]),
    )
    return compactor, provider, store, events


def append_history(store: SQLiteSessionStore, session_id: str) -> None:
    store.append_message(
        session_id,
        "run-1",
        ChatMessage(
            role=Role.USER,
            content="初始目标：修复上下文压缩，原始目标绝不能丢。",
        ),
    )
    store.append_message(
        session_id,
        "run-1",
        ChatMessage(
            role=Role.ASSISTANT,
            tool_calls=[ToolCall(id="call-1", name="read_file", arguments={"path": "agent.py"})],
        ),
    )
    store.append_message(
        session_id,
        "run-1",
        ChatMessage(
            role=Role.TOOL,
            name="read_file",
            tool_call_id="call-1",
            content="agent.py 中存在旧的 Episode 拼装路径。",
        ),
    )
    store.append_message(
        session_id,
        "run-1",
        ChatMessage(role=Role.ASSISTANT, content="准备改成单一活动摘要。"),
    )


@pytest.mark.asyncio
async def test_compaction_publishes_one_summary_without_deleting_source(
    tmp_path: Path,
) -> None:
    compactor, provider, store, events = make_compactor(tmp_path)
    session_id = store.create_session(tmp_path)
    append_history(store, session_id)

    result = await compactor.compact(
        session_id,
        through_position=4,
        trigger="context_pressure",
    )

    assert result.compacted is True
    assert result.covered_end_position == 4
    assert result.input_tokens == 222
    assert result.output_tokens == 111
    projection = compactor.projection(session_id)
    assert projection["cursor_position"] == 4
    active = projection["compaction"]
    assert active["status"] == "ready"
    assert active["parent_id"] is None
    assert store.latest_message_position(session_id) == 4
    assert len(store.load_positioned_messages(session_id)) == 4

    context_message = compactor.context_message(session_id, active)
    assert "初始目标：修复上下文压缩" in (context_message.content or "")
    assert "load_compaction_source" in (context_message.content or "")
    source = compactor.read_source(
        session_id=session_id,
        compaction_id=active["id"],
        start_position=2,
        end_position=3,
    )
    assert [item["position"] for item in source["messages"]] == [2, 3]
    assert source["source_verification"]["verified"] is True
    assert store.search_session_messages(session_id, "Episode")[0]["position"] == 3
    assert provider.requests[0].temperature == 0
    assert EventType.CONTEXT_COMPACTION_COMPLETED in {event.type for event in events.events}
    forked = store.fork_session(session_id, up_to_position=4)
    forked_projection = compactor.projection(forked)
    assert forked_projection["cursor_position"] == 4
    assert forked_projection["compaction"]["trigger"] == "fork"
    assert len(store.load_positioned_messages(forked)) == 4
    store.close()


@pytest.mark.asyncio
async def test_failed_compaction_does_not_advance_active_boundary(
    tmp_path: Path,
) -> None:
    compactor, provider, store, events = make_compactor(tmp_path)
    session_id = store.create_session(tmp_path)
    store.append_message(
        session_id,
        "run-1",
        ChatMessage(role=Role.USER, content="初始目标必须保留。"),
    )
    store.append_message(
        session_id,
        "run-1",
        ChatMessage(role=Role.ASSISTANT, content="已记录目标。"),
    )
    first = await compactor.compact(
        session_id,
        through_position=2,
        trigger="context_pressure",
    )
    store.append_message(
        session_id,
        "run-2",
        ChatMessage(role=Role.USER, content="新增约束：失败时不能推进游标。"),
    )
    provider.invalid = True

    failed = await compactor.compact(
        session_id,
        through_position=3,
        trigger="context_pressure",
    )

    assert failed.compacted is False
    assert failed.reason == "compaction_failed"
    assert failed.covered_end_position == 2
    projection = compactor.projection(session_id)
    assert projection["cursor_position"] == 2
    assert projection["compaction"]["id"] == first.compaction_id
    statuses = [item["status"] for item in store.list_context_compactions(session_id)]
    assert statuses.count("ready") == 1
    assert statuses.count("failed") == 1
    assert store.latest_message_position(session_id) == 3
    assert EventType.CONTEXT_COMPACTION_FAILED in {event.type for event in events.events}
    store.close()


@pytest.mark.asyncio
async def test_rebuild_rollback_and_corruption_recovery_use_version_chain(
    tmp_path: Path,
) -> None:
    compactor, provider, store, _ = make_compactor(tmp_path)
    session_id = store.create_session(tmp_path)
    store.append_message(
        session_id,
        "run-1",
        ChatMessage(role=Role.USER, content="初始目标：实现可恢复压缩。"),
    )
    store.append_message(
        session_id,
        "run-1",
        ChatMessage(role=Role.ASSISTANT, content="第一阶段完成。"),
    )
    first = await compactor.compact(
        session_id,
        through_position=2,
        trigger="context_pressure",
    )
    store.append_message(
        session_id,
        "run-2",
        ChatMessage(role=Role.USER, content="第二阶段：增加事务发布。"),
    )
    store.append_message(
        session_id,
        "run-2",
        ChatMessage(role=Role.ASSISTANT, content="第二阶段完成。"),
    )
    second = await compactor.compact(
        session_id,
        through_position=4,
        trigger="context_pressure",
    )
    assert second.parent_id == first.compaction_id

    rolled_back = compactor.rollback(session_id, first.compaction_id or "")
    assert rolled_back["id"] == first.compaction_id
    assert compactor.projection(session_id)["cursor_position"] == 2

    rebuilt = await compactor.rebuild(session_id)
    assert rebuilt.compacted is True
    assert rebuilt.rebuilt_from_raw is True
    assert rebuilt.covered_end_position == 2
    request_payload = json.loads(provider.requests[-1].messages[-1].content or "{}")
    assert request_payload["mode"] == "rebuild_from_raw"
    assert "raw_messages" in request_payload
    active_id = rebuilt.compaction_id or ""

    with store._lock, store._connection:
        store._connection.execute(
            """
            UPDATE context_compactions
            SET source_sha256 = ?
            WHERE id = ?
            """,
            ("0" * 64, active_id),
        )
    reopened = ContextCompactor(
        config=compactor.config,
        provider=provider,
        store=store,
        event_bus=compactor.event_bus,
    )
    recovered = reopened.projection(session_id)
    assert recovered["compaction"]["id"] == first.compaction_id
    assert recovered["cursor_position"] == 2
    assert store.get_context_compaction(session_id, active_id)["status"] == "failed"
    store.close()
