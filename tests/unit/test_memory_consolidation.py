import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

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
from bot.memory import MemoryConsolidator
from bot.memory.retrieval import rank_memory_cards
from bot.providers import ModelProvider
from bot.sessions import SQLiteSessionStore


class ConsolidationProvider(ModelProvider):
    def __init__(self, responder: Callable[[ModelRequest], dict | str]) -> None:
        self.responder = responder
        self.requests: list[ModelRequest] = []

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities()

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        response = self.responder(request)
        text = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
        yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text=text)
        yield ModelEvent(
            kind=ModelEventKind.USAGE,
            input_tokens=321,
            output_tokens=123,
        )
        yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")


def make_consolidator(
    tmp_path: Path,
    provider: ModelProvider,
    *,
    min_confidence: float = 0.65,
    auto_consolidate: bool = False,
    session_gate: int = 5,
    time_gate_hours: float = 24,
) -> tuple[MemoryConsolidator, SQLiteSessionStore, MemoryEventSink]:
    config = AppConfig.model_validate(
        {
            "model": {
                "base_url": "https://unused",
                "name": "memory-model",
            },
            "memory": {
                "auto_consolidate": auto_consolidate,
                "session_gate": session_gate,
                "time_gate_hours": time_gate_hours,
                "min_confidence": min_confidence,
            },
            "storage": {"state_path": str(tmp_path / "state.db")},
        }
    )
    store = SQLiteSessionStore(tmp_path / "state.db")
    events = MemoryEventSink()
    consolidator = MemoryConsolidator(
        config=config,
        workspace=tmp_path,
        provider=provider,
        store=store,
        event_bus=EventBus([store, events]),
    )
    return consolidator, store, events


def append_verified_run(store: SQLiteSessionStore, session_id: str, run_id: str) -> None:
    store.start_run(session_id, run_id)
    store.append_message(
        session_id,
        run_id,
        ChatMessage(role=Role.USER, content="必须使用 pytest 验证修改。"),
    )
    store.append_message(
        session_id,
        run_id,
        ChatMessage(
            role=Role.ASSISTANT,
            tool_calls=[ToolCall(id="tool-test", name="run_command", arguments={})],
        ),
    )
    store.append_message(
        session_id,
        run_id,
        ChatMessage(
            role=Role.TOOL,
            name="run_command",
            tool_call_id="tool-test",
            content="2 passed",
        ),
    )
    store.record_tool_run(
        session_id=session_id,
        run_id=run_id,
        tool_call_id="tool-test",
        tool_name="run_command",
        arguments={"argv": ["pytest"]},
        status="completed",
        result={
            "success": True,
            "output": "2 passed",
            "error": None,
            "metadata": {},
            "truncated": False,
        },
    )
    store.append_message(
        session_id,
        run_id,
        ChatMessage(role=Role.ASSISTANT, content="测试已通过。"),
    )
    store.finish_run(run_id, "completed")


@pytest.mark.asyncio
async def test_consolidation_creates_versioned_cards_and_rejects_unverified_candidates(
    tmp_path: Path,
) -> None:
    def respond(request: ModelRequest) -> dict:
        source = json.loads(request.messages[-1].content or "{}")
        episode_id = source["episodes"][0]["episode_id"]
        return {
            "episodes": [
                {
                    "episode_id": episode_id,
                    "title": "使用 pytest 验证修改",
                    "summary": "用户要求使用 pytest，工具执行成功。",
                    "keywords": ["pytest", "verification"],
                }
            ],
            "candidates": [
                {
                    "operation": "upsert",
                    "kind": "constraint",
                    "scope": "workspace",
                    "memory_key": "constraint.verification.pytest",
                    "content": "修改后必须使用 pytest 验证。",
                    "source_positions": [1],
                    "evidence_refs": ["message:1"],
                    "confidence": 0.98,
                },
                {
                    "operation": "upsert",
                    "kind": "verification",
                    "scope": "session",
                    "memory_key": "verification.pytest.latest",
                    "content": "pytest 已通过。",
                    "source_positions": [3, 4],
                    "evidence_refs": ["tool:tool-test"],
                    "confidence": 0.95,
                },
                {
                    "operation": "upsert",
                    "kind": "constraint",
                    "scope": "workspace",
                    "memory_key": "constraint.model.invented",
                    "content": "模型自行提出的约束。",
                    "source_positions": [4],
                    "evidence_refs": ["message:4"],
                    "confidence": 0.99,
                },
                {
                    "operation": "upsert",
                    "kind": "artifact",
                    "scope": "session",
                    "memory_key": "artifact.fictional",
                    "content": "虚构的产物。",
                    "source_positions": [4],
                    "evidence_refs": [],
                    "confidence": 0.99,
                },
            ],
        }

    provider = ConsolidationProvider(respond)
    consolidator, store, events = make_consolidator(tmp_path, provider)
    session_id = store.create_session(tmp_path)
    append_verified_run(store, session_id, "run-1")

    captured = await consolidator.after_run(session_id=session_id, run_id="run-1")
    result = await consolidator.consolidate(session_id, force=True)

    assert captured.reason == "auto_consolidation_disabled"
    assert result.consolidated is True
    assert result.episodes_consolidated == 1
    assert result.candidates_accepted == 2
    assert result.candidates_rejected == 2
    assert result.cards_created == 2
    assert result.input_tokens == 321
    assert result.output_tokens == 123
    assert provider.requests[0].temperature == 0
    assert provider.requests[0].tools == []

    cards = store.list_active_memory_cards(
        workspace=tmp_path,
        session_id=session_id,
    )
    assert {item["kind"] for item in cards} == {"constraint", "verification"}
    assert all(store.list_memory_card_versions(item["id"])[0]["version"] == 1 for item in cards)
    assert store.list_memory_episodes(session_id)[0]["status"] == "consolidated"
    assert EventType.MEMORY_CONSOLIDATION_COMPLETED in {event.type for event in events.events}
    store.close()


@pytest.mark.asyncio
async def test_after_run_auto_consolidates_when_episode_gate_is_reached(
    tmp_path: Path,
) -> None:
    def respond(request: ModelRequest) -> dict:
        source = json.loads(request.messages[-1].content or "{}")
        episode_id = source["episodes"][0]["episode_id"]
        return {
            "episodes": [
                {
                    "episode_id": episode_id,
                    "title": "自动整合",
                    "summary": "Episode 达到门槛后自动整合。",
                    "keywords": ["auto"],
                }
            ],
            "candidates": [],
        }

    provider = ConsolidationProvider(respond)
    consolidator, store, _ = make_consolidator(
        tmp_path,
        provider,
        auto_consolidate=True,
        session_gate=1,
    )
    session_id = store.create_session(tmp_path)
    append_verified_run(store, session_id, "run-1")

    result = await consolidator.after_run(session_id=session_id, run_id="run-1")

    assert result.consolidated is True
    assert result.trigger == "auto"
    assert result.episodes_consolidated == 1
    assert len(provider.requests) == 1
    assert store.list_memory_episodes(session_id)[0]["status"] == "consolidated"
    store.close()


@pytest.mark.asyncio
async def test_explicit_user_signal_bypasses_automatic_gates(tmp_path: Path) -> None:
    def respond(request: ModelRequest) -> dict:
        source = json.loads(request.messages[-1].content or "{}")
        return {
            "episodes": [
                {
                    "episode_id": source["episodes"][0]["episode_id"],
                    "title": "保存进度",
                    "objective": "持久化当前进展",
                    "summary": "用户显式要求保存进度。",
                    "keywords": ["save"],
                    "topics": ["progress"],
                    "depth": "deep",
                }
            ],
            "candidates": [],
        }

    provider = ConsolidationProvider(respond)
    consolidator, store, _ = make_consolidator(tmp_path, provider)
    session_id = store.create_session(tmp_path)
    append_verified_run(store, session_id, "run-1")

    result = await consolidator.after_run(
        session_id=session_id,
        run_id="run-1",
        user_signal="请保存进度",
    )

    assert result.consolidated is True
    assert result.trigger == "explicit_lock"
    store.close()


@pytest.mark.asyncio
async def test_consolidation_retracts_existing_card_with_audited_version(
    tmp_path: Path,
) -> None:
    call_count = 0

    def respond(request: ModelRequest) -> dict:
        nonlocal call_count
        call_count += 1
        source = json.loads(request.messages[-1].content or "{}")
        episode = source["episodes"][0]
        if call_count == 1:
            return {
                "episodes": [
                    {
                        "episode_id": episode["episode_id"],
                        "title": "记录偏好",
                        "summary": "用户要求默认使用 pytest。",
                        "keywords": ["pytest"],
                    }
                ],
                "candidates": [
                    {
                        "operation": "upsert",
                        "kind": "preference",
                        "scope": "workspace",
                        "memory_key": "preference.test_runner",
                        "content": "默认使用 pytest。",
                        "source_positions": [1],
                        "evidence_refs": ["message:1"],
                        "confidence": 0.95,
                    }
                ],
            }
        target = source["existing_memory_cards"][0]["id"]
        return {
            "episodes": [
                {
                    "episode_id": episode["episode_id"],
                    "title": "撤销偏好",
                    "summary": "用户撤销了默认使用 pytest 的偏好。",
                    "keywords": ["pytest", "retract"],
                }
            ],
            "candidates": [
                {
                    "operation": "retract",
                    "kind": "preference",
                    "scope": "workspace",
                    "memory_key": "preference.test_runner",
                    "content": "用户明确撤销了该偏好。",
                    "target_memory_id": target,
                    "source_positions": [5],
                    "evidence_refs": ["message:5"],
                    "confidence": 0.98,
                }
            ],
        }

    provider = ConsolidationProvider(respond)
    consolidator, store, _ = make_consolidator(tmp_path, provider)
    session_id = store.create_session(tmp_path)
    append_verified_run(store, session_id, "run-1")
    await consolidator.after_run(session_id=session_id, run_id="run-1")
    first = await consolidator.consolidate(session_id, force=True)
    card_id = store.list_active_memory_cards(
        workspace=tmp_path,
        session_id=session_id,
    )[0]["id"]

    store.start_run(session_id, "run-2")
    position = store.append_message(
        session_id,
        "run-2",
        ChatMessage(role=Role.USER, content="不再默认使用 pytest。"),
    )
    assert position == 5
    store.append_message(
        session_id,
        "run-2",
        ChatMessage(role=Role.ASSISTANT, content="已记录偏好变更。"),
    )
    store.finish_run("run-2", "completed")
    await consolidator.after_run(session_id=session_id, run_id="run-2")
    second = await consolidator.consolidate(session_id, force=True)

    assert first.cards_created == 1
    assert second.cards_updated == 1
    assert (
        store.list_active_memory_cards(
            workspace=tmp_path,
            session_id=session_id,
        )
        == []
    )
    card = store.get_memory_card(card_id)
    assert card is not None
    assert card["status"] == "retracted"
    assert card["version"] == 2
    assert [item["operation"] for item in store.list_memory_card_versions(card_id)] == [
        "upsert",
        "retract",
    ]
    store.close()


@pytest.mark.asyncio
async def test_failed_consolidation_keeps_episode_pending_for_retry(tmp_path: Path) -> None:
    provider = ConsolidationProvider(lambda request: "not-json")
    consolidator, store, events = make_consolidator(tmp_path, provider)
    session_id = store.create_session(tmp_path)
    append_verified_run(store, session_id, "run-1")
    await consolidator.after_run(session_id=session_id, run_id="run-1")

    result = await consolidator.consolidate(session_id, force=True)

    assert result.consolidated is False
    assert result.reason == "consolidation_failed"
    assert store.list_memory_episodes(session_id)[0]["status"] == "pending"
    assert store.latest_memory_consolidation(session_id)["status"] == "failed"
    assert EventType.MEMORY_CONSOLIDATION_FAILED in {event.type for event in events.events}
    store.close()


@pytest.mark.asyncio
async def test_stable_memory_key_updates_card_and_preserves_qualified_provenance(
    tmp_path: Path,
) -> None:
    call_count = 0

    def respond(request: ModelRequest) -> dict:
        nonlocal call_count
        call_count += 1
        source = json.loads(request.messages[-1].content or "{}")
        episode = source["episodes"][0]
        position = 1 if call_count == 1 else 5
        content = "默认使用 pytest。" if call_count == 1 else "默认使用 unittest。"
        return {
            "episodes": [
                {
                    "episode_id": episode["episode_id"],
                    "title": "测试框架偏好",
                    "objective": "记录用户偏好",
                    "summary": content,
                    "keywords": ["test"],
                    "topics": ["preference"],
                    "depth": "deep",
                }
            ],
            "candidates": [
                {
                    "operation": "upsert",
                    "kind": "preference",
                    "scope": "workspace",
                    "memory_key": "preference.test_runner",
                    "content": content,
                    "source_positions": [position],
                    "evidence_refs": [f"message:{position}"],
                    "confidence": 0.98,
                }
            ],
        }

    provider = ConsolidationProvider(respond)
    consolidator, store, _ = make_consolidator(tmp_path, provider)
    session_id = store.create_session(tmp_path)
    append_verified_run(store, session_id, "run-1")
    await consolidator.after_run(session_id=session_id, run_id="run-1")
    first = await consolidator.consolidate(session_id, force=True)

    store.start_run(session_id, "run-2")
    store.append_message(
        session_id,
        "run-2",
        ChatMessage(role=Role.USER, content="改为默认使用 unittest。"),
    )
    store.append_message(
        session_id,
        "run-2",
        ChatMessage(role=Role.ASSISTANT, content="已更新。"),
    )
    store.finish_run("run-2", "completed")
    await consolidator.after_run(session_id=session_id, run_id="run-2")
    second = await consolidator.consolidate(session_id, force=True)

    cards = store.list_active_memory_cards(workspace=tmp_path, session_id=session_id)
    assert first.cards_created == 1
    assert second.cards_created == 0
    assert second.cards_updated == 1
    assert len(cards) == 1
    assert cards[0]["memory_key"] == "preference.test_runner"
    assert cards[0]["content"] == "默认使用 unittest。"
    assert cards[0]["version"] == 2
    assert len(cards[0]["source_refs"]) == 2
    assert all(reference.startswith("episode:") for reference in cards[0]["source_refs"])

    projection = consolidator.retrieve(
        session_id=session_id,
        run_id="retrieval",
        query="接下来使用哪个测试框架",
    )
    refreshed = store.get_memory_card(cards[0]["id"])
    assert projection["cards"][0]["id"] == cards[0]["id"]
    assert refreshed is not None
    assert refreshed["access_count"] == 1
    assert store.list_memory_retrievals(session_id)[0]["hit_count"] >= 1

    episode_id = cards[0]["source_refs"][0].split(":")[1]
    source = store.read_memory_episode_source(
        requesting_session_id=session_id,
        episode_id=episode_id,
    )
    unrelated = store.create_session(tmp_path / "other")
    assert source is not None
    assert source["messages"]
    assert (
        store.read_memory_episode_source(
            requesting_session_id=unrelated,
            episode_id=episode_id,
        )
        is None
    )

    assert store.retract_memory_card(
        memory_id=cards[0]["id"],
        workspace=tmp_path,
        session_id=session_id,
        reason="user requested",
    )
    assert store.get_memory_card(cards[0]["id"])["status"] == "retracted"
    assert store.list_memory_card_versions(cards[0]["id"])[-1]["operation"] == "user_retract"
    store.close()


@pytest.mark.asyncio
async def test_time_gate_triggers_without_a_previous_successful_consolidation(
    tmp_path: Path,
) -> None:
    def respond(request: ModelRequest) -> dict:
        source = json.loads(request.messages[-1].content or "{}")
        return {
            "episodes": [
                {
                    "episode_id": episode["episode_id"],
                    "title": "陈旧 Episode",
                    "objective": "",
                    "summary": "等待时间门触发。",
                    "keywords": ["time"],
                    "topics": ["gate"],
                    "depth": "shallow",
                }
                for episode in source["episodes"]
            ],
            "candidates": [],
        }

    provider = ConsolidationProvider(respond)
    consolidator, store, _ = make_consolidator(
        tmp_path,
        provider,
        auto_consolidate=True,
        session_gate=100,
        time_gate_hours=1,
    )
    session_id = store.create_session(tmp_path)
    append_verified_run(store, session_id, "run-1")
    store.seal_memory_episodes(session_id)
    with store._lock, store._connection:  # noqa: SLF001
        store._connection.execute(  # noqa: SLF001
            "UPDATE memory_episodes SET created_at = '2020-01-01T00:00:00+00:00'"
        )

    result = await consolidator.consolidate(session_id, trigger="auto", force=False)

    assert result.consolidated is True
    assert result.trigger == "auto"
    store.close()


def test_memory_retrieval_prioritizes_constraints_and_query_overlap() -> None:
    cards = [
        {
            "id": "constraint",
            "kind": "constraint",
            "scope": "workspace",
            "confidence": 0.9,
            "content": "修改后必须运行 pytest。",
            "updated_at": "2026-01-01",
        },
        {
            "id": "lesson",
            "kind": "lesson",
            "scope": "workspace",
            "confidence": 0.9,
            "content": "数据库迁移需要备份。",
            "updated_at": "2026-01-02",
        },
    ]

    ranked = rank_memory_cards(cards, "请修改代码并运行 pytest", limit=1)

    assert ranked[0]["id"] == "constraint"
