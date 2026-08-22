import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from bot.cli.runtime import build_runtime
from bot.config.models import AppConfig
from bot.core import AgentRunner
from bot.core.context import ContextAssembler, ContextLayer, ContextTrust
from bot.core.events import EventBus, MemoryEventSink
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
from bot.memory import ExtractedMemoryCandidate, MarkdownMemoryStore, MemoryExtractor, MemoryKind
from bot.policy import DefaultPolicyEngine
from bot.providers import ModelProvider
from bot.sessions import SQLiteSessionStore
from bot.skills import SkillCatalog, SkillManager
from bot.tools import ToolRegistry


def candidate(
    content: str,
    *,
    key: str = "testing.primary-command",
    positions: list[int] | None = None,
) -> ExtractedMemoryCandidate:
    return ExtractedMemoryCandidate(
        kind=MemoryKind.PROCEDURE,
        scope="workspace",
        memory_key=key,
        content=content,
        confidence=0.91,
        evidence_positions=positions or [1, 2],
    )


def test_markdown_memory_separates_user_auto_conflict_and_forget(tmp_path: Path) -> None:
    memory = MarkdownMemoryStore(tmp_path / "memory")

    user_id = memory.add_user_memory("默认使用简体中文")
    assert user_id.startswith("user-")
    assert memory.add_user_memory("默认使用简体中文") == user_id

    added = memory.consolidate(
        [candidate("修改后运行 pytest tests/unit。")],
        session_id="session-1",
        run_id="run-1",
    )
    assert added.model_dump() == {"added": 1, "merged": 0, "conflicts": 0, "suppressed": 0}

    merged = memory.consolidate(
        [candidate("修改后运行 pytest tests/unit。", positions=[3])],
        session_id="session-1",
        run_id="run-2",
    )
    assert merged.merged == 1
    record = memory.get("procedure.testing.primary-command")
    assert record is not None
    assert sum(len(item.positions) for item in record.evidence) == 3

    conflict = memory.consolidate(
        [candidate("修改后只运行 tox。")],
        session_id="session-2",
        run_id="run-3",
    )
    assert conflict.conflicts == 1
    assert "存在冲突" in memory.index_path.read_text(encoding="utf-8")
    assert "tox" in memory.conflicts_path.read_text(encoding="utf-8")

    assert memory.search("pytest", limit=8)[0].key == "procedure.testing.primary-command"
    assert memory.forget("procedure.testing.primary-command")
    assert not memory.search("pytest", limit=8)
    assert "procedure.testing.primary-command" in memory.forget_path.read_text(encoding="utf-8")
    suppressed = memory.consolidate(
        [candidate("修改后运行 pytest tests/unit。")],
        session_id="session-3",
        run_id="run-4",
    )
    assert suppressed.suppressed == 1


def test_markdown_memory_imports_legacy_ids_idempotently(tmp_path: Path) -> None:
    memory = MarkdownMemoryStore(tmp_path / "memory")
    legacy = [{"id": 7, "content": "use pnpm"}]

    assert memory.import_legacy(legacy) == [7]
    assert memory.import_legacy(legacy) == [7]
    records = memory.list_user_memories()
    assert [(record.id, record.content) for record in records] == [("legacy-7", "use pnpm")]
    assert memory.forget("7")


class ExtractionProvider(ModelProvider):
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities()

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        response = {
            "candidates": [
                {
                    "kind": "procedure",
                    "scope": "workspace",
                    "memory_key": "testing.primary-command",
                    "content": "项目测试命令是 pytest tests/unit。",
                    "confidence": 0.92,
                    "evidence_positions": [1, 2],
                },
                {
                    "kind": "user_preference",
                    "scope": "workspace",
                    "memory_key": "output.verbosity",
                    "content": "用户偏好极简输出。",
                    "confidence": 0.9,
                    "evidence_positions": [2],
                },
                {
                    "kind": "workspace_fact",
                    "scope": "workspace",
                    "memory_key": "service.token",
                    "content": "api_key=should-not-persist",
                    "confidence": 0.99,
                    "evidence_positions": [1],
                },
            ]
        }
        yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text=json.dumps(response))
        yield ModelEvent(kind=ModelEventKind.USAGE, input_tokens=120, output_tokens=60)
        yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")


@pytest.mark.asyncio
async def test_extractor_processes_completed_root_run_and_keeps_sqlite_evidence(
    tmp_path: Path,
) -> None:
    config = AppConfig.model_validate(
        {
            "model": {"base_url": "https://unused", "name": "memory-model"},
            "memory": {"path": str(tmp_path / "memory")},
            "storage": {"state_path": str(tmp_path / "state.db")},
        }
    )
    provider = ExtractionProvider()
    store = SQLiteSessionStore(tmp_path / "state.db")
    session_id = store.create_session(tmp_path)
    store.start_run(session_id, "run-1")
    store.append_message(
        session_id,
        "run-1",
        ChatMessage(role=Role.USER, content="请验证项目测试命令。"),
    )
    store.append_message(
        session_id,
        "run-1",
        ChatMessage(role=Role.ASSISTANT, content="已验证 pytest tests/unit 可以通过。"),
    )
    store.finish_run("run-1", "completed")
    memory = MarkdownMemoryStore(tmp_path / "memory")
    events = MemoryEventSink()
    extractor = MemoryExtractor(
        config=config,
        workspace=tmp_path,
        provider=provider,
        store=store,
        memory_store=memory,
        event_bus=EventBus([store, events]),
    )

    result = await extractor.extract_pending()

    assert result["processed"] == 1
    assert result["candidates"] == 1
    assert result["added"] == 1
    assert len(provider.requests) == 1
    extracted = store.list_memory_extractions()
    assert extracted[0]["status"] == "ready"
    assert extracted[0]["candidate_count"] == 1
    record = memory.get("procedure.testing.primary-command")
    assert record is not None
    assert record.evidence[0].positions == [1, 2]
    assert (await extractor.extract_pending())["processed"] == 0
    store.close()


class NoopProvider(ModelProvider):
    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities()

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")


def test_agent_loads_physical_memory_files_with_separate_trust(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {
            "model": {"base_url": "https://unused", "name": "mock"},
            "storage": {"state_path": str(tmp_path / "state.db")},
        }
    )
    memory = MarkdownMemoryStore(tmp_path / "memory")
    memory.add_user_memory("默认使用中文。")
    memory.consolidate(
        [candidate("测试命令是 pytest。")],
        session_id="session-1",
        run_id="run-1",
    )
    store = SQLiteSessionStore(tmp_path / "state.db")
    store.create_session(tmp_path, session_id="session-1")
    store.start_run("session-1", "run-1")
    store.append_message(
        "session-1",
        "run-1",
        ChatMessage(role=Role.USER, content="请验证测试命令。"),
    )
    store.append_message(
        "session-1",
        "run-1",
        ChatMessage(role=Role.ASSISTANT, content="测试命令是 pytest。"),
    )
    store.finish_run("run-1", "completed")
    catalog = SkillCatalog(tmp_path / "skills")
    catalog.scan()
    runner = AgentRunner(
        config=config,
        workspace=tmp_path,
        provider=NoopProvider(),
        tool_registry=ToolRegistry(),
        policy=DefaultPolicyEngine(config.permissions, tmp_path),
        execution_target=LocalExecutionTarget(),
        skills=SkillManager(catalog),
        context=ContextAssembler(workspace=tmp_path, skill_catalog=catalog),
        store=store,
        event_bus=EventBus([store]),
        memory_store=memory,
    )

    items = runner._memory_context_items()  # noqa: SLF001

    assert [(item.message.name, item.layer, item.trust) for item in items] == [
        ("explicit_memory", ContextLayer.MEMORY, ContextTrust.USER),
        (
            "automatic_memory",
            ContextLayer.AUTOMATIC_MEMORY,
            ContextTrust.UNTRUSTED,
        ),
    ]
    definitions, _ = runner._select_tool_definitions("session", [])  # noqa: SLF001
    assert {definition.name for definition in definitions} >= {
        "search_memory",
        "load_memory_evidence",
    }
    search = runner._search_memory(  # noqa: SLF001
        ToolCall(id="call-1", name="search_memory", arguments={"query": "pytest"})
    )
    assert search.success
    assert "procedure.testing.primary-command" in search.output
    evidence = runner._load_memory_evidence(  # noqa: SLF001
        ToolCall(
            id="call-2",
            name="load_memory_evidence",
            arguments={"memory": "procedure.testing.primary-command"},
        )
    )
    assert evidence.success
    assert "请验证测试命令" in evidence.output
    store.close()


def test_runtime_migrates_legacy_sqlite_memory_and_protects_markdown_root(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / ".bot" / "state.db"
    store = SQLiteSessionStore(state_path)
    store.add_memory("legacy preference")
    store.close()
    (tmp_path / ".env").write_text("BOT_MODEL_API_KEY=test-key\n", encoding="utf-8")

    runtime = build_runtime(
        workspace=tmp_path,
        config_overrides={
            "model": {"base_url": "https://unused", "name": "mock"},
            "subagents": {"enabled": False},
            "storage": {"state_path": str(state_path)},
            "memory": {"path": str(tmp_path / ".bot" / "memory")},
        },
    )

    assert runtime.store.list_memories() == []
    assert runtime.memory_store is not None
    assert runtime.memory_store.list_user_memories()[0].content == "legacy preference"
    assert runtime.memory_store.root in runtime.runner.denied_tool_paths
    runtime.close()
