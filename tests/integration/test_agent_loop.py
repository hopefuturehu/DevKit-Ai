import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from bot.config.models import AppConfig
from bot.core import AgentRunner, RunRequest
from bot.core.approval import (
    AllowApprovalHandler,
    ApprovalResponse,
    ApprovalScope,
)
from bot.core.context import MANAGED_PROCESS_REMINDER, ContextAssembler
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
from bot.execution import LocalExecutionTarget, ProcessSpec
from bot.memory import ExtractedMemoryCandidate, MarkdownMemoryStore, MemoryKind
from bot.policy import DefaultPolicyEngine
from bot.providers import ModelProvider, ProviderError, ProviderErrorKind
from bot.sessions import SQLiteSessionStore
from bot.skills import SkillCatalog, SkillManager
from bot.tools import Tool, ToolAnnotations, ToolRegistry, ToolResult
from bot.tools.builtins import ReadFileTool
from bot.tools.plan import UpdatePlanTool


class ScriptedProvider(ModelProvider):
    def __init__(self, turns: list[list[ModelEvent]]) -> None:
        self.turns = turns
        self.requests: list[ModelRequest] = []

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities()

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        for event in self.turns.pop(0):
            yield event


class NoNamedToolChoiceProvider(ScriptedProvider):
    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(named_tool_choice=False)


class SteerableProvider(ModelProvider):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests: list[ModelRequest] = []

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities()

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        if len(self.requests) == 1:
            self.started.set()
            await self.release.wait()
            yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="first answer")
        else:
            yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="steered answer")
        yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")


class SteerableToolProvider(ModelProvider):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests: list[ModelRequest] = []

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities()

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        if len(self.requests) == 1:
            self.started.set()
            await self.release.wait()
            for index, (call_id, path) in enumerate(
                (("steer-call-a", "a.txt"), ("steer-call-b", "b.txt"))
            ):
                yield ModelEvent(
                    kind=ModelEventKind.TOOL_CALL_DELTA,
                    tool_index=index,
                    tool_call_id=call_id,
                    tool_name="read_file",
                    arguments_delta=json.dumps({"path": path}),
                )
            yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="tool_calls")
            return
        yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="steering handled")
        yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")


class UsageThenProviderError(ModelProvider):
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities()

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        if len(self.requests) == 1:
            yield ModelEvent(kind=ModelEventKind.USAGE, input_tokens=12, output_tokens=3)
            raise ProviderError("模型 API 返回 HTTP 400: invalid request")
        yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="provider failure summary")
        yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")


class ContextRetryProvider(ModelProvider):
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities()

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        if len(self.requests) == 1:
            raise ProviderError("maximum context length exceeded")
        yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="recovered")
        yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")


class TransientProvider(ModelProvider):
    def __init__(
        self,
        *,
        failures: int,
        emit_partial: bool = False,
        emit_usage: bool = False,
    ) -> None:
        self.failures = failures
        self.emit_partial = emit_partial
        self.emit_usage = emit_usage
        self.requests: list[ModelRequest] = []

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities()

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        if len(self.requests) <= self.failures:
            if self.emit_partial:
                yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="discarded partial")
                yield ModelEvent(kind=ModelEventKind.REASONING_DELTA, text="discarded reasoning")
                yield ModelEvent(
                    kind=ModelEventKind.TOOL_CALL_DELTA,
                    tool_index=0,
                    tool_call_id="discarded-call",
                    tool_name="read_file",
                    arguments_delta='{"path":"never-read.txt"}',
                )
            if self.emit_usage:
                yield ModelEvent(kind=ModelEventKind.USAGE, input_tokens=12, output_tokens=3)
            raise ProviderError(
                "Server disconnected without sending a response.",
                kind=ProviderErrorKind.TRANSPORT,
            )
        yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="recovered")
        if self.emit_usage:
            yield ModelEvent(kind=ModelEventKind.USAGE, input_tokens=5, output_tokens=2)
        yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")


class ApprovalHandlerStub:
    def __init__(self, scope: ApprovalScope) -> None:
        self.scope = scope
        self.calls = 0

    async def approve(self, action, decision) -> ApprovalResponse:
        self.calls += 1
        return ApprovalResponse(approved=True, scope=self.scope)


class DestructiveTestTool(Tool):
    name = "destructive_test"
    description = "A deterministic destructive test tool."
    input_schema = {"type": "object", "additionalProperties": False}
    annotations = ToolAnnotations(destructive=True)

    def __init__(self) -> None:
        self.executions = 0

    async def execute(self, context, arguments) -> ToolResult:
        self.executions += 1
        return ToolResult(success=True, output="executed")


class CommandApprovalTestTool(Tool):
    name = "run_command"
    description = "A command-shaped test tool without subprocess execution."
    input_schema = {
        "type": "object",
        "properties": {
            "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "cwd": {"type": "string"},
        },
        "required": ["argv"],
        "additionalProperties": False,
    }
    annotations = ToolAnnotations(read_only=False)

    def __init__(self) -> None:
        self.executions = 0

    async def execute(self, context, arguments) -> ToolResult:
        self.executions += 1
        return ToolResult(success=True, output="command simulated")


class LargeSchemaTool(Tool):
    description = "x" * 1_200
    input_schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "additionalProperties": False,
    }
    annotations = ToolAnnotations(read_only=True)

    def __init__(self, name: str) -> None:
        self.name = name

    async def execute(self, context, arguments) -> ToolResult:
        return ToolResult(success=True, output="ok")


def tool_turn(call_id: str, name: str, arguments: str) -> list[ModelEvent]:
    return [
        ModelEvent(
            kind=ModelEventKind.TOOL_CALL_DELTA,
            tool_index=0,
            tool_call_id=call_id,
            tool_name=name,
            arguments_delta=arguments,
        ),
        ModelEvent(kind=ModelEventKind.FINISH, finish_reason="tool_calls"),
    ]


def automatic_memory_candidate(
    content: str,
    *,
    positions: list[int] | None = None,
) -> ExtractedMemoryCandidate:
    return ExtractedMemoryCandidate(
        kind=MemoryKind.PROCEDURE,
        scope="workspace",
        memory_key="testing.primary-command",
        content=content,
        confidence=0.92,
        evidence_positions=positions or [1, 2],
    )


class CompactedPlanProjection:
    """Hide all old transcript messages while leaving durable plan events available."""

    def projection(self, session_id: str) -> dict[str, object]:
        del session_id
        return {"cursor_position": 10_000, "compaction": None}


@pytest.mark.asyncio
async def test_update_plan_persists_and_rehydrates_after_compaction(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            tool_turn(
                "plan-1",
                "update_plan",
                json.dumps(
                    {
                        "explanation": "实现阶段一",
                        "items": [
                            {"content": "分析入口", "status": "completed"},
                            {"content": "实现 TODO", "status": "in_progress"},
                            {"content": "运行测试", "status": "pending"},
                        ],
                    },
                    ensure_ascii=False,
                ),
            ),
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="第一轮完成"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="恢复后继续"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
        ]
    )
    runner, store = make_test_runner(tmp_path, provider, tools=[UpdatePlanTool()])

    first = await runner.run(RunRequest(prompt="实现功能"))

    assert first.status == "completed"
    assert store.load_plan(first.session_id) == {
        "explanation": "实现阶段一",
        "items": [
            {"content": "分析入口", "status": "completed"},
            {"content": "实现 TODO", "status": "in_progress"},
            {"content": "运行测试", "status": "pending"},
        ],
    }
    assert any(
        event["type"] == EventType.PLAN_UPDATED.value
        for event in store.list_events(first.session_id)
    )
    second_request_context = "\n".join(
        message.content or "" for message in provider.requests[1].messages
    )
    assert "当前会话 TODO list" in second_request_context
    assert "实现 TODO" in second_request_context

    runner.context_compactor = CompactedPlanProjection()
    resumed = await runner.run(
        RunRequest(prompt="继续处理未完成项", session_id=first.session_id)
    )

    assert resumed.status == "completed"
    resumed_context = "\n".join(message.content or "" for message in provider.requests[2].messages)
    assert "当前会话 TODO list" in resumed_context
    assert "上下文压缩不会清除" in resumed_context
    assert "实现 TODO" in resumed_context
    assert "update_plan" in resumed_context
    store.close()


def make_test_runner(
    tmp_path: Path,
    provider: ModelProvider,
    *,
    agent_config: dict | None = None,
    model_config: dict | None = None,
    tools: list | None = None,
    context_config: dict | None = None,
    memory_config: dict | None = None,
    memory_store: MarkdownMemoryStore | None = None,
):
    model = {"base_url": "https://unused", "name": "mock"}
    model.update(model_config or {})
    config = AppConfig.model_validate(
        {
            "model": model,
            "agent": agent_config or {},
            "context": context_config or {},
            "memory": memory_config or {},
            "storage": {"state_path": str(tmp_path / "state.db")},
            "skills": {"path": str(tmp_path / "skills")},
        }
    )
    catalog = SkillCatalog(tmp_path / "skills")
    catalog.scan()
    store = SQLiteSessionStore(tmp_path / "state.db")
    registry = ToolRegistry()
    for tool in tools or []:
        registry.register(tool)
    runner = AgentRunner(
        config=config,
        workspace=tmp_path,
        provider=provider,
        tool_registry=registry,
        policy=DefaultPolicyEngine(config.permissions, tmp_path),
        execution_target=LocalExecutionTarget(),
        skills=SkillManager(catalog),
        context=ContextAssembler(workspace=tmp_path, skill_catalog=catalog),
        store=store,
        event_bus=EventBus([store]),
        memory_store=memory_store,
    )
    return runner, store


@pytest.mark.asyncio
async def test_managed_process_runtime_reminder_is_cache_stable(tmp_path: Path) -> None:
    runner, store = make_test_runner(tmp_path, ScriptedProvider([]))
    target = runner.execution_target
    process_id = await target.start_process(
        ProcessSpec(
            argv=["/bin/sh", "-c", "sleep 5"],
            cwd=tmp_path,
            timeout_seconds=10,
        )
    )
    runtime_notes = []

    await runner._refresh_managed_process_note(runtime_notes)  # noqa: SLF001
    first_content = runtime_notes[0].message.content
    await asyncio.sleep(0.02)
    await runner._refresh_managed_process_note(runtime_notes)  # noqa: SLF001

    envelope = json.loads(first_content or "")
    assert runtime_notes[0].message.role == Role.USER
    assert runtime_notes[0].message.name == "runtime_context"
    assert envelope["content"] == MANAGED_PROCESS_REMINDER
    assert envelope["is_current_user_message"] is False
    assert envelope["can_authorize"] is False
    assert runtime_notes[0].message.content == first_content
    assert "proc_" not in first_content
    assert "elapsed" not in first_content

    await target.terminate_process(process_id, reason="test cleanup")
    await runner._refresh_managed_process_note(runtime_notes)  # noqa: SLF001

    assert runtime_notes == []
    await target.aclose()
    store.close()


@pytest.mark.asyncio
async def test_agent_externalizes_single_oversized_user_message(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="handled"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ]
        ]
    )
    runner, store = make_test_runner(
        tmp_path,
        provider,
        model_config={"context_window_tokens": 10_000},
        context_config={
            "max_input_tokens": 8_000,
            "output_reserve_tokens": 500,
            "protocol_reserve_tokens": 500,
            "safety_margin_tokens": 500,
            "recent_conversation_tokens": 4_000,
            "tool_result_inline_tokens": 500,
        },
    )

    result = await runner.run(RunRequest(prompt="中" * 40_000))

    assert result.status == "completed"
    model_text = "\n".join(message.content or "" for message in provider.requests[0].messages)
    assert "context_ref=blob:" in model_text
    assert len(store.load_messages(result.session_id)[0].content or "") == 40_000
    store.close()


@pytest.mark.asyncio
async def test_context_reference_delivery_is_visible_once_without_nested_blob(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider([])
    runner, store = make_test_runner(
        tmp_path,
        provider,
        tools=[ReadFileTool()],
        context_config={"tool_result_inline_tokens": 100},
    )
    session_id = store.create_session(tmp_path)
    payload = "BEGIN-" + ("evidence" * 1_000) + "-END"
    reference = store.put_context_blob(
        session_id=session_id,
        run_id="seed",
        content=payload,
    )
    provider.turns = [
        tool_turn(
            "load-once",
            "load_context_reference",
            json.dumps({"reference": reference}),
        ),
        tool_turn(
            "activate-after-load",
            "activate_tools",
            json.dumps({"names": ["read_file"]}),
        ),
        [
            ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="done"),
            ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
        ],
    ]

    result = await runner.run(RunRequest(prompt="读取证据", session_id=session_id))

    assert result.status == "completed"
    second_load_result = next(
        message for message in provider.requests[1].messages if message.tool_call_id == "load-once"
    )
    third_load_result = next(
        message for message in provider.requests[2].messages if message.tool_call_id == "load-once"
    )
    assert payload in (second_load_result.content or "")
    assert payload not in (third_load_result.content or "")
    assert "disposable_context_delivery" in (third_load_result.content or "")
    assert reference in (third_load_result.content or "")

    stored_load_result = next(
        message
        for message in store.load_messages(session_id)
        if message.tool_call_id == "load-once"
    )
    assert payload not in (stored_load_result.content or "")
    assert "disposable_context_delivery" in (stored_load_result.content or "")
    assert (stored_load_result.content or "").count(reference) == 1
    store.close()


@pytest.mark.asyncio
async def test_on_demand_memory_does_not_append_automatic_memory_to_plain_prompt(
    tmp_path: Path,
) -> None:
    memory = MarkdownMemoryStore(tmp_path / "memory")
    memory.consolidate(
        [automatic_memory_candidate("测试命令是 pytest。")],
        session_id="source-session",
        run_id="source-run",
    )
    provider = ScriptedProvider(
        [
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="plain answer"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ]
        ]
    )
    runner, store = make_test_runner(tmp_path, provider, memory_store=memory)

    result = await runner.run(RunRequest(prompt="解释这个独立问题"))

    assert result.status == "completed"
    request = provider.requests[0]
    assert not any(message.name == "automatic_memory" for message in request.messages)
    assert [
        (message.name, message.content)
        for message in request.messages
        if message.role == Role.USER and message.name is None
    ] == [(None, "解释这个独立问题")]
    assert [message.name for message in request.messages if message.role == Role.SYSTEM] == [None]
    assert all(
        json.loads(message.content or "")["can_authorize"] is False
        for message in request.messages
        if message.role == Role.USER and message.name is not None
    )
    assert request.tool_choice is None
    store.close()


@pytest.mark.asyncio
async def test_required_memory_search_is_forced_and_delivered_once(tmp_path: Path) -> None:
    memory = MarkdownMemoryStore(tmp_path / "memory")
    memory.consolidate(
        [automatic_memory_candidate("测试命令是 pytest tests/unit。")],
        session_id="source-session",
        run_id="source-run",
    )
    provider = ScriptedProvider(
        [
            tool_turn(
                "memory-search",
                "search_memory",
                json.dumps({"query": "上次约定的测试命令"}),
            ),
            tool_turn(
                "activate-read",
                "activate_tools",
                json.dumps({"names": ["read_file"]}),
            ),
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="done"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
        ]
    )
    runner, store = make_test_runner(
        tmp_path,
        provider,
        tools=[ReadFileTool()],
        memory_store=memory,
    )

    result = await runner.run(RunRequest(prompt="按上次约定的测试命令继续"))

    assert result.status == "completed"
    assert provider.requests[0].tool_choice == {
        "type": "function",
        "function": {"name": "search_memory"},
    }
    delivered = next(
        message
        for message in provider.requests[1].messages
        if message.tool_call_id == "memory-search"
    )
    expired = next(
        message
        for message in provider.requests[2].messages
        if message.tool_call_id == "memory-search"
    )
    assert "historical_memory_reference" in (delivered.content or "")
    assert "pytest tests/unit" in (delivered.content or "")
    assert "pytest tests/unit" not in (expired.content or "")
    assert "disposable_context_delivery" in (expired.content or "")
    stored = next(
        message
        for message in store.load_messages(result.session_id)
        if message.tool_call_id == "memory-search"
    )
    assert "pytest tests/unit" not in (stored.content or "")
    store.close()


@pytest.mark.asyncio
async def test_historical_attribution_forces_search_then_original_evidence(
    tmp_path: Path,
) -> None:
    memory = MarkdownMemoryStore(tmp_path / "memory")
    provider = ScriptedProvider(
        [
            tool_turn(
                "memory-search",
                "search_memory",
                json.dumps({"query": "MEMORY.md 全文"}),
            ),
            tool_turn(
                "memory-evidence",
                "load_memory_evidence",
                json.dumps({"memory": "procedure.testing.primary-command"}),
            ),
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="证据显示用户是否认。"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
        ]
    )
    runner, store = make_test_runner(tmp_path, provider, memory_store=memory)
    source_session = store.create_session(tmp_path, session_id="source-session")
    store.start_run(source_session, "source-run")
    user_position = store.append_message(
        source_session,
        "source-run",
        ChatMessage(role=Role.USER, content="我没有贴出 MEMORY.md 全文。"),
    )
    assistant_position = store.append_message(
        source_session,
        "source-run",
        ChatMessage(role=Role.ASSISTANT, content="用户贴出了 MEMORY.md 全文。"),
    )
    store.finish_run("source-run", "completed")
    memory.consolidate(
        [
            automatic_memory_candidate(
                "用户贴出了 MEMORY.md 全文。",
                positions=[user_position, assistant_position],
            )
        ],
        session_id=source_session,
        run_id="source-run",
    )

    result = await runner.run(RunRequest(prompt="我之前是否说过自己贴出了 MEMORY.md 全文？"))

    assert result.status == "completed"
    assert provider.requests[0].tool_choice == {
        "type": "function",
        "function": {"name": "search_memory"},
    }
    assert provider.requests[1].tool_choice == {
        "type": "function",
        "function": {"name": "load_memory_evidence"},
    }
    evidence = next(
        message
        for message in provider.requests[2].messages
        if message.tool_call_id == "memory-evidence"
    )
    assert "original_transcript_evidence" in (evidence.content or "")
    assert '"role": "user"' in (evidence.content or "")
    assert "我没有贴出 MEMORY.md 全文" in (evidence.content or "")
    assert "用户贴出了 MEMORY.md 全文" in (evidence.content or "")
    assert "disposable_context_delivery" in "\n".join(
        message.content or "" for message in provider.requests[2].messages
    )
    store.close()


@pytest.mark.asyncio
async def test_required_memory_tool_gate_discards_unsupported_final_text(
    tmp_path: Path,
) -> None:
    memory = MarkdownMemoryStore(tmp_path / "memory")
    provider = ScriptedProvider(
        [
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="unsupported attribution"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="unsupported attribution"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
        ]
    )
    runner, store = make_test_runner(tmp_path, provider, memory_store=memory)

    result = await runner.run(RunRequest(prompt="按上次的方案继续"))

    assert result.status == "failed"
    assert len(provider.requests) == 2
    assert all(
        request.tool_choice == {"type": "function", "function": {"name": "search_memory"}}
        for request in provider.requests
    )
    persisted_text = "\n".join(
        message.content or "" for message in store.load_messages(result.session_id)
    )
    assert "unsupported attribution" not in persisted_text
    store.close()


@pytest.mark.asyncio
async def test_required_memory_gate_rejects_wrong_tool_without_named_tool_choice(
    tmp_path: Path,
) -> None:
    memory = MarkdownMemoryStore(tmp_path / "memory")
    memory.consolidate(
        [automatic_memory_candidate("测试命令是 pytest tests/unit。")],
        session_id="source-session",
        run_id="source-run",
    )
    provider = NoNamedToolChoiceProvider(
        [
            tool_turn(
                "wrong-evidence",
                "load_memory_evidence",
                json.dumps({"memory": "procedure.testing.primary-command"}),
            ),
            tool_turn(
                "memory-search",
                "search_memory",
                json.dumps({"query": "上次方案"}),
            ),
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="done"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
        ]
    )
    runner, store = make_test_runner(tmp_path, provider, memory_store=memory)

    result = await runner.run(RunRequest(prompt="按上次的方案继续"))

    assert result.status == "completed"
    assert all(request.tool_choice is None for request in provider.requests)
    messages = store.load_messages(result.session_id)
    assert not any(
        call.name == "load_memory_evidence"
        for message in messages
        for call in message.tool_calls
    )
    assert any(
        call.name == "search_memory" for message in messages for call in message.tool_calls
    )
    store.close()


def test_agent_searches_context_reference_without_loading_whole_blob(tmp_path: Path) -> None:
    runner, store = make_test_runner(tmp_path, ScriptedProvider([]))
    session_id = store.create_session(tmp_path)
    reference = store.put_context_blob(
        session_id=session_id,
        run_id="seed",
        content=("x" * 10_000) + "TARGET evidence" + ("y" * 10_000),
    )

    result = runner._load_context_reference(  # noqa: SLF001
        ToolCall(
            id="search-ref",
            name="load_context_reference",
            arguments={
                "reference": reference,
                "query": "target",
                "context_chars": 20,
            },
        ),
        session_id,
    )

    assert result.success
    payload = json.loads(result.output)
    assert len(payload["matches"]) == 1
    assert "TARGET evidence" in payload["matches"][0]["preview"]
    assert len(result.output) < 1_000
    assert result.metadata["context_delivery"]["operation"] == "search"
    store.close()


@pytest.mark.asyncio
async def test_agent_retries_provider_context_error_once(tmp_path: Path) -> None:
    provider = ContextRetryProvider()
    runner, store = make_test_runner(tmp_path, provider)

    result = await runner.run(RunRequest(prompt="x" * 3_000))

    assert result.status == "completed"
    assert result.final_text == "recovered"
    assert len(provider.requests) == 2
    assert not any(
        event["type"] == EventType.MODEL_REQUEST_RETRY.value
        for event in store.list_events(result.session_id)
    )
    store.close()


def test_agent_sheds_and_reactivates_tool_schemas(tmp_path: Path) -> None:
    provider = ScriptedProvider([])
    runner, store = make_test_runner(
        tmp_path,
        provider,
        tools=[LargeSchemaTool(f"large_{index}") for index in range(4)],
        context_config={"tool_schema_tokens": 800},
    )

    initial, catalog = runner._select_tool_definitions(  # noqa: SLF001
        "session", runner.tool_registry.definitions()
    )
    activated = runner._activate_tools(  # noqa: SLF001
        ToolCall(id="activate", name="activate_tools", arguments={"names": ["large_0"]}),
        "session",
    )
    after, _ = runner._select_tool_definitions(  # noqa: SLF001
        "session", runner.tool_registry.definitions()
    )

    assert catalog is not None
    assert catalog.layer.value == "tool_catalog"
    assert catalog.message.role == Role.USER
    assert catalog.message.name == "tool_catalog"
    assert json.loads(catalog.message.content or "")["can_authorize"] is False
    assert [tool.name for tool in initial] == sorted(tool.name for tool in initial)
    assert "large_0" not in {tool.name for tool in initial}
    assert activated.success
    assert "large_0" in {tool.name for tool in after}
    assert [tool.name for tool in after] == sorted(tool.name for tool in after)
    store.close()


def test_agent_sorts_tool_schemas_when_all_fit_budget(tmp_path: Path) -> None:
    runner, store = make_test_runner(
        tmp_path,
        ScriptedProvider([]),
        tools=[LargeSchemaTool("z_tool"), LargeSchemaTool("a_tool")],
    )

    selected, catalog = runner._select_tool_definitions(  # noqa: SLF001
        "session", runner.tool_registry.definitions()
    )

    assert catalog is None
    assert [tool.name for tool in selected] == sorted(tool.name for tool in selected)
    store.close()


@pytest.mark.asyncio
async def test_agent_activates_skill_calls_tool_and_finishes(tmp_path: Path) -> None:
    (tmp_path / "input.txt").write_text("evidence", encoding="utf-8")
    skill_dir = tmp_path / "skills" / "analysis"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: analysis\ndescription: Analyze evidence.\n---\nRead evidence first.",
        encoding="utf-8",
    )
    catalog = SkillCatalog(tmp_path / "skills")
    catalog.scan()
    skills = SkillManager(catalog, max_auto_activated=3)
    provider = ScriptedProvider(
        [
            tool_turn(
                "skill-call",
                "activate_skill",
                '{"name":"analysis","reason":"task requires evidence"}',
            ),
            tool_turn("read-call", "read_file", '{"path":"input.txt"}'),
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="Completed from evidence."),
                ModelEvent(
                    kind=ModelEventKind.USAGE,
                    input_tokens=100,
                    output_tokens=50,
                ),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
        ]
    )
    config = AppConfig.model_validate(
        {
            "model": {
                "base_url": "https://unused",
                "name": "mock",
                "input_cost_per_million": 1,
                "output_cost_per_million": 2,
            },
            "storage": {"state_path": str(tmp_path / "state.db")},
            "skills": {"path": str(tmp_path / "skills")},
        }
    )
    store = SQLiteSessionStore(tmp_path / "state.db")
    memory = MemoryEventSink()
    bus = EventBus([store, memory])
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    target = LocalExecutionTarget()
    runner = AgentRunner(
        config=config,
        workspace=tmp_path,
        provider=provider,
        tool_registry=registry,
        policy=DefaultPolicyEngine(config.permissions, tmp_path),
        execution_target=target,
        skills=skills,
        context=ContextAssembler(workspace=tmp_path, skill_catalog=catalog),
        store=store,
        event_bus=bus,
        approval_handler=AllowApprovalHandler(),
    )

    result = await runner.run(RunRequest(prompt="analyze input"))

    assert result.status == "completed"
    assert result.final_text == "Completed from evidence."
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert result.cost_usd == pytest.approx(0.0002)
    assert len(provider.requests) == 3
    assert not skills.active
    assert runner.active_skill_names(result.session_id) == []
    event_types = [event.type for event in memory.events]
    assert EventType.SKILL_DISCOVERED in event_types
    assert EventType.SKILL_ACTIVATED in event_types
    assert EventType.TOOL_COMPLETED in event_types
    assert EventType.MODEL_USAGE in event_types
    assert all(
        len([message for message in request.messages if message.role == Role.SYSTEM]) == 1
        for request in provider.requests
    )
    for request in provider.requests[1:]:
        bodies = [
            message
            for message in request.messages
            if "Read evidence first." in (message.content or "")
        ]
        assert len(bodies) == 1
        assert bodies[0].role == Role.TOOL
        assert bodies[0].tool_call_id == "skill-call"
        assert not any(message.name == "active_skill" for message in request.messages)
    assert all(
        [tool.name for tool in request.tools] == [tool.name for tool in provider.requests[0].tools]
        for request in provider.requests
    )
    assert store.load_messages(result.session_id)[-1].content == "Completed from evidence."
    store.close()


@pytest.mark.asyncio
async def test_agent_applies_steering_at_model_boundary(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {
            "model": {"base_url": "https://unused", "name": "mock"},
            "storage": {"state_path": str(tmp_path / "state.db")},
            "skills": {"path": str(tmp_path / "skills")},
        }
    )
    catalog = SkillCatalog(tmp_path / "skills")
    catalog.scan()
    store = SQLiteSessionStore(tmp_path / "state.db")
    session_id = store.create_session(tmp_path)
    memory = MemoryEventSink()
    provider = SteerableProvider()
    runner = AgentRunner(
        config=config,
        workspace=tmp_path,
        provider=provider,
        tool_registry=ToolRegistry(),
        policy=DefaultPolicyEngine(config.permissions, tmp_path),
        execution_target=LocalExecutionTarget(),
        skills=SkillManager(catalog),
        context=ContextAssembler(workspace=tmp_path, skill_catalog=catalog),
        store=store,
        event_bus=EventBus([store, memory]),
    )

    run_task = asyncio.create_task(runner.run(RunRequest(prompt="initial", session_id=session_id)))
    await provider.started.wait()
    assert await runner.steer(session_id, "focus on the new requirement")
    provider.release.set()
    result = await run_task

    assert result.status == "completed"
    assert result.final_text == "steered answer"
    assert result.steps == 2
    assert EventType.RUN_STEERED in [event.type for event in memory.events]
    assert any(
        "focus on the new requirement" in (message.content or "")
        for message in provider.requests[1].messages
    )
    store.close()


@pytest.mark.asyncio
async def test_agent_defers_steering_until_all_tool_results_are_persisted(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    provider = SteerableToolProvider()
    runner, store = make_test_runner(tmp_path, provider, tools=[ReadFileTool()])
    session_id = store.create_session(tmp_path)

    run_task = asyncio.create_task(
        runner.run(RunRequest(prompt="inspect", session_id=session_id))
    )
    await provider.started.wait()
    assert await runner.steer(session_id, "use the new direction")
    provider.release.set()
    result = await run_task

    assert result.status == "completed"
    persisted = store.load_messages(session_id)
    assistant_index = next(
        index for index, message in enumerate(persisted) if message.tool_calls
    )
    assert [message.role for message in persisted[assistant_index : assistant_index + 4]] == [
        Role.ASSISTANT,
        Role.TOOL,
        Role.TOOL,
        Role.USER,
    ]
    second_request = provider.requests[1].messages
    request_assistant_index = next(
        index for index, message in enumerate(second_request) if message.tool_calls
    )
    assert [
        message.role
        for message in second_request[request_assistant_index : request_assistant_index + 4]
    ] == [Role.ASSISTANT, Role.TOOL, Role.TOOL, Role.USER]
    assert "use the new direction" in (second_request[request_assistant_index + 3].content or "")
    store.close()


@pytest.mark.asyncio
async def test_agent_repairs_legacy_interleaved_tool_history_before_request(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="recovered history"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ]
        ]
    )
    runner, store = make_test_runner(tmp_path, provider)
    session_id = store.create_session(tmp_path)
    legacy_run_id = "legacy-tool-order"
    store.start_run(session_id, legacy_run_id)
    store.append_message(
        session_id,
        legacy_run_id,
        ChatMessage(
            role=Role.ASSISTANT,
            tool_calls=[ToolCall(id="legacy-call", name="read_file", arguments={})],
        ),
    )
    store.append_message(
        session_id,
        legacy_run_id,
        ChatMessage(role=Role.USER, content="interleaved steering"),
    )
    store.append_message(
        session_id,
        legacy_run_id,
        ChatMessage(role=Role.TOOL, tool_call_id="legacy-call", content="legacy result"),
    )
    store.finish_run(legacy_run_id, "failed")

    result = await runner.run(RunRequest(prompt="continue", session_id=session_id))

    assert result.status == "completed"
    request_messages = provider.requests[0].messages
    assistant_index = next(
        index for index, message in enumerate(request_messages) if message.tool_calls
    )
    assert request_messages[assistant_index + 1].role == Role.TOOL
    assert request_messages[assistant_index + 2].role == Role.USER
    repair_events = [
        event
        for event in store.list_events(session_id)
        if event["type"] == EventType.CONTEXT_TOOL_PROTOCOL_REPAIRED.value
    ]
    assert repair_events[-1]["payload"]["moved_tool_results"] == 1
    store.close()


@pytest.mark.asyncio
async def test_provider_error_preserves_step_and_usage_for_finalization(tmp_path: Path) -> None:
    provider = UsageThenProviderError()
    runner, store = make_test_runner(tmp_path, provider)

    result = await runner.run(RunRequest(prompt="fail after usage"))

    assert result.status == "failed"
    assert result.termination_reason == "provider_error"
    assert result.steps == 1
    assert result.input_tokens == 12
    assert result.output_tokens == 3
    assert result.final_text == "provider failure summary"
    assert provider.requests[-1].tools == provider.requests[-2].tools
    assert provider.requests[-1].tool_choice == "none"
    assert not any(
        event["type"] == EventType.MODEL_REQUEST_RETRY.value
        for event in store.list_events(result.session_id)
    )
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("exception", [RuntimeError, TimeoutError])
async def test_unexpected_failure_preserves_usage_before_finalizer(tmp_path, exception):
    class CrashingProvider(ScriptedProvider):
        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield ModelEvent(kind=ModelEventKind.USAGE, input_tokens=12, output_tokens=3)
                raise exception("unexpected failure after billed response")
            yield ModelEvent(kind=ModelEventKind.USAGE, input_tokens=5, output_tokens=2)
            yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="partial report")
            yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")

    provider = CrashingProvider([])
    runner, store = make_test_runner(tmp_path, provider)
    try:
        result = await runner.run(RunRequest(prompt="preserve billed usage"))
        assert result.status == "failed"
        assert result.input_tokens == 17 and result.output_tokens == 5
        assert result.steps == 1
        assert result.final_text == "partial report"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_agent_retries_transient_provider_error_and_discards_partial_turn(
    tmp_path: Path,
) -> None:
    provider = TransientProvider(failures=1, emit_partial=True, emit_usage=True)
    runner, store = make_test_runner(
        tmp_path,
        provider,
        agent_config={
            "model_request_retries": 2,
            "model_request_retry_backoff_seconds": 0,
        },
        tools=[ReadFileTool()],
    )

    result = await runner.run(RunRequest(prompt="recover from disconnect"))

    assert result.status == "completed"
    assert result.final_text == "recovered"
    assert result.steps == 1
    assert result.input_tokens == 17
    assert result.output_tokens == 5
    assert len(provider.requests) == 2
    events = store.list_events(result.session_id)
    retry = next(event for event in events if event["type"] == EventType.MODEL_REQUEST_RETRY.value)
    assert retry["payload"] == {
        "step": 1,
        "failed_attempt": 1,
        "next_attempt": 2,
        "retry_count": 1,
        "max_retries": 2,
        "delay_seconds": 0.0,
        "error": "Server disconnected without sending a response.",
        "error_kind": "transport",
        "status_code": None,
        "discarded_content_chars": len("discarded partial"),
        "discarded_reasoning_chars": len("discarded reasoning"),
        "discarded_tool_call_buffers": 1,
    }
    assert not any(event["type"] == EventType.TOOL_REQUESTED.value for event in events)
    persisted = store.load_messages(result.session_id)
    assert not any(
        "discarded" in (message.content or "")
        or "discarded" in (message.reasoning_content or "")
        or any(call.id == "discarded-call" for call in message.tool_calls)
        for message in persisted
    )
    store.close()


@pytest.mark.asyncio
async def test_agent_stops_after_transient_provider_retry_limit(tmp_path: Path) -> None:
    provider = TransientProvider(failures=4)
    runner, store = make_test_runner(
        tmp_path,
        provider,
        agent_config={
            "model_request_retries": 2,
            "model_request_retry_backoff_seconds": 0,
            "finalization": {"enabled": False},
        },
    )

    result = await runner.run(RunRequest(prompt="keep disconnecting"))

    assert result.status == "failed"
    assert result.termination_reason == "provider_error"
    assert len(provider.requests) == 3
    events = store.list_events(result.session_id)
    assert sum(event["type"] == EventType.MODEL_REQUEST_RETRY.value for event in events) == 2
    terminal = next(event for event in events if event["type"] == EventType.RUN_FAILED.value)
    assert terminal["payload"]["provider_retry_count"] == 2
    assert terminal["payload"]["provider_error_kind"] == "transport"
    store.close()


@pytest.mark.asyncio
async def test_agent_does_not_retry_provider_error_after_cost_limit(tmp_path: Path) -> None:
    provider = TransientProvider(failures=1, emit_usage=True)
    runner, store = make_test_runner(
        tmp_path,
        provider,
        agent_config={
            "max_cost_usd": 0.01,
            "model_request_retries": 2,
            "model_request_retry_backoff_seconds": 0,
            "finalization": {"enabled": False},
        },
        model_config={"input_cost_per_million": 1_000, "output_cost_per_million": 1_000},
    )

    result = await runner.run(RunRequest(prompt="do not exceed budget"))

    assert result.status == "limit_reached"
    assert result.termination_reason == "max_cost_usd"
    assert result.cost_usd == 0.015
    assert len(provider.requests) == 1
    assert not any(
        event["type"] == EventType.MODEL_REQUEST_RETRY.value
        for event in store.list_events(result.session_id)
    )
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, ProviderError])
async def test_finalizer_reuses_wire_prefix_after_partial_stream_failure(tmp_path, failure):
    class InterruptedProvider(ScriptedProvider):
        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="incomplete response")
                yield ModelEvent(
                    kind=ModelEventKind.TOOL_CALL_DELTA,
                    tool_call_id="broken-call",
                    tool_name="read_file",
                    arguments_delta='{"path":',
                )
                raise failure("connection interrupted")
            yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="interrupted summary")
            yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")

    provider = InterruptedProvider([])
    runner, store = make_test_runner(tmp_path, provider, tools=[ReadFileTool()])
    try:
        result = await runner.run(RunRequest(prompt="read evidence"))
        assert result.final_text == "interrupted summary"
        main, final = provider.requests
        original_wire = provider.serialized_messages(main)
        assert provider.serialized_messages(final)[: len(original_wire)] == original_wire
        assert final.tools == main.tools and final.tools
        assert final.tool_choice == "none"
        assert "incomplete response" not in str(final.messages)
        assert "broken-call" not in str(final.messages)
        assert not runner._finalization_frames
    finally:
        store.close()


@pytest.mark.asyncio
async def test_finalizer_repacking_keeps_schemas_when_appended_tail_exceeds_budget(tmp_path):
    class BudgetProvider(ScriptedProvider):
        def count_tokens(self, request):
            # The frozen old prefix fits; adding the finalizer exceeds the limit.
            text = "\n".join(m.content or "" for m in request.messages)
            return 6_000 if "OLD_PREFIX" in text and "终止判定" in text else 200

    provider = BudgetProvider(
        [
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="partial"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="length"),
            ],
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="bounded summary"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
        ]
    )
    runner, store = make_test_runner(
        tmp_path,
        provider,
        tools=[ReadFileTool()],
        context_config={"max_input_tokens": 5_000},
    )
    session = store.create_session(tmp_path)
    store.start_run(session, "seed")
    store.append_message(session, "seed", ChatMessage(role=Role.ASSISTANT, content="OLD_PREFIX"))
    store.finish_run("seed", "completed")
    try:
        result = await runner.run(RunRequest(prompt="continue", session_id=session))
        assert result.final_text == "bounded summary"
        main, final = provider.requests
        assert "OLD_PREFIX" in str(main.messages)
        assert "OLD_PREFIX" not in str(final.messages)
        assert final.tools == main.tools and final.tool_choice == "none"
        assert provider.count_tokens(final) <= runner._token_budget.hard_input_limit
        response = next(
            e
            for e in store.list_events(session)
            if e["type"] == EventType.MODEL_RESPONSE.value
            and e["payload"].get("phase") == "finalizing"
        )
        assert response["payload"]["reused_request_prefix"] is False
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("consumed", [False, True])
async def test_finalizer_respects_one_shot_retrieval_lifetime(tmp_path, consumed):
    provider = ScriptedProvider([])
    runner, store = make_test_runner(
        tmp_path,
        provider,
        agent_config={"max_steps": 2 if consumed else 1},
    )
    session = store.create_session(tmp_path)
    body = "ONCE_ONLY_EVIDENCE" * 500
    reference = store.put_context_blob(session_id=session, run_id="seed", content=body)
    provider.turns = [
        tool_turn("one-shot", "load_context_reference", json.dumps({"reference": reference}))
    ]
    if consumed:
        provider.turns.append(
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="partial"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="length"),
            ]
        )
    provider.turns.append(
        [
            ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="retrieval summary"),
            ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
        ]
    )
    try:
        result = await runner.run(RunRequest(prompt="read evidence", session_id=session))
        assert result.final_text == "retrieval summary"
        final = provider.requests[-1]
        message = next(m for m in final.messages if m.tool_call_id == "one-shot")
        assert (body in message.content) is not consumed
        if consumed:
            assert "disposable_context_delivery" in message.content
        else:
            main = provider.requests[0]
            assert final.messages[: len(main.messages)] == main.messages
    finally:
        store.close()


@pytest.mark.asyncio
async def test_finalizer_never_executes_an_unexpected_tool_call(tmp_path):
    tool = DestructiveTestTool()
    provider = ScriptedProvider(
        [
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="partial"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="length"),
            ],
            tool_turn("must-not-execute", tool.name, "{}"),
        ]
    )
    runner, store = make_test_runner(tmp_path, provider, tools=[tool])
    try:
        result = await runner.run(RunRequest(prompt="summarize"))
        assert tool.executions == 0
        assert "任务尚未完整完成" in result.final_text
        assert provider.requests[-1].tool_choice == "none"
        final_event = next(
            e
            for e in store.list_events(result.session_id)
            if e["type"] == EventType.ASSISTANT_MESSAGE.value
            and e["payload"].get("phase") == "finalizing"
        )
        assert final_event["payload"]["fallback"] is True
        assert "禁用工具" in final_event["payload"]["finalization_error"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_finalizer_synthesizes_unexecuted_tool_result_after_batch_limit(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.txt").write_text("large result", encoding="utf-8")
    (tmp_path / "b.txt").write_text("not executed", encoding="utf-8")
    provider = ScriptedProvider(
        [
            [
                ModelEvent(
                    kind=ModelEventKind.TOOL_CALL_DELTA,
                    tool_index=0,
                    tool_call_id="limit-call-a",
                    tool_name="read_file",
                    arguments_delta='{"path":"a.txt"}',
                ),
                ModelEvent(
                    kind=ModelEventKind.TOOL_CALL_DELTA,
                    tool_index=1,
                    tool_call_id="limit-call-b",
                    tool_name="read_file",
                    arguments_delta='{"path":"b.txt"}',
                ),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="tool_calls"),
            ],
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="batch limit summary"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
        ]
    )
    runner, store = make_test_runner(
        tmp_path,
        provider,
        agent_config={"max_total_tool_output_bytes": 1},
        tools=[ReadFileTool()],
    )

    result = await runner.run(RunRequest(prompt="read both"))

    assert result.status == "limit_reached"
    assert result.final_text == "batch limit summary"
    finalizer_messages = provider.requests[-1].messages
    assistant_index = next(
        index for index, message in enumerate(finalizer_messages) if len(message.tool_calls) == 2
    )
    assert [
        message.tool_call_id
        for message in finalizer_messages[assistant_index + 1 : assistant_index + 3]
    ] == ["limit-call-a", "limit-call-b"]
    assert "结果未知" in (finalizer_messages[assistant_index + 2].content or "")
    repair_events = [
        event
        for event in store.list_events(result.session_id)
        if event["type"] == EventType.CONTEXT_TOOL_PROTOCOL_REPAIRED.value
    ]
    assert repair_events[-1]["payload"]["phase"] == "finalizing"
    assert repair_events[-1]["payload"]["synthesized_tool_results"] == 1
    store.close()


@pytest.mark.asyncio
async def test_agent_stops_repeated_idempotent_results(tmp_path: Path) -> None:
    (tmp_path / "input.txt").write_text("unchanged", encoding="utf-8")
    provider = ScriptedProvider(
        [
            tool_turn("read-1", "read_file", '{"path":"input.txt"}'),
            tool_turn("read-2", "read_file", '{"path":"input.txt"}'),
            tool_turn("read-3", "read_file", '{"path":"input.txt"}'),
            tool_turn("read-4", "read_file", '{"path":"input.txt"}'),
            tool_turn("read-5", "read_file", '{"path":"input.txt"}'),
            tool_turn("read-6", "read_file", '{"path":"input.txt"}'),
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="blocked summary"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
        ]
    )
    config = AppConfig.model_validate(
        {
            "model": {"base_url": "https://unused", "name": "mock"},
            "storage": {"state_path": str(tmp_path / "state.db")},
            "skills": {"path": str(tmp_path / "skills")},
        }
    )
    catalog = SkillCatalog(tmp_path / "skills")
    catalog.scan()
    store = SQLiteSessionStore(tmp_path / "state.db")
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    memory = MemoryEventSink()
    runner = AgentRunner(
        config=config,
        workspace=tmp_path,
        provider=provider,
        tool_registry=registry,
        policy=DefaultPolicyEngine(config.permissions, tmp_path),
        execution_target=LocalExecutionTarget(),
        skills=SkillManager(catalog),
        context=ContextAssembler(workspace=tmp_path, skill_catalog=catalog),
        store=store,
        event_bus=EventBus([store, memory]),
    )

    result = await runner.run(RunRequest(prompt="read until it changes"))

    assert result.status == "blocked"
    assert result.final_text == "blocked summary"
    assert result.termination_reason == "tool_cycle_after_recovery"
    assert len(provider.requests) == 7
    assert provider.requests[-1].tools == provider.requests[-2].tools
    assert provider.requests[-1].tool_choice == "none"
    event_types = [event.type for event in memory.events]
    assert EventType.RUN_STALL_WARNING in event_types
    assert EventType.RUN_RECOVERY_STARTED in event_types
    assert EventType.RUN_FINALIZING in event_types
    assert EventType.RUN_BLOCKED in event_types
    store.close()


@pytest.mark.asyncio
async def test_agent_has_no_default_thirty_step_limit(tmp_path: Path) -> None:
    turns: list[list[ModelEvent]] = []
    for index in range(31):
        path = f"input-{index}.txt"
        (tmp_path / path).write_text(str(index), encoding="utf-8")
        turns.append(tool_turn(f"read-{index}", "read_file", json.dumps({"path": path})))
    turns.append(
        [
            ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="all read"),
            ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
        ]
    )
    provider = ScriptedProvider(turns)
    runner, store = make_test_runner(tmp_path, provider, tools=[ReadFileTool()])

    result = await runner.run(RunRequest(prompt="read all inputs"))

    assert result.status == "completed"
    assert result.steps == 32
    assert result.final_text == "all read"
    store.close()


@pytest.mark.asyncio
async def test_progress_state_is_restored_across_runs(tmp_path: Path) -> None:
    (tmp_path / "input.txt").write_text("unchanged", encoding="utf-8")
    provider = ScriptedProvider(
        [
            tool_turn("read-1", "read_file", '{"path":"input.txt"}'),
            tool_turn("read-2", "read_file", '{"path":"input.txt"}'),
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="first run limit"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
            tool_turn("read-3", "read_file", '{"path":"input.txt"}'),
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="recovered and done"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
        ]
    )
    runner, store = make_test_runner(
        tmp_path,
        provider,
        agent_config={"max_steps": 2},
        tools=[ReadFileTool()],
    )

    first = await runner.run(RunRequest(prompt="keep reading"))
    second = await runner.run(
        RunRequest(prompt="continue", session_id=first.session_id)
    )

    assert first.status == "limit_reached"
    assert second.status == "completed"
    event_types = [event["type"] for event in store.list_events(first.session_id)]
    assert EventType.RUN_PROGRESS_RESTORED.value in event_types
    assert EventType.RUN_RECOVERY_STARTED.value in event_types
    assert store.load_progress_state(first.session_id) is None
    store.close()


@pytest.mark.asyncio
async def test_agent_preserves_explicit_step_limit(tmp_path: Path) -> None:
    (tmp_path / "input.txt").write_text("value", encoding="utf-8")
    provider = ScriptedProvider(
        [
            tool_turn("read-1", "read_file", '{"path":"input.txt"}'),
            tool_turn("read-2", "read_file", '{"path":"input.txt"}'),
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="step limit summary"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
        ]
    )
    runner, store = make_test_runner(
        tmp_path,
        provider,
        agent_config={"max_steps": 2, "progress": {"enabled": False}},
        tools=[ReadFileTool()],
    )

    result = await runner.run(RunRequest(prompt="keep reading"))

    assert result.status == "limit_reached"
    assert result.steps == 2
    assert result.termination_reason == "max_steps"
    assert result.final_text == "step limit summary"
    assert len(provider.requests) == 3
    main_request, final_request = provider.requests[-2:]
    assert final_request.tools == main_request.tools
    assert final_request.tool_choice == "none"
    assert final_request.messages[: len(main_request.messages)] == main_request.messages
    tail = final_request.messages[len(main_request.messages) :]
    assert tail[0].tool_calls[0].id == "read-2"
    assert tail[1].tool_call_id == "read-2" and "value" in tail[1].content
    assert "max_steps" in tail[-1].content
    assert not runner._finalization_frames
    store.close()


@pytest.mark.asyncio
async def test_agent_allows_same_idempotent_result_for_different_arguments(tmp_path: Path) -> None:
    for name in ("first.txt", "second.txt", "third.txt"):
        (tmp_path / name).write_text("unchanged", encoding="utf-8")
    provider = ScriptedProvider(
        [
            tool_turn("read-1", "read_file", '{"path":"first.txt"}'),
            tool_turn("read-2", "read_file", '{"path":"second.txt"}'),
            tool_turn("read-3", "read_file", '{"path":"third.txt"}'),
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="done"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
        ]
    )
    runner, store = make_test_runner(
        tmp_path,
        provider,
        agent_config={"max_steps": 5},
        tools=[ReadFileTool()],
    )

    result = await runner.run(RunRequest(prompt="read all files"))

    assert result.status == "completed"
    assert result.final_text == "done"
    assert len(provider.requests) == 4
    store.close()


@pytest.mark.asyncio
async def test_agent_treats_length_finish_as_limit(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="partial"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="length"),
            ],
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="length limit summary"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
        ]
    )
    runner, store = make_test_runner(tmp_path, provider)

    result = await runner.run(RunRequest(prompt="answer"))

    assert result.status == "limit_reached"
    assert result.final_text == "length limit summary"
    assert provider.requests[-1].tools == provider.requests[-2].tools
    assert provider.requests[-1].tool_choice == "none"
    store.close()


@pytest.mark.asyncio
async def test_agent_persists_reasoning_and_round_trips_it_for_tool_calls(
    tmp_path: Path,
) -> None:
    (tmp_path / "input.txt").write_text("evidence", encoding="utf-8")
    provider = ScriptedProvider(
        [
            [
                ModelEvent(kind=ModelEventKind.REASONING_DELTA, text="inspect the file"),
                *tool_turn("read-1", "read_file", '{"path":"input.txt"}'),
            ],
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="done"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
        ]
    )
    runner, store = make_test_runner(tmp_path, provider, tools=[ReadFileTool()])

    result = await runner.run(RunRequest(prompt="inspect"))

    assert result.status == "completed"
    assistant_tool_message = next(
        message
        for message in provider.requests[1].messages
        if message.role == Role.ASSISTANT and message.tool_calls
    )
    assert assistant_tool_message.reasoning_content == "inspect the file"
    assert assistant_tool_message.to_openai()["reasoning_content"] == "inspect the file"
    events = store.list_events(result.session_id)
    reasoning = [event for event in events if event["type"] == "assistant.reasoning.delta"]
    assert reasoning[0]["payload"]["text"] == "inspect the file"
    response = next(event for event in events if event["type"] == "model.response")
    assert response["payload"]["reasoning_chars"] == len("inspect the file")
    store.close()


@pytest.mark.asyncio
async def test_agent_retries_reasoning_only_response_without_persisting_empty_message(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ModelEvent(kind=ModelEventKind.REASONING_DELTA, text="reasoning only"),
                ModelEvent(
                    kind=ModelEventKind.USAGE,
                    input_tokens=10,
                    output_tokens=12,
                    provider_metadata={
                        "raw_usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 12,
                            "completion_tokens_details": {"reasoning_tokens": 12},
                        }
                    },
                ),
                ModelEvent(
                    kind=ModelEventKind.FINISH,
                    finish_reason="stop",
                    provider_metadata={"observed_delta_fields": ["reasoning_content"]},
                ),
            ],
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="recovered"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
        ]
    )
    runner, store = make_test_runner(tmp_path, provider)

    result = await runner.run(RunRequest(prompt="answer"))

    assert result.status == "completed"
    assert result.final_text == "recovered"
    assert len(provider.requests) == 2
    assert all(
        message.assistant_payload_error() is None
        for message in store.load_messages(result.session_id)
    )
    events = store.list_events(result.session_id)
    empty = next(event for event in events if event["type"] == "model.empty_response")
    assert empty["payload"]["likely_cause"] == "reasoning_without_final_content"
    assert empty["payload"]["will_retry"] is True
    usage = next(event for event in events if event["type"] == "model.usage")
    assert usage["payload"]["turn_usage"]["completion_tokens_details"] == {"reasoning_tokens": 12}
    store.close()


@pytest.mark.asyncio
async def test_agent_fails_two_empty_responses_without_poisoning_history(tmp_path: Path) -> None:
    empty_turn = [
        ModelEvent(kind=ModelEventKind.REASONING_DELTA, text="unfinished"),
        ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
    ]
    provider = ScriptedProvider(
        [
            empty_turn,
            empty_turn,
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="empty response summary"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
        ]
    )
    runner, store = make_test_runner(tmp_path, provider)

    result = await runner.run(RunRequest(prompt="answer"))

    assert result.status == "failed"
    assert "连续 2 次" in (result.error or "")
    messages = store.load_messages(result.session_id)
    assert [message.role for message in messages] == [Role.USER, Role.ASSISTANT]
    assert messages[-1].content == "empty response summary"
    empty_events = [
        event
        for event in store.list_events(result.session_id)
        if event["type"] == "model.empty_response"
    ]
    assert [event["payload"]["will_retry"] for event in empty_events] == [True, False]
    store.close()


@pytest.mark.asyncio
async def test_agent_drops_legacy_empty_assistant_before_provider_request(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="healthy"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ]
        ]
    )
    runner, store = make_test_runner(tmp_path, provider)
    session_id = store.create_session(tmp_path)
    invalid = {
        "role": "assistant",
        "content": None,
        "reasoning_content": None,
        "name": None,
        "tool_call_id": None,
        "tool_calls": [],
    }
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            INSERT INTO messages(
                session_id, run_id, position, role, content, message_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, "legacy", 1, "assistant", None, json.dumps(invalid), "now"),
        )

    result = await runner.run(RunRequest(prompt="recover", session_id=session_id))

    assert result.status == "completed"
    assert all(
        message.assistant_payload_error() is None for message in provider.requests[0].messages
    )
    dropped = next(
        event
        for event in store.list_events(session_id)
        if event["type"] == "context.invalid_message_dropped"
    )
    assert dropped["payload"]["position"] == 1
    store.close()


@pytest.mark.asyncio
async def test_agent_enforces_cost_before_requested_tool_runs(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            [
                ModelEvent(
                    kind=ModelEventKind.TOOL_CALL_DELTA,
                    tool_index=0,
                    tool_call_id="read",
                    tool_name="read_file",
                    arguments_delta='{"path":"input.txt"}',
                ),
                ModelEvent(
                    kind=ModelEventKind.USAGE,
                    input_tokens=1_000_000,
                    output_tokens=0,
                ),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="tool_calls"),
            ]
        ]
    )
    runner, store = make_test_runner(
        tmp_path,
        provider,
        agent_config={"max_cost_usd": 0.5},
        model_config={"input_cost_per_million": 1, "output_cost_per_million": 1},
        tools=[ReadFileTool()],
    )

    result = await runner.run(RunRequest(prompt="read"))

    assert result.status == "limit_reached"
    assert result.cost_usd == 1
    event_types = [event["type"] for event in store.list_events(result.session_id)]
    assert event_types[-3:] == ["assistant.message", "run.limit_reached", "run.finished"]
    assert store.list_events(result.session_id)[-1]["payload"]["cost_usd"] == 1
    assert "run.finalizing" in event_types
    store.close()


@pytest.mark.asyncio
async def test_agent_enforces_cumulative_tool_output_limit(tmp_path: Path) -> None:
    (tmp_path / "input.txt").write_text("x" * 200, encoding="utf-8")
    provider = ScriptedProvider([tool_turn("read", "read_file", '{"path":"input.txt"}')])
    runner, store = make_test_runner(
        tmp_path,
        provider,
        agent_config={"max_total_tool_output_bytes": 100},
        tools=[ReadFileTool()],
    )

    result = await runner.run(RunRequest(prompt="read"))

    assert result.status == "limit_reached"
    assert "累计 Tool 输出" in (result.error or "")
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "expected_approval_calls"),
    [(ApprovalScope.ONCE, 2), (ApprovalScope.SESSION, 1)],
)
async def test_agent_applies_once_and_session_approval_scope(
    tmp_path: Path,
    scope: ApprovalScope,
    expected_approval_calls: int,
) -> None:
    provider = ScriptedProvider(
        [
            tool_turn("danger-1", "destructive_test", "{}"),
            tool_turn("danger-2", "destructive_test", "{}"),
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="done"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
        ]
    )
    tool = DestructiveTestTool()
    runner, store = make_test_runner(tmp_path, provider, tools=[tool])
    approval = ApprovalHandlerStub(scope)
    runner.approval_handler = approval

    result = await runner.run(RunRequest(prompt="perform twice"))

    assert result.status == "completed"
    assert tool.executions == 2
    assert approval.calls == expected_approval_calls
    store.close()


@pytest.mark.asyncio
async def test_agent_persists_always_approval_across_sessions(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            tool_turn("danger-1", "destructive_test", "{}"),
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="first done"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
            tool_turn("danger-2", "destructive_test", "{}"),
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="second done"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
        ]
    )
    tool = DestructiveTestTool()
    runner, store = make_test_runner(tmp_path, provider, tools=[tool])
    approval = ApprovalHandlerStub(ApprovalScope.ALWAYS)
    runner.approval_handler = approval

    first = await runner.run(RunRequest(prompt="first session"))
    second = await runner.run(RunRequest(prompt="second session"))

    assert first.status == second.status == "completed"
    assert first.session_id != second.session_id
    assert tool.executions == 2
    assert approval.calls == 1
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "expected_approval_calls"),
    [(ApprovalScope.ONCE, 2), (ApprovalScope.SESSION, 1)],
)
async def test_command_family_approval_reuses_different_pytest_arguments(
    tmp_path: Path,
    scope: ApprovalScope,
    expected_approval_calls: int,
) -> None:
    provider = ScriptedProvider(
        [
            tool_turn(
                "pytest-a",
                "run_command",
                '{"argv":["python3","-m","pytest","tests/a.py"]}',
            ),
            tool_turn(
                "pytest-b",
                "run_command",
                '{"argv":["python3","-m","pytest","tests/b.py","-q"]}',
            ),
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="tests done"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
        ]
    )
    tool = CommandApprovalTestTool()
    runner, store = make_test_runner(tmp_path, provider, tools=[tool])
    approval = ApprovalHandlerStub(scope)
    runner.approval_handler = approval

    result = await runner.run(RunRequest(prompt="run two test selections"))

    assert result.status == "completed"
    assert tool.executions == 2
    assert approval.calls == expected_approval_calls
    store.close()


@pytest.mark.asyncio
async def test_command_family_always_approval_reuses_across_sessions(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            tool_turn(
                "pytest-a",
                "run_command",
                '{"argv":["python3","-m","pytest","tests/a.py"]}',
            ),
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="first done"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
            tool_turn(
                "pytest-b",
                "run_command",
                '{"argv":["python3","-m","pytest","tests/b.py"]}',
            ),
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="second done"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
        ]
    )
    tool = CommandApprovalTestTool()
    runner, store = make_test_runner(tmp_path, provider, tools=[tool])
    approval = ApprovalHandlerStub(ApprovalScope.ALWAYS)
    runner.approval_handler = approval

    first = await runner.run(RunRequest(prompt="first selection"))
    second = await runner.run(RunRequest(prompt="second selection"))

    assert first.status == second.status == "completed"
    assert approval.calls == 1
    assert tool.executions == 2
    store.close()


@pytest.mark.asyncio
async def test_agent_automatically_activates_multiple_complementary_skills(
    tmp_path: Path,
) -> None:
    for name in ("migration", "performance"):
        skill_dir = tmp_path / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} guidance.\n---\nUse evidence.",
            encoding="utf-8",
        )
    provider = ScriptedProvider(
        [
            tool_turn(
                "skill-1",
                "activate_skill",
                '{"name":"migration","reason":"migration task"}',
            ),
            tool_turn(
                "skill-2",
                "activate_skill",
                '{"name":"performance","reason":"performance task"}',
            ),
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="combined"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
        ]
    )
    runner, store = make_test_runner(tmp_path, provider)

    result = await runner.run(RunRequest(prompt="migrate and tune"))

    assert result.status == "completed"
    assert not runner.skills.active
    deliveries = [
        entry.skill_delivery
        for entry in store.load_positioned_messages(result.session_id)
        if entry.skill_delivery is not None
    ]
    assert [item.skill_name for item in deliveries] == ["migration", "performance"]
    store.close()


@pytest.mark.asyncio
async def test_agent_compaction_trigger_uses_model_input_budget(tmp_path, monkeypatch):
    from bot.core.models import InputTokenEstimate

    provider = ScriptedProvider(
        [
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="done"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ]
        ]
    )
    runner, store = make_test_runner(tmp_path, provider)
    reserved = runner._token_budget.target_input_limit + 1
    provider.estimate_input_tokens = lambda _: InputTokenEstimate(
        tokens=reserved - 256, budget_tokens=reserved, source="test_tokenizer"
    )
    consolidations = []

    async def consolidate(**kwargs):
        consolidations.append(kwargs)
        return None

    monkeypatch.setattr(runner, "_consolidate_conversation", consolidate)
    try:
        result = await runner.run(RunRequest(prompt="hello"))
        assert result.status == "completed"
        assert len(consolidations) == 1
        assert not consolidations[0]["force"]
        assert len(provider.requests) == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_agent_rechecks_final_request_budget_before_provider_call(tmp_path):
    from bot.core.agent import _ModelRequestFailure
    from bot.core.models import InputTokenEstimate

    provider = ScriptedProvider([])
    runner, store = make_test_runner(tmp_path, provider)
    provider.estimate_input_tokens = lambda _: InputTokenEstimate(
        tokens=runner._token_budget.hard_input_limit,
        budget_tokens=runner._token_budget.hard_input_limit + 256,
        source="test_tokenizer",
    )
    try:
        with pytest.raises(_ModelRequestFailure) as exc:
            await runner._request_model_with_retries(
                ModelRequest(model="mock", messages=[ChatMessage(role=Role.USER, content="hello")]),
                session_id="unused",
                run_id="unused",
                step=1,
                required_memory_tool=None,
                input_tokens=0,
                output_tokens=0,
            )
        assert exc.value.error.kind == ProviderErrorKind.CONTEXT_LENGTH
        assert not provider.requests
    finally:
        store.close()
