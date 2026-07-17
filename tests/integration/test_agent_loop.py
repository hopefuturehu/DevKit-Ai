import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from bot.config.models import AppConfig
from bot.core import AgentRunner, RunRequest
from bot.core.approval import AllowApprovalHandler
from bot.core.context import ContextAssembler
from bot.core.events import EventBus, EventType, MemoryEventSink
from bot.core.models import ModelCapabilities, ModelEvent, ModelEventKind, ModelRequest
from bot.execution import LocalExecutionTarget
from bot.policy import DefaultPolicyEngine
from bot.providers import ModelProvider
from bot.sessions import SQLiteSessionStore
from bot.skills import SkillCatalog, SkillManager
from bot.tools import ToolRegistry
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
):
    model = {"base_url": "https://unused", "name": "mock"}
    model.update(model_config or {})
    config = AppConfig.model_validate(
        {
            "model": model,
            "agent": agent_config or {},
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
