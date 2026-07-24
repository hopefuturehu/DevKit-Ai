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
from bot.memory import MemoryConsolidator
from bot.policy import DefaultPolicyEngine
from bot.providers import ModelProvider, ProviderError
from bot.sessions import SQLiteSessionStore
from bot.skills import SkillCatalog, SkillManager
from bot.tools import Tool, ToolAnnotations, ToolRegistry, ToolResult
from bot.tools.builtins import ReadFileTool


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


def make_test_runner(
    tmp_path: Path,
    provider: ModelProvider,
    *,
    agent_config: dict | None = None,
    model_config: dict | None = None,
    tools: list | None = None,
    context_config: dict | None = None,
    memory_config: dict | None = None,
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
    )
    return runner, store


@pytest.mark.asyncio
async def test_consolidated_memory_cards_are_retrieved_into_the_next_run(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="first done"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ]
        ]
    )
    runner, store = make_test_runner(
        tmp_path,
        provider,
        memory_config={"auto_consolidate": False},
    )
    consolidator = MemoryConsolidator(
        config=runner.config,
        workspace=tmp_path,
        provider=provider,
        store=store,
        event_bus=runner.event_bus,
    )
    runner.memory_consolidator = consolidator

    first = await runner.run(RunRequest(prompt="必须始终运行 pytest"))
    episode_id = store.list_memory_episodes(first.session_id, status="pending")[0]["id"]
    provider.turns.append(
        [
            ModelEvent(
                kind=ModelEventKind.TEXT_DELTA,
                text=json.dumps(
                    {
                        "episodes": [
                            {
                                "episode_id": episode_id,
                                "title": "pytest 约束",
                                "summary": "用户要求始终运行 pytest。",
                                "keywords": ["pytest"],
                            }
                        ],
                        "candidates": [
                            {
                                "operation": "upsert",
                                "kind": "constraint",
                                "scope": "workspace",
                                "memory_key": "constraint.verification.pytest",
                                "content": "必须始终运行 pytest。",
                                "source_positions": [1],
                                "evidence_refs": ["message:1"],
                                "confidence": 0.99,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
            ),
            ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
        ]
    )
    consolidation = await consolidator.consolidate(first.session_id, force=True)
    provider.turns.append(
        [
            ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="second done"),
            ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
        ]
    )

    second = await runner.run(RunRequest(prompt="继续修改", session_id=first.session_id))

    assert first.status == second.status == "completed"
    assert consolidation.cards_created == 1
    injected = [
        message
        for message in provider.requests[-1].messages
        if message.name == "consolidated_memory"
    ]
    assert len(injected) == 1
    assert "必须始终运行 pytest" in (injected[0].content or "")
    assert injected[0].role == Role.USER
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
async def test_agent_consolidates_episodes_and_resumes_from_cursor(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="first"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="second"),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ],
        ]
    )
    runner, store = make_test_runner(
        tmp_path,
        provider,
        model_config={"context_window_tokens": 8_000},
        context_config={
            "max_input_tokens": 6_500,
            "auto_compact_threshold": 0.5,
            "output_reserve_tokens": 500,
            "protocol_reserve_tokens": 500,
            "safety_margin_tokens": 500,
            "recent_conversation_tokens": 1_500,
        },
    )
    session_id = store.create_session(tmp_path)
    for index in range(12):
        role = Role.USER if index % 2 == 0 else Role.ASSISTANT
        store.append_message(
            session_id,
            "seed",
            ChatMessage(role=role, content=f"old-{index}-" + "中" * 1_000),
        )
    episode_id = store.seal_memory_episodes(session_id)[0]["id"]
    consolidator = MemoryConsolidator(
        config=runner.config,
        workspace=tmp_path,
        provider=provider,
        store=store,
        event_bus=runner.event_bus,
    )
    runner.memory_consolidator = consolidator
    provider.turns.insert(
        0,
        [
            ModelEvent(
                kind=ModelEventKind.TEXT_DELTA,
                text=json.dumps(
                    {
                        "episodes": [
                            {
                                "episode_id": episode_id,
                                "title": "旧会话",
                                "objective": "保留旧任务上下文",
                                "summary": "旧会话完成了多轮讨论。",
                                "keywords": ["old"],
                                "topics": ["history"],
                                "depth": "deep",
                            }
                        ],
                        "candidates": [],
                    }
                ),
            ),
            ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
        ],
    )

    first = await runner.run(RunRequest(prompt="new-one", session_id=session_id))
    second = await runner.run(RunRequest(prompt="new-two", session_id=session_id))

    assert first.status == second.status == "completed"
    assert store.consolidated_memory_cursor(session_id) == 12
    assert store.latest_context_snapshot(session_id) is None
    second_messages = provider.requests[2].messages
    summaries = [message for message in second_messages if message.name == "consolidated_episodes"]
    assert len(summaries) == 1
    assert not any("old-0-" in (message.content or "") for message in second_messages)
    store.close()


@pytest.mark.asyncio
async def test_agent_retries_provider_context_error_once(tmp_path: Path) -> None:
    provider = ContextRetryProvider()
    runner, store = make_test_runner(tmp_path, provider)

    result = await runner.run(RunRequest(prompt="x" * 3_000))

    assert result.status == "completed"
    assert result.final_text == "recovered"
    assert len(provider.requests) == 2
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
    assert "large_0" not in {tool.name for tool in initial}
    assert activated.success
    assert "large_0" in {tool.name for tool in after}
    store.close()


@pytest.mark.asyncio
async def test_manual_compaction_consolidates_episodes_immediately(tmp_path: Path) -> None:
    provider = ScriptedProvider([])
    runner, store = make_test_runner(tmp_path, provider)
    session_id = store.create_session(tmp_path)
    store.append_message(session_id, "seed", ChatMessage(role=Role.USER, content="objective"))
    store.append_message(session_id, "seed", ChatMessage(role=Role.ASSISTANT, content="result"))
    episode_id = store.seal_memory_episodes(session_id)[0]["id"]
    provider.turns.append(
        [
            ModelEvent(
                kind=ModelEventKind.TEXT_DELTA,
                text=json.dumps(
                    {
                        "episodes": [
                            {
                                "episode_id": episode_id,
                                "title": "目标完成",
                                "objective": "objective",
                                "summary": "任务得到 result。",
                                "keywords": ["objective", "result"],
                                "topics": ["task"],
                                "depth": "deep",
                            }
                        ],
                        "candidates": [],
                    }
                ),
            ),
            ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
        ]
    )
    runner.memory_consolidator = MemoryConsolidator(
        config=runner.config,
        workspace=tmp_path,
        provider=provider,
        store=store,
        event_bus=runner.event_bus,
    )

    result = await runner.compact_session(session_id)
    status = runner.context_status(session_id)

    assert result["compacted"] is True
    assert result["cursor_position"] == 2
    assert status["delta_messages"] == 0
    assert status["compression"]["cursor_position"] == 2
    assert status["compression"]["method"] == "llm_episode_consolidation"
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
    assert "analysis" in skills.active
    event_types = [event.type for event in memory.events]
    assert EventType.SKILL_DISCOVERED in event_types
    assert EventType.SKILL_ACTIVATED in event_types
    assert EventType.TOOL_COMPLETED in event_types
    assert EventType.MODEL_USAGE in event_types
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
async def test_agent_stops_repeated_idempotent_results(tmp_path: Path) -> None:
    (tmp_path / "input.txt").write_text("unchanged", encoding="utf-8")
    provider = ScriptedProvider(
        [
            tool_turn("read-1", "read_file", '{"path":"input.txt"}'),
            tool_turn("read-2", "read_file", '{"path":"input.txt"}'),
            tool_turn("read-3", "read_file", '{"path":"input.txt"}'),
        ]
    )
    config = AppConfig.model_validate(
        {
            "model": {"base_url": "https://unused", "name": "mock"},
            "storage": {"state_path": str(tmp_path / "state.db")},
            "skills": {"path": str(tmp_path / "skills")},
            "agent": {"max_steps": 5},
        }
    )
    catalog = SkillCatalog(tmp_path / "skills")
    catalog.scan()
    store = SQLiteSessionStore(tmp_path / "state.db")
    registry = ToolRegistry()
    registry.register(ReadFileTool())
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
    )

    result = await runner.run(RunRequest(prompt="read until it changes"))

    assert result.status == "failed"
    assert "无进展" in (result.error or "")
    assert len(provider.requests) == 3
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
            ]
        ]
    )
    runner, store = make_test_runner(tmp_path, provider)

    result = await runner.run(RunRequest(prompt="answer"))

    assert result.status == "limit_reached"
    assert result.final_text == "partial"
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
    provider = ScriptedProvider([empty_turn, empty_turn])
    runner, store = make_test_runner(tmp_path, provider)

    result = await runner.run(RunRequest(prompt="answer"))

    assert result.status == "failed"
    assert "连续 2 次" in (result.error or "")
    messages = store.load_messages(result.session_id)
    assert [message.role for message in messages] == [Role.USER]
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
    assert store.list_events(result.session_id)[-1]["type"] == "run.failed"
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
    assert list(runner.skills.active) == ["migration", "performance"]
    store.close()
