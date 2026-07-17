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
    assert len(provider.requests) == 3
    assert "analysis" in skills.active
    event_types = [event.type for event in memory.events]
    assert EventType.SKILL_DISCOVERED in event_types
    assert EventType.SKILL_ACTIVATED in event_types
    assert EventType.TOOL_COMPLETED in event_types
    assert store.load_messages(result.session_id)[-1].content == "Completed from evidence."
    store.close()
