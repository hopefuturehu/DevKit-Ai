import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from bot.compaction import CompactionErrorClass, ContextCompactor
from bot.config.models import AppConfig
from bot.core.context import TokenEstimator
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
from bot.providers import ModelProvider, ProviderError, ProviderErrorKind
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


class SequenceCompactionProvider(ModelProvider):
    def __init__(
        self,
        responses: list[str | None | tuple[str | None, str] | ProviderError],
    ) -> None:
        self.responses = responses
        self.requests: list[ModelRequest] = []

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities()

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, ProviderError):
            raise response
        finish_reason = "stop"
        if isinstance(response, tuple):
            response, finish_reason = response
        if response is None:
            payload = json.loads(request.messages[-1].content or "{}")
            start, end = payload.get("covered_range") or payload["allowed_reference_range"]
            response = CompactionProvider._summary(start, end)
        yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text=response)
        yield ModelEvent(kind=ModelEventKind.USAGE, input_tokens=222, output_tokens=111)
        yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason=finish_reason)


class SlowCompactionProvider(ModelProvider):
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities()

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        await asyncio.sleep(1)
        yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")


def make_compactor(
    tmp_path: Path,
) -> tuple[ContextCompactor, CompactionProvider, SQLiteSessionStore, MemoryEventSink]:
    config = AppConfig.model_validate(
        {
            "model": {
                "base_url": "https://api.deepseek.com/v1",
                "name": "compaction-model",
                "context_window_tokens": 32_000,
            },
            "context": {
                "compaction_strategy": "current",
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
            content="agent.py 中存在旧的上下文拼装路径。",
        ),
    )
    store.append_message(
        session_id,
        "run-1",
        ChatMessage(role=Role.ASSISTANT, content="准备改成单一活动摘要。"),
    )


@pytest.mark.parametrize(
    "heading",
    [
        "# Constraints（用户硬约束，原样保留）",
        "## Constraints (user hard requirements, verbatim intent)",
        "### constraints\t(unverified)\t",
    ],
)
def test_summary_accepts_known_heading_annotation_without_changing_content(tmp_path, heading):
    compactor, _, store, _ = make_compactor(tmp_path)
    try:
        summary = CompactionProvider._summary(1, 4).replace("# Constraints", heading)
        normalized = compactor._normalize_summary(summary)
        assert normalized == summary
        assert compactor._validate_candidate(
            normalized, finish_reason="stop", covered_start=1, covered_end=4
        ) == ["[m:1-4]"]
    finally:
        store.close()


@pytest.mark.parametrize(
    "heading",
    [
        "# Constraints extra",
        "# Constraints (unclosed",
        "# Constraints（mismatched)",
        "# Constraints (note) trailing text",
        "# Constraints (nested (note))",
        "# Constraints (first\nsecond)",
        "#### Constraints (wrong level)",
        "#\nConstraints (not a heading)",
    ],
)
def test_summary_annotation_compatibility_still_requires_a_real_known_heading(tmp_path, heading):
    compactor, _, store, _ = make_compactor(tmp_path)
    try:
        summary = CompactionProvider._summary(1, 4).replace("# Constraints", heading)
        with pytest.raises(ValueError, match="缺少章节: Constraints"):
            compactor._validate_candidate(
                compactor._normalize_summary(summary),
                finish_reason="stop",
                covered_start=1,
                covered_end=4,
            )
    finally:
        store.close()


@pytest.mark.parametrize("problem", ["empty", "missing", "length", "budget", "source"])
def test_heading_compatibility_does_not_bypass_candidate_guards(tmp_path, problem):
    compactor, _, store, _ = make_compactor(tmp_path)
    try:
        summary = CompactionProvider._summary(1, 4).replace(
            "# Critical Context", "# Critical Context（待核实）"
        )
        if problem == "empty":
            summary = ""
        elif problem == "missing":
            summary = summary.replace("# Failures", "# Other")
        elif problem == "budget":
            summary += "\n" + "evidence " * 10_000
        elif problem == "source":
            summary += "\n- 越界证据 [m:5]"
        with pytest.raises(ValueError):
            compactor._validate_candidate(
                summary,
                finish_reason="length" if problem == "length" else "stop",
                covered_start=1,
                covered_end=4,
            )
    finally:
        store.close()


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

    context_messages = compactor.context_messages(session_id, active)
    assert [message.role for message in context_messages] == [Role.USER, Role.ASSISTANT]
    assert "初始目标：修复上下文压缩" in (context_messages[0].content or "")
    assert context_messages[1].name == "context_compaction"
    assert "load_compaction_source" in (context_messages[1].content or "")
    assert context_messages[1].role == Role.ASSISTANT
    source = compactor.read_source(
        session_id=session_id,
        compaction_id=active["id"],
        start_position=2,
        end_position=3,
    )
    assert [item["position"] for item in source["messages"]] == [2, 3]
    assert source["source_verification"]["verified"] is True
    assert store.search_session_messages(session_id, "上下文拼装")[0]["position"] == 3
    assert provider.requests[0].temperature == 0
    assert EventType.CONTEXT_COMPACTION_COMPLETED in {event.type for event in events.events}
    forked = store.fork_session(session_id, up_to_position=4)
    forked_projection = compactor.projection(forked)
    assert forked_projection["cursor_position"] == 4
    assert forked_projection["compaction"]["trigger"] == "fork"
    assert len(store.load_positioned_messages(forked)) == 4
    store.close()


@pytest.mark.asyncio
async def test_manual_compaction_replays_latest_covered_user_as_raw_anchor(
    tmp_path: Path,
) -> None:
    compactor, _provider, store, _events = make_compactor(tmp_path)
    session_id = store.create_session(tmp_path)
    first_user = store.append_message(
        session_id,
        "run-1",
        ChatMessage(role=Role.USER, content="旧任务已经完成。"),
    )
    store.append_message(
        session_id,
        "run-1",
        ChatMessage(role=Role.ASSISTANT, content="旧任务完成。"),
    )
    latest_user = store.append_message(
        session_id,
        "run-2",
        ChatMessage(role=Role.USER, content="当前任务：只修复重试逻辑。"),
    )
    store.append_message(
        session_id,
        "run-2",
        ChatMessage(role=Role.ASSISTANT, content="开始检查重试逻辑。"),
    )

    result = await compactor.compact(
        session_id,
        through_position=4,
        trigger="explicit_compaction",
    )

    assert result.compacted is True
    active = compactor.projection(session_id)["compaction"]
    assert active["anchor_positions"] == [latest_user]
    assert active["anchor_positions"] != [first_user]
    replay = compactor.context_messages(session_id, active)
    assert replay[0].role == Role.USER
    assert replay[0].content == "当前任务：只修复重试逻辑。"
    assert replay[1].role == Role.ASSISTANT
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
async def test_compaction_chunks_oldest_atomic_prefix_to_input_budget(tmp_path: Path) -> None:
    compactor, provider, store, _ = make_compactor(tmp_path)
    compactor.config.context.compaction_max_input_tokens = 3_000
    compactor.config.context.compaction_max_output_tokens = 1_024
    compactor.config.context.compaction_summary_tokens = 1_024
    session_id = store.create_session(tmp_path)
    for position in range(1, 13):
        store.append_message(
            session_id,
            "long-run",
            ChatMessage(role=Role.USER, content=f"message-{position}\n" + "x" * 4_000),
        )

    result = await compactor.compact(
        session_id,
        through_position=12,
        trigger="context_pressure",
    )

    assert result.compacted is True
    assert 0 < result.covered_end_position < 12
    assert result.requested_end_position == 12
    assert result.planned_input_tokens <= 3_000
    assert TokenEstimator().request(provider.requests[0].messages, []) <= 3_000
    assert compactor.projection(session_id)["cursor_position"] == result.covered_end_position
    store.close()


@pytest.mark.asyncio
async def test_compaction_repairs_invalid_candidate_without_resending_source(
    tmp_path: Path,
) -> None:
    config = AppConfig.model_validate(
        {
            "model": {
                "base_url": "https://unused",
                "name": "compaction-model",
                "context_window_tokens": 32_000,
            },
            "context": {
                "compaction_summary_tokens": 2_048,
                "compaction_max_output_tokens": 3_000,
                "compaction_repair_attempts": 1,
                "compaction_range_attempts": 1,
            },
            "storage": {"state_path": str(tmp_path / "state.db")},
        }
    )
    provider = SequenceCompactionProvider(["# Goal\n- 缺少其余章节。 [m:1]", None])
    store = SQLiteSessionStore(tmp_path / "state.db")
    compactor = ContextCompactor(
        config=config,
        provider=provider,
        store=store,
        event_bus=EventBus([store]),
    )
    session_id = store.create_session(tmp_path)
    store.append_message(session_id, "run", ChatMessage(role=Role.USER, content="目标"))

    result = await compactor.compact(
        session_id,
        through_position=1,
        trigger="context_pressure",
    )

    assert result.compacted is True
    assert result.repair_attempts == 1
    assert result.input_tokens == 444
    assert result.output_tokens == 222
    assert [request.messages[-1].name for request in provider.requests] == [
        "context_compaction_input",
        "context_compaction_repair",
    ]
    repair_payload = json.loads(provider.requests[1].messages[-1].content or "{}")
    assert "new_messages" not in repair_payload
    assert repair_payload["candidate_summary"].startswith("# Goal")
    store.close()


@pytest.mark.asyncio
async def test_compaction_shrinks_range_only_after_context_overflow(tmp_path: Path) -> None:
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
                "compaction_repair_attempts": 0,
                "compaction_range_attempts": 2,
                "compaction_failure_backoff_seconds": 0,
            },
            "storage": {"state_path": str(tmp_path / "state.db")},
        }
    )
    provider = SequenceCompactionProvider(
        [
            ProviderError(
                "maximum context length exceeded",
                kind=ProviderErrorKind.CONTEXT_LENGTH,
            ),
            None,
        ]
    )
    store = SQLiteSessionStore(tmp_path / "state.db")
    compactor = ContextCompactor(
        config=config,
        provider=provider,
        store=store,
        event_bus=EventBus([store]),
    )
    session_id = store.create_session(tmp_path)
    for position in range(1, 9):
        store.append_message(
            session_id,
            "run",
            ChatMessage(role=Role.USER, content=f"message-{position}"),
        )

    result = await compactor.compact(
        session_id,
        through_position=8,
        trigger="context_pressure",
    )

    assert result.compacted is True
    assert result.covered_end_position == 4
    assert result.requested_end_position == 8
    assert result.attempts == 2
    failed_request_tokens = TokenEstimator().request(provider.requests[0].messages, [])
    assert result.input_tokens == failed_request_tokens + 222
    assert [item["status"] for item in store.list_context_compactions(session_id)] == [
        "ready",
        "failed",
    ]
    store.close()


@pytest.mark.asyncio
async def test_format_failure_does_not_shrink_or_resend_source(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {
            "model": {
                "base_url": "https://unused",
                "name": "compaction-model",
                "context_window_tokens": 32_000,
            },
            "context": {
                "compaction_repair_attempts": 0,
                "compaction_range_attempts": 2,
                "compaction_failure_backoff_seconds": 0,
            },
            "storage": {"state_path": str(tmp_path / "state.db")},
        }
    )
    provider = SequenceCompactionProvider(["无效摘要"])
    store = SQLiteSessionStore(tmp_path / "state.db")
    compactor = ContextCompactor(
        config=config,
        provider=provider,
        store=store,
        event_bus=EventBus([store]),
    )
    session_id = store.create_session(tmp_path)
    for position in range(1, 9):
        store.append_message(
            session_id,
            "run",
            ChatMessage(role=Role.USER, content=f"message-{position}"),
        )

    result = await compactor.compact(
        session_id,
        through_position=8,
        trigger="context_pressure",
    )

    assert result.compacted is False
    assert result.error_class == CompactionErrorClass.FORMAT
    assert result.attempts == 1
    assert result.request_count == 1
    assert len(provider.requests) == 1
    assert compactor.projection(session_id)["cursor_position"] == 0
    store.close()


@pytest.mark.asyncio
async def test_length_failure_condenses_candidate_without_resending_source(
    tmp_path: Path,
) -> None:
    config = AppConfig.model_validate(
        {
            "model": {
                "base_url": "https://unused",
                "name": "compaction-model",
                "context_window_tokens": 32_000,
            },
            "context": {
                "compaction_summary_tokens": 2_048,
                "compaction_max_output_tokens": 3_000,
                "compaction_repair_attempts": 0,
                "compaction_condense_attempts": 1,
                "compaction_range_attempts": 2,
            },
            "storage": {"state_path": str(tmp_path / "state.db")},
        }
    )
    provider = SequenceCompactionProvider([(CompactionProvider._summary(1, 1), "length"), None])
    store = SQLiteSessionStore(tmp_path / "state.db")
    events = MemoryEventSink()
    compactor = ContextCompactor(
        config=config,
        provider=provider,
        store=store,
        event_bus=EventBus([store, events]),
    )
    session_id = store.create_session(tmp_path)
    store.append_message(session_id, "run", ChatMessage(role=Role.USER, content="目标"))

    result = await compactor.compact(
        session_id,
        through_position=1,
        trigger="context_pressure",
    )

    assert result.compacted is True
    assert result.condense_attempts == 1
    assert result.request_count == 2
    assert [request.messages[-1].name for request in provider.requests] == [
        "context_compaction_input",
        "context_compaction_condense",
    ]
    condense_payload = json.loads(provider.requests[1].messages[-1].content or "{}")
    assert "new_messages" not in condense_payload
    assert "raw_messages" not in condense_payload
    assert condense_payload["candidate_summary"].startswith("# Goal")
    completed = [
        event
        for event in events.events
        if event.type == EventType.CONTEXT_COMPACTION_REQUEST_COMPLETED
    ]
    assert [event.payload["original_phase"] for event in completed] == [
        "generate",
        "condense",
    ]
    store.close()


@pytest.mark.asyncio
async def test_length_after_format_repair_switches_to_candidate_condense(
    tmp_path: Path,
) -> None:
    config = AppConfig.model_validate(
        {
            "model": {
                "base_url": "https://unused",
                "name": "compaction-model",
                "context_window_tokens": 32_000,
            },
            "context": {
                "compaction_summary_tokens": 2_048,
                "compaction_max_output_tokens": 3_000,
                "compaction_repair_attempts": 1,
                "compaction_condense_attempts": 1,
                "compaction_range_attempts": 1,
            },
            "storage": {"state_path": str(tmp_path / "state.db")},
        }
    )
    provider = SequenceCompactionProvider(
        ["# Goal\n- 缺少其余章节。 [m:1]", (None, "length"), None]
    )
    store = SQLiteSessionStore(tmp_path / "state.db")
    compactor = ContextCompactor(
        config=config,
        provider=provider,
        store=store,
        event_bus=EventBus([store]),
    )
    session_id = store.create_session(tmp_path)
    store.append_message(session_id, "run", ChatMessage(role=Role.USER, content="目标"))

    result = await compactor.compact(
        session_id,
        through_position=1,
        trigger="context_pressure",
    )

    assert result.compacted is True
    assert result.repair_attempts == 1
    assert result.condense_attempts == 1
    assert [request.messages[-1].name for request in provider.requests] == [
        "context_compaction_input",
        "context_compaction_repair",
        "context_compaction_condense",
    ]
    for request in provider.requests[1:]:
        payload = json.loads(request.messages[-1].content or "{}")
        assert "new_messages" not in payload
        assert "raw_messages" not in payload
        assert payload["candidate_summary"]
    store.close()


@pytest.mark.asyncio
async def test_compaction_cost_budget_stops_before_repair_request(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {
            "model": {
                "base_url": "https://unused",
                "name": "compaction-model",
                "context_window_tokens": 32_000,
                "input_cost_per_million": 1.0,
                "output_cost_per_million": 1.0,
            },
            "context": {
                "compaction_repair_attempts": 1,
                "compaction_range_attempts": 1,
                "compaction_command_max_cost_usd": 0.0001,
            },
            "storage": {"state_path": str(tmp_path / "state.db")},
        }
    )
    provider = SequenceCompactionProvider(["# Goal\n- 缺少其余章节。 [m:1]"])
    store = SQLiteSessionStore(tmp_path / "state.db")
    compactor = ContextCompactor(
        config=config,
        provider=provider,
        store=store,
        event_bus=EventBus([store]),
    )
    session_id = store.create_session(tmp_path)
    store.append_message(session_id, "run", ChatMessage(role=Role.USER, content="目标"))

    result = await compactor.compact(
        session_id,
        through_position=1,
        trigger="context_pressure",
    )

    assert result.compacted is False
    assert result.error_class == CompactionErrorClass.REQUEST_BUDGET
    assert result.request_count == 1
    assert len(provider.requests) == 1
    assert compactor.projection(session_id)["cursor_position"] == 0
    store.close()


@pytest.mark.asyncio
async def test_authentication_failure_is_not_retried_or_range_shrunk(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {
            "model": {
                "base_url": "https://unused",
                "name": "compaction-model",
                "context_window_tokens": 32_000,
            },
            "context": {
                "compaction_transport_retries": 3,
                "compaction_range_attempts": 3,
                "compaction_failure_backoff_seconds": 0,
            },
            "storage": {"state_path": str(tmp_path / "state.db")},
        }
    )
    provider = SequenceCompactionProvider(
        [ProviderError("HTTP 401", kind=ProviderErrorKind.AUTHENTICATION)]
    )
    store = SQLiteSessionStore(tmp_path / "state.db")
    compactor = ContextCompactor(
        config=config,
        provider=provider,
        store=store,
        event_bus=EventBus([store]),
    )
    session_id = store.create_session(tmp_path)
    store.append_message(session_id, "run", ChatMessage(role=Role.USER, content="目标"))

    result = await compactor.compact(
        session_id,
        through_position=1,
        trigger="context_pressure",
    )

    assert result.compacted is False
    assert result.error_class == CompactionErrorClass.AUTHENTICATION
    assert result.request_count == 1
    assert len(provider.requests) == 1
    store.close()


@pytest.mark.asyncio
async def test_rate_limit_retries_same_source_range_once(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {
            "model": {
                "base_url": "https://unused",
                "name": "compaction-model",
                "context_window_tokens": 32_000,
            },
            "context": {
                "compaction_transport_retries": 1,
                "compaction_transport_retry_backoff_seconds": 0,
                "compaction_range_attempts": 2,
            },
            "storage": {"state_path": str(tmp_path / "state.db")},
        }
    )
    provider = SequenceCompactionProvider(
        [ProviderError("HTTP 429", kind=ProviderErrorKind.RATE_LIMIT), None]
    )
    store = SQLiteSessionStore(tmp_path / "state.db")
    compactor = ContextCompactor(
        config=config,
        provider=provider,
        store=store,
        event_bus=EventBus([store]),
    )
    session_id = store.create_session(tmp_path)
    for position in range(1, 5):
        store.append_message(
            session_id,
            "run",
            ChatMessage(role=Role.USER, content=f"message-{position}"),
        )

    result = await compactor.compact(
        session_id,
        through_position=4,
        trigger="context_pressure",
    )

    assert result.compacted is True
    assert result.covered_end_position == 4
    assert result.transport_retries == 1
    assert result.request_count == 2
    first_payload = json.loads(provider.requests[0].messages[-1].content or "{}")
    second_payload = json.loads(provider.requests[1].messages[-1].content or "{}")
    assert first_payload["covered_range"] == second_payload["covered_range"] == [1, 4]
    store.close()


@pytest.mark.asyncio
async def test_request_wall_timeout_fails_closed(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {
            "model": {
                "base_url": "https://unused",
                "name": "compaction-model",
                "context_window_tokens": 32_000,
            },
            "context": {
                "compaction_request_timeout_seconds": 0.01,
                "compaction_transport_retries": 0,
                "compaction_range_attempts": 1,
            },
            "storage": {"state_path": str(tmp_path / "state.db")},
        }
    )
    provider = SlowCompactionProvider()
    store = SQLiteSessionStore(tmp_path / "state.db")
    compactor = ContextCompactor(
        config=config,
        provider=provider,
        store=store,
        event_bus=EventBus([store]),
    )
    session_id = store.create_session(tmp_path)
    store.append_message(session_id, "run", ChatMessage(role=Role.USER, content="目标"))

    result = await compactor.compact(
        session_id,
        through_position=1,
        trigger="context_pressure",
    )

    assert result.compacted is False
    assert result.error_class == CompactionErrorClass.TIMEOUT
    assert result.request_count == 1
    assert compactor.projection(session_id)["cursor_position"] == 0
    assert [item["status"] for item in store.list_context_compactions(session_id)] == ["failed"]
    store.close()


@pytest.mark.asyncio
async def test_persistence_failure_does_not_retry_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compactor, provider, store, _ = make_compactor(tmp_path)
    session_id = store.create_session(tmp_path)
    store.append_message(session_id, "run", ChatMessage(role=Role.USER, content="目标"))

    def fail_publish(*args, **kwargs) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "complete_context_compaction", fail_publish)

    result = await compactor.compact(
        session_id,
        through_position=1,
        trigger="context_pressure",
    )

    assert result.compacted is False
    assert result.error_class == CompactionErrorClass.PERSISTENCE
    assert len(provider.requests) == 1
    assert compactor.projection(session_id)["cursor_position"] == 0
    store.close()


@pytest.mark.asyncio
async def test_compaction_model_is_frozen_across_runtime_model_switch(tmp_path: Path) -> None:
    compactor, provider, store, _ = make_compactor(tmp_path)
    session_id = store.create_session(tmp_path)
    store.append_message(session_id, "run", ChatMessage(role=Role.USER, content="目标"))
    compactor.config.model.name = "reasoning-agent-model"
    compactor.config.model.thinking = "enabled"

    result = await compactor.compact(
        session_id,
        through_position=1,
        trigger="context_pressure",
    )

    assert result.compacted is True
    assert provider.requests[0].model == "compaction-model"
    assert provider.requests[0].thinking == "enabled"
    assert store.list_context_compactions(session_id)[0]["model"] == "compaction-model"
    store.close()


@pytest.mark.asyncio
async def test_range_source_mode_accepts_summary_without_item_references(tmp_path: Path) -> None:
    summary = "\n\n".join(
        f"# {section}\n- 保留原始事实。"
        for section in (
            "Goal",
            "Constraints",
            "Progress",
            "Key Decisions",
            "Relevant Files",
            "Failures",
            "Next Steps",
            "Critical Context",
        )
    )
    config = AppConfig.model_validate(
        {
            "model": {
                "base_url": "https://unused",
                "name": "compaction-model",
                "context_window_tokens": 32_000,
            },
            "context": {
                "compaction_source_refs": "range",
                "compaction_repair_attempts": 0,
            },
            "storage": {"state_path": str(tmp_path / "state.db")},
        }
    )
    provider = SequenceCompactionProvider([summary])
    store = SQLiteSessionStore(tmp_path / "state.db")
    compactor = ContextCompactor(
        config=config,
        provider=provider,
        store=store,
        event_bus=EventBus([store]),
    )
    session_id = store.create_session(tmp_path)
    store.append_message(session_id, "run", ChatMessage(role=Role.USER, content="目标"))

    result = await compactor.compact(
        session_id,
        through_position=1,
        trigger="context_pressure",
    )

    assert result.compacted is True
    assert result.source_refs == []
    assert provider.requests[0].thinking is None
    assert "[EVAL_FACT:...]" in (provider.requests[0].messages[0].content or "")
    source = compactor.read_source(session_id=session_id, compaction_id=result.compaction_id or "")
    assert source is not None
    assert source["source_verification"]["verified"] is True
    store.close()


@pytest.mark.asyncio
async def test_failed_compaction_persists_usage_and_backs_off(tmp_path: Path) -> None:
    compactor, provider, store, events = make_compactor(tmp_path)
    compactor.config.context.compaction_repair_attempts = 0
    compactor.config.context.compaction_range_attempts = 1
    compactor.config.context.compaction_failure_backoff_seconds = 300
    provider.invalid = True
    session_id = store.create_session(tmp_path)
    store.append_message(session_id, "run", ChatMessage(role=Role.USER, content="目标"))

    failed = await compactor.compact(
        session_id,
        through_position=1,
        trigger="context_pressure",
    )
    skipped = await compactor.compact(
        session_id,
        through_position=1,
        trigger="context_pressure",
    )

    assert failed.compacted is False
    assert skipped.reason == "failure_backoff"
    assert len(provider.requests) == 1
    record = store.list_context_compactions(session_id)[0]
    assert record["input_tokens"] == 222
    assert record["output_tokens"] == 111
    assert record["duration_ms"] >= 0
    assert any(event.type == EventType.CONTEXT_COMPACTION_SKIPPED for event in events.events)
    store.close()


@pytest.mark.asyncio
async def test_compaction_logically_closes_missing_results_from_inactive_run(
    tmp_path: Path,
) -> None:
    compactor, provider, store, events = make_compactor(tmp_path)
    session_id = store.create_session(tmp_path)
    store.start_run(session_id, "base-run")
    store.append_message(
        session_id,
        "base-run",
        ChatMessage(role=Role.USER, content="初始目标必须保留。"),
    )
    store.append_message(
        session_id,
        "base-run",
        ChatMessage(role=Role.ASSISTANT, content="已记录目标。"),
    )
    store.finish_run("base-run", "completed")
    first = await compactor.compact(
        session_id,
        through_position=2,
        trigger="context_pressure",
    )
    assert first.compacted is True

    store.start_run(session_id, "stale-run")
    store.append_message(
        session_id,
        "stale-run",
        ChatMessage(
            role=Role.ASSISTANT,
            tool_calls=[ToolCall(id="interrupted-call", name="read_file", arguments={})],
        ),
    )
    store.start_run(session_id, "active-run")
    store.append_message(
        session_id,
        "active-run",
        ChatMessage(role=Role.USER, content="继续处理后续任务。"),
    )

    second = await compactor.compact(
        session_id,
        through_position=4,
        trigger="context_pressure",
        active_run_ids={"active-run"},
    )

    assert second.compacted is True
    assert second.covered_end_position == 4
    assert len(store.load_positioned_messages(session_id)) == 4
    payload = json.loads(provider.requests[-1].messages[-1].content or "{}")
    derived = [item for item in payload["new_messages"] if item.get("derived")]
    assert len(derived) == 1
    assert derived[0]["position"] == 3
    assert derived[0]["tool_call_id"] == "interrupted-call"
    assert derived[0]["logical_resolution"] == "interrupted_result_unknown"
    assert derived[0]["stored_run_status"] == "running"
    started = [
        event for event in events.events if event.type == EventType.CONTEXT_COMPACTION_STARTED
    ]
    assert started[-1].payload["logical_tool_closures"] == [
        {
            "assistant_position": 3,
            "tool_call_id": "interrupted-call",
            "run_id": "stale-run",
            "stored_run_status": "running",
            "resolution": "interrupted_result_unknown",
            "reason": "inactive_run",
        }
    ]
    store.close()


@pytest.mark.asyncio
async def test_compaction_blocks_live_missing_result_and_emits_diagnostic(
    tmp_path: Path,
) -> None:
    compactor, provider, store, events = make_compactor(tmp_path)
    session_id = store.create_session(tmp_path)
    store.start_run(session_id, "base-run")
    store.append_message(
        session_id,
        "base-run",
        ChatMessage(role=Role.USER, content="初始目标必须保留。"),
    )
    store.append_message(
        session_id,
        "base-run",
        ChatMessage(role=Role.ASSISTANT, content="已记录目标。"),
    )
    store.finish_run("base-run", "completed")
    first = await compactor.compact(
        session_id,
        through_position=2,
        trigger="context_pressure",
    )
    assert first.compacted is True

    store.start_run(session_id, "live-run")
    store.append_message(
        session_id,
        "live-run",
        ChatMessage(
            role=Role.ASSISTANT,
            tool_calls=[ToolCall(id="live-call", name="read_file", arguments={})],
        ),
    )

    blocked = await compactor.compact(
        session_id,
        through_position=3,
        trigger="context_pressure",
        active_run_ids={"live-run"},
    )

    assert blocked.compacted is False
    assert blocked.reason == "incomplete_tool_group"
    assert blocked.covered_end_position == 2
    assert len(provider.requests) == 1
    blocked_events = [
        event for event in events.events if event.type == EventType.CONTEXT_COMPACTION_BLOCKED
    ]
    assert len(blocked_events) == 1
    assert blocked_events[0].payload["requested_boundary"] == 3
    assert blocked_events[0].payload["safe_boundary"] == 2
    assert blocked_events[0].payload["blocking_tool_groups"][0]["calls"][0] == {
        "tool_call_id": "live-call",
        "tool_name": "read_file",
        "reason": "missing_result_in_active_run",
        "result_position": None,
    }

    skipped = await compactor.compact(
        session_id,
        through_position=2,
        trigger="context_pressure",
        active_run_ids={"live-run"},
    )
    assert skipped.reason == "no_new_messages"
    assert any(event.type == EventType.CONTEXT_COMPACTION_SKIPPED for event in events.events)
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
    assert compactor.projection(session_id)["compaction"]["anchor_positions"] == [1]
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


@pytest.mark.asyncio
async def test_compaction_planning_respects_model_input_budget(tmp_path):
    from bot.core.models import InputTokenEstimate

    compactor, provider, store, _ = make_compactor(tmp_path)
    session_id = store.create_session(tmp_path)
    append_history(store, session_id)
    provider.estimate_input_tokens = lambda _: InputTokenEstimate(
        tokens=60000, budget_tokens=63000, source="test_tokenizer"
    )
    try:
        result = await compactor.compact(session_id, through_position=4, trigger="context_pressure")
        assert not result.compacted
        assert result.reason == "source_group_exceeds_budget"
        assert not provider.requests
        assert len(store.load_positioned_messages(session_id)) == 4
        assert compactor.projection(session_id)["compaction"] is None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_locally_rejected_summary_does_not_acquire_quota_or_charge_input(tmp_path):
    from bot.compaction.service import _CompactionFailure, _RequestQuota
    from bot.core.models import InputTokenEstimate

    compactor, provider, store, _ = make_compactor(tmp_path)
    provider.estimate_input_tokens = lambda _: InputTokenEstimate(
        tokens=60000, budget_tokens=63000, source="test_tokenizer"
    )
    quota = _RequestQuota(limit=3, cost_limit_usd=1)
    try:
        with pytest.raises(_CompactionFailure) as failure:
            await compactor._consume_text_request(
                ModelRequest(
                    model="compaction-model", messages=[ChatMessage(role=Role.USER, content="x")]
                ),
                session_id="unused",
                event_run_id="unused",
                compaction_id="unused",
                phase="summary",
                original_phase="summary",
                source_range=(1, 1),
                range_attempt=1,
                request_attempt=1,
                quota=quota,
            )
        assert failure.value.error_class == CompactionErrorClass.CONTEXT_OVERFLOW
        assert failure.value.input_tokens == 0
        assert quota.used == 0 and quota.cost_usd == 0
        assert not provider.requests
    finally:
        store.close()
