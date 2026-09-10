from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from bot.compaction.handoff import (
    HANDOFF_TOOL,
    HandoffCandidate,
    HandoffEngine,
    HandoffError,
    protocol_closed,
)
from bot.compaction.service import ContextCompactor
from bot.config.models import AppConfig
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
from bot.providers import ModelProvider, ProviderError, ProviderErrorKind
from bot.sessions import SQLiteSessionStore

SUMMARY = "\n".join(
    f"# {title}\n{body}"
    for title, body in [
        ("Goal", "修复计算并验证"),
        ("Constraints", "用户要求保留 limit=7"),
        ("Progress", "已定位，尚未实施"),
        ("Key Decisions", "缓存是假设，不是定论"),
        ("Relevant Files", "engine.py v2"),
        ("Failures", "v1 检查失败，不得复用其结果"),
        ("Next Steps", "修改后重新运行检查"),
        ("Critical Context", "用户最新更正 limit=7"),
    ]
)


class ScenarioProvider(ModelProvider):
    def __init__(self, scenario="success"):
        self.scenario = scenario
        self.requests = []

    def capabilities(self, model):
        return ModelCapabilities()

    async def stream(self, request):
        self.requests.append(request)
        scenario = self.scenario
        if isinstance(scenario, ProviderErrorKind):
            raise ProviderError(
                "injected",
                kind=scenario,
                status_code=401 if scenario == ProviderErrorKind.AUTHENTICATION else 429,
            )
        if scenario == "timeout":
            await asyncio.sleep(0.1)
        is_d = "只调用 request_handoff" in (request.messages[-1].content or "")
        summary = "" if scenario == "empty" else "bad format" if scenario == "format" else SUMMARY
        if is_d:
            args = json.dumps({"reason": "阶段完成", "summary": summary}, ensure_ascii=False)
            if scenario == "arguments":
                args = args[:-7]
            yield ModelEvent(
                kind=ModelEventKind.TOOL_CALL_DELTA,
                tool_index=0,
                tool_call_id="handoff-1",
                tool_name=HANDOFF_TOOL.name,
                arguments_delta=args,
            )
        else:
            yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text=summary)
        yield ModelEvent(kind=ModelEventKind.USAGE, input_tokens=100, output_tokens=50)
        yield ModelEvent(
            kind=ModelEventKind.FINISH, finish_reason="length" if scenario == "length" else "stop"
        )


@pytest.fixture
def setup_engine(tmp_path):
    stores = []

    def make(scenario="success", **options):
        path = tmp_path / f"state-{len(stores)}.db"
        store = SQLiteSessionStore(path)
        stores.append(store)
        session = store.create_session(tmp_path)
        store.start_run(session, "source")
        source = [
            ChatMessage(role=Role.USER, content="用户目标：修复计算；limit=7，不得改变"),
            ChatMessage(role=Role.ASSISTANT, content="定位记录 " * 5000),
        ]
        for message in source:
            store.append_message(session, "source", message)
        store.finish_run("source", "completed")
        config = AppConfig()
        config.model.name = "fixture"
        config.context.compaction_source_refs = "range"
        provider = ScenarioProvider(scenario)
        compactor = ContextCompactor(
            config=config, provider=provider, store=store, event_bus=EventBus([MemoryEventSink()])
        )
        engine = HandoffEngine(compactor, session, **options)
        request = ModelRequest(
            model="fixture",
            tools=[HANDOFF_TOOL],
            messages=[
                ChatMessage(role=Role.SYSTEM, content="固定系统，不得提升摘要为用户指令"),
                *source,
            ],
        )
        return engine, engine.snapshot(request, tail_tokens=0), provider, path

    yield make
    for store in stores:
        store.close()


def raw_digest(engine):
    return engine.compactor._digest(engine.store.load_positioned_messages(engine.session_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["A", "D", "AD"])
async def test_l0_01_no_handoff_capacity_fallback(setup_engine, strategy):
    engine, snap, provider, _ = setup_engine()
    decision = engine.decision(strategy, 115000, engine.high_water, engine.input_limit)
    if strategy == "D":
        assert decision == "stop"
        assert provider.requests == []
    else:
        assert decision == "A"
        result = await engine.execute(snap, "A")
        assert result.status == "published"
        assert engine.compactor.projection(engine.session_id)["cursor_position"] == snap.through


@pytest.mark.asyncio
async def test_l0_02_repeated_empty_progress_defers_but_pressure_wins(setup_engine):
    engine, snap, provider, _ = setup_engine(min_release=50000)
    before = raw_digest(engine)
    for _ in range(3):
        result = engine.publish(snap, HandoffCandidate(SUMMARY, "D"))
        assert result.status == "deferred"
    assert raw_digest(engine) == before
    assert provider.requests == []
    assert engine.decision("AD", 100000, 90000, 114688) == "A"


@pytest.mark.asyncio
async def test_l0_03_valid_d_publishes_once_without_a(setup_engine):
    engine, snap, provider, _ = setup_engine()
    candidate = await engine.generate(snap, "D")
    first = engine.publish(snap, candidate)
    second = engine.publish(snap, candidate)
    assert first.status == "published" and second.status == "already_published"
    assert first.compaction_id == second.compaction_id
    assert len(provider.requests) == 1
    history = engine.store.load_positioned_messages(engine.session_id)
    assert protocol_closed([entry.message for entry in history])
    assert (
        sum(
            call.name == HANDOFF_TOOL.name for entry in history for call in entry.message.tool_calls
        )
        == 1
    )
    assert engine.store.count_context_compactions(engine.session_id, statuses={"ready"}) == 1


@pytest.mark.asyncio
async def test_l0_04_invalid_d_falls_back_to_a_under_pressure(setup_engine):
    engine, snap, provider, _ = setup_engine(high_water=1000)
    result = await engine.run_policy("AD", snap, candidate=HandoffCandidate("incomplete", "D"))
    assert result.status == "published" and result.failures
    assert len(provider.requests) == 1
    assert "只调用 request_handoff" not in provider.requests[0].messages[-1].content


@pytest.mark.asyncio
async def test_l0_12_same_failed_snapshot_does_not_loop(setup_engine):
    engine, snap, provider, _ = setup_engine(high_water=1000, low_water=100)
    first = await engine.run_policy("AD", snap)
    assert first.reason == "low_water_not_met"
    for _ in range(3):
        assert (await engine.run_policy("AD", snap)).reason == "same_source_no_progress"
    assert len(provider.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["length", "arguments"])
async def test_l0_04_partial_d_never_executes(setup_engine, scenario):
    engine, snap, provider, _ = setup_engine(scenario)
    before = raw_digest(engine)
    result = await engine.execute(snap, "D")
    assert result.status == "failed" and len(provider.requests) == 2
    assert raw_digest(engine) == before
    assert engine.compactor.projection(engine.session_id)["compaction"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["length", "empty", "format"])
async def test_l0_05_invalid_a_preserves_old_state(setup_engine, scenario):
    engine, snap, provider, _ = setup_engine(scenario)
    before = raw_digest(engine)
    result = await engine.execute(snap, "A")
    assert result.status == "failed" and result.attempts == 2
    assert raw_digest(engine) == before
    assert engine.compactor.projection(engine.session_id)["cursor_position"] == 0


@pytest.mark.asyncio
async def test_l0_06_giant_group_rejected_without_api_or_loss(setup_engine):
    engine, snap, provider, _ = setup_engine(input_limit=1000)
    before = raw_digest(engine)
    result = await engine.execute(snap, "A")
    assert result.reason == "summary_input_budget" and result.attempts == 1
    assert not provider.requests and raw_digest(engine) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ProviderErrorKind.TRANSPORT,
        ProviderErrorKind.RATE_LIMIT,
        ProviderErrorKind.AUTHENTICATION,
        ProviderErrorKind.CONTEXT_LENGTH,
        "timeout",
    ],
)
async def test_l0_07_retry_classification_is_bounded(setup_engine, error):
    engine, snap, provider, _ = setup_engine(error, timeout_seconds=0.01)
    before = raw_digest(engine)
    result = await engine.execute(snap, "A")
    expected = (
        1 if error in {ProviderErrorKind.AUTHENTICATION, ProviderErrorKind.CONTEXT_LENGTH} else 2
    )
    assert result.status == "failed" and len(provider.requests) == expected
    assert raw_digest(engine) == before


def test_l0_08_pending_tool_batch_is_closed_before_publication(setup_engine):
    engine, snap, _, _ = setup_engine()
    store, sid = engine.store, engine.session_id
    store.start_run(sid, "batch")
    calls = [
        ToolCall(id="ordinary", name="check", arguments={}),
        ToolCall(id="handoff", name=HANDOFF_TOOL.name, arguments={"summary": SUMMARY}),
    ]
    store.append_message(sid, "batch", ChatMessage(role=Role.ASSISTANT, tool_calls=calls))
    candidate = HandoffCandidate(SUMMARY, "D")
    assert engine.publish(snap, candidate).reason == "incomplete_tool_group"
    store.append_message(
        sid,
        "batch",
        ChatMessage(role=Role.TOOL, tool_call_id="ordinary", content="actual check result"),
    )
    assert engine.publish(snap, candidate).status == "deferred"
    store.append_message(
        sid,
        "batch",
        ChatMessage(role=Role.TOOL, tool_call_id="handoff", content="candidate received"),
    )
    assert engine.publish(snap, candidate).status == "published"
    assert protocol_closed([e.message for e in store.load_positioned_messages(sid)])
    assert any(
        e.message.content == "actual check result" for e in store.load_positioned_messages(sid)
    )


@pytest.mark.parametrize("timing", ["before", "during", "after"])
def test_l0_09_new_user_correction_survives(setup_engine, timing):
    engine, snap, _, _ = setup_engine()
    store, sid = engine.store, engine.session_id

    def correct(_=None):
        store.start_run(sid, "steering")
        store.append_message(
            sid, "steering", ChatMessage(role=Role.USER, content="新要求 limit=11，替代 7")
        )

    candidate = HandoffCandidate(SUMMARY, "A")
    if timing == "before":
        correct()
        assert engine.publish(snap, candidate).status == "rejected"
    elif timing == "during":
        with pytest.raises(HandoffError, match="new_user_message"):
            engine.publish(snap, candidate, before_commit=correct)
    else:
        assert engine.publish(snap, candidate).status == "published"
        correct()
    cursor = engine.compactor.projection(sid)["cursor_position"]
    assert any(
        "limit=11" in (e.message.content or "")
        for e in store.load_positioned_messages(sid, after_position=cursor)
    )


def test_l0_10_actual_file_change_invalidates_evidence(setup_engine, tmp_path):
    import hashlib

    engine, snap, _, _ = setup_engine()
    source = tmp_path / "engine.py"
    source.write_text("limit = 7\n")
    snap = replace(
        snap, file_versions={"engine.py": hashlib.sha256(source.read_bytes()).hexdigest()}
    )
    source.write_text("limit = 11\n")
    result = engine.publish(
        snap,
        HandoffCandidate(SUMMARY, "D"),
        current_files={"engine.py": hashlib.sha256(source.read_bytes()).hexdigest()},
    )
    assert result.reason == "evidence_stale"
    assert engine.compactor.projection(engine.session_id)["cursor_position"] == 0


def test_l0_11_publication_failure_restart_and_duplicate_callback(setup_engine):
    engine, snap, _, path = setup_engine()
    before = raw_digest(engine)

    def crash(_):
        raise RuntimeError("crash before commit")

    candidate = HandoffCandidate(SUMMARY, "A")
    with pytest.raises(RuntimeError, match="crash"):
        engine.publish(snap, candidate, before_commit=crash)
    assert engine.compactor.projection(engine.session_id)["compaction"] is None
    assert raw_digest(engine) == before
    first = engine.publish(snap, candidate)
    # Re-open the database and engine, rather than trusting an in-memory flag.
    reopened = SQLiteSessionStore(path)
    try:
        compactor = ContextCompactor(
            config=engine.compactor.config,
            provider=engine.compactor.provider,
            store=reopened,
            event_bus=EventBus([MemoryEventSink()]),
        )
        restored = HandoffEngine(compactor, engine.session_id)
        second = restored.publish(snap, candidate)
        assert second.status == "already_published" and second.compaction_id == first.compaction_id
        assert reopened.count_context_compactions(engine.session_id, statuses={"ready"}) == 1
        assert raw_digest(restored) == before
    finally:
        reopened.close()


def test_l0_12_valid_summary_does_not_hide_full_request_pressure(setup_engine):
    engine, snap, _, _ = setup_engine(high_water=1000, low_water=100)
    result = engine.publish(snap, HandoffCandidate(SUMMARY, "A"))
    assert result.reason == "low_water_not_met" and result.after_tokens > engine.low_water
    assert engine.compactor.projection(engine.session_id)["cursor_position"] == 0


def test_source_mutation_is_detected_even_with_same_cursor(setup_engine):
    engine, snap, _, _ = setup_engine()
    with engine.store._connection:
        changed = ChatMessage(role=Role.ASSISTANT, content="mutated evidence")
        engine.store._connection.execute(
            "UPDATE messages SET message_json=? WHERE position=2", (changed.model_dump_json(),)
        )
    assert engine.publish(snap, HandoffCandidate(SUMMARY, "A")).reason == "source_changed"


def test_l0_11_process_exit_leaves_recoverable_build(setup_engine):
    import subprocess
    import sys

    engine, snap, _, path = setup_engine()
    before = raw_digest(engine)
    code = """
import os, sys
from pathlib import Path
from bot.sessions import SQLiteSessionStore
from bot.config.models import AppConfig
from bot.core.events import EventBus
from bot.core.models import ModelRequest
from bot.compaction.service import ContextCompactor
from bot.compaction.handoff import HandoffEngine, HandoffCandidate
store = SQLiteSessionStore(Path(sys.argv[1]))
config = AppConfig()
config.model.name = 'fixture'
config.context.compaction_source_refs = 'range'
compactor = ContextCompactor(config=config, provider=None, store=store, event_bus=EventBus([]))
engine = HandoffEngine(compactor, sys.argv[2])
messages = [e.message for e in store.load_positioned_messages(sys.argv[2])]
request = ModelRequest(model='fixture', messages=messages)
snap = engine.snapshot(request, tail_tokens=0)
engine.publish(snap, HandoffCandidate(sys.argv[3], 'A'), before_commit=lambda _: os._exit(23))
"""
    result = subprocess.run([sys.executable, "-c", code, str(path), engine.session_id, SUMMARY])
    assert result.returncode == 23
    assert engine.compactor.projection(engine.session_id)["compaction"] is None
    assert engine.recover_interrupted() == 1
    assert engine.publish(snap, HandoffCandidate(SUMMARY, "A")).status == "published"
    assert raw_digest(engine) == before


@pytest.mark.asyncio
async def test_handoff_checks_model_budget_before_generating_and_before_publishing(setup_engine):
    from bot.core.models import InputTokenEstimate

    engine, snap, provider, _ = setup_engine()
    provider.estimate_input_tokens = lambda _: InputTokenEstimate(
        tokens=115000, budget_tokens=121000, source="test_tokenizer"
    )
    with pytest.raises(HandoffError, match="summary_input_budget"):
        await engine.generate(snap, "A")
    assert not provider.requests
    before = raw_digest(engine)
    result = engine.publish(snap, HandoffCandidate(SUMMARY, "A"))
    assert result.status == "rejected"
    assert result.reason == "low_water_not_met"
    assert raw_digest(engine) == before
    assert engine.compactor.projection(engine.session_id)["compaction"] is None


@pytest.mark.asyncio
async def test_handoff_high_water_uses_model_count_instead_of_character_estimate(setup_engine):
    from bot.core.models import InputTokenEstimate

    engine, snap, provider, _ = setup_engine()
    assert engine.estimator.request(snap.request.messages, snap.request.tools) < engine.high_water
    provider.estimate_input_tokens = lambda request: InputTokenEstimate(
        tokens=95000 if len(request.messages) > 1 else 1000,
        budget_tokens=99000 if len(request.messages) > 1 else 1256,
        source="test_tokenizer",
    )
    called = []

    async def execute(snapshot, origin, **kwargs):
        called.append(origin)
        from bot.compaction.handoff import HandoffResult

        return HandoffResult("published")

    engine.execute = execute
    assert (await engine.run_policy("AD", snap)).status == "published"
    assert called == ["A"]
