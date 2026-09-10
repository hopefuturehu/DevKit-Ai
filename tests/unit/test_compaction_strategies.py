import asyncio
import json

import pytest

from bot.compaction.service import ContextCompactor
from bot.compaction.strategies import StrategyCompactor, StrategyFrame
from bot.config.models import AppConfig
from bot.core.events import EventBus, EventType, MemoryEventSink
from bot.core.models import (
    ChatMessage,
    ModelCapabilities,
    ModelEvent,
    ModelEventKind,
    ModelRequest,
    Role,
)
from bot.providers import ModelProvider
from bot.sessions import SQLiteSessionStore

SUMMARY = "\n\n".join(
    f"# {name}\n- Preserve the original constraints and continue testing."
    for name in [
        "Goal",
        "Constraints",
        "Progress",
        "Key Decisions",
        "Relevant Files",
        "Failures",
        "Next Steps",
        "Critical Context",
    ]
)


class SummaryProvider(ModelProvider):
    def __init__(self):
        self.requests = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.finish_reason = "stop"

    def capabilities(self, model):
        return ModelCapabilities()

    async def stream(self, request):
        self.requests.append(request.model_copy(deep=True))
        self.started.set()
        await self.release.wait()
        yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text=SUMMARY)
        yield ModelEvent(
            kind=ModelEventKind.USAGE,
            input_tokens=100,
            output_tokens=20,
            provider_metadata={
                "raw_usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 100,
                }
            },
        )
        yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason=self.finish_reason)


def engine(tmp_path, strategy="b", *, after_size=0):
    config = AppConfig.model_validate(
        {
            "model": {"name": "mock"},
            "context": {
                "compaction_strategy": strategy,
                "compaction_leaf_input_tokens": 2048,
                "compaction_low_water_tokens": 2000,
                "compaction_merge_fanout": 2,
            },
        }
    )
    store = SQLiteSessionStore(tmp_path / "state.db")
    session = store.create_session(tmp_path)
    store.start_run(session, "run")
    for index in range(12):
        store.append_message(
            session,
            "run",
            ChatMessage(
                role=Role.USER if index == 0 else Role.ASSISTANT,
                content=f"source {index}\n" + "observed record\n" * 220,
            ),
        )
    provider = SummaryProvider()
    sink = MemoryEventSink()
    compactor = ContextCompactor(
        config=config, provider=provider, store=store, event_bus=EventBus([store, sink])
    )
    instance = StrategyCompactor(compactor, provider, session, "run")
    request = ModelRequest(
        model="mock",
        messages=[ChatMessage(role=Role.SYSTEM, content="policy")]
        + [e.message for e in store.load_positioned_messages(session)],
    )
    instance.frame = StrategyFrame(
        request,
        lambda record: ModelRequest(
            model="mock",
            messages=[
                ChatMessage(role=Role.SYSTEM, content="policy"),
                ChatMessage(role=Role.ASSISTANT, content=record["summary_text"] + "X" * after_size),
            ],
        ),
    )
    return instance, provider, store, sink


@pytest.mark.asyncio
async def test_b_background_tree_covers_every_range_and_publishes_only_once(tmp_path):
    b, provider, store, sink = engine(tmp_path)
    try:
        before = b.compactor._digest(store.load_positioned_messages(b.session_id))
        provider.release.clear()
        b.schedule(12)
        await asyncio.wait_for(provider.started.wait(), 1)
        assert b.background is not None and not b.background.done()
        assert b.compactor.projection(b.session_id)["cursor_position"] == 0
        provider.release.set()
        await b.background
        prepared_count = len(provider.requests)
        # Accounting completed background work cannot charge it twice on publication.
        paid_input, paid_output = b.take_usage()
        assert paid_input > 0
        result = await b.compact(12, [1])
        assert result.compacted, result.error
        assert result.input_tokens + paid_input == len(provider.requests) * 100
        assert result.output_tokens + paid_output == len(provider.requests) * 20
        leaves = [
            r for r in provider.requests if json.loads(r.messages[-1].content)["kind"] == "leaf"
        ]
        positions = [
            m["position"] for r in leaves for m in json.loads(r.messages[-1].content)["messages"]
        ]
        assert positions == list(range(1, 13))
        assert prepared_count > 0
        assert all(b.count(r) <= b.config.context.compaction_leaf_input_tokens for r in leaves)
        merges = [
            e
            for e in sink.events
            if e.type == EventType.CONTEXT_COMPACTION_NODE_READY and e.payload["children"]
        ]
        assert len(merges) > 1  # Exercise multiple tree levels.
        assert len(store.list_context_compactions(b.session_id)) == 1
        assert b.compactor.projection(b.session_id)["cursor_position"] == 12
        assert b.compactor._digest(store.load_positioned_messages(b.session_id)) == before
        assert b.take_usage() == (0, 0)
    finally:
        await b.close()
        store.close()


@pytest.mark.asyncio
async def test_a_preserves_input_prefix_and_checks_post_skill_recovery_budget(tmp_path):
    a, provider, store, _ = engine(tmp_path, "a", after_size=12000)
    try:
        original = a.frame.request.model_copy(deep=True)
        result = await a.compact(12, [1])
        assert not result.compacted
        assert "low_water_not_met" in result.error
        assert provider.requests[0].messages[:-1] == original.messages
        assert provider.requests[0].tools == original.tools
        assert a.frame.request == original
        assert a.compactor.projection(a.session_id)["cursor_position"] == 0
        assert not store.list_context_compactions(a.session_id)
    finally:
        await a.close()
        store.close()


@pytest.mark.asyncio
async def test_truncated_candidates_keep_old_history_and_charge_both_attempts(tmp_path):
    a, provider, store, _ = engine(tmp_path, "a")
    try:
        provider.finish_reason = "length"
        result = await a.compact(12, [1])
        assert not result.compacted
        assert len(provider.requests) == 2
        assert result.input_tokens == 200 and result.output_tokens == 40
        assert a.compactor.projection(a.session_id)["cursor_position"] == 0
        repeated = await a.compact(12, [1])
        assert repeated.reason == "failure_backoff" and len(provider.requests) == 2
    finally:
        await a.close()
        store.close()


@pytest.mark.asyncio
async def test_background_cancellation_is_joined_and_missing_usage_is_explicit(tmp_path):
    b, provider, store, sink = engine(tmp_path)
    try:
        provider.release.clear()
        b.schedule(12)
        await asyncio.wait_for(provider.started.wait(), 1)
        task = b.background
        await b.close()
        assert task.done() and b.background is None
        failures = [e for e in sink.events if e.type == EventType.CONTEXT_COMPACTION_REQUEST_FAILED]
        assert len(failures) == 1 and failures[0].payload["usage_missing"]
        assert b.take_usage() == (0, 0)
        assert not store.list_context_compactions(b.session_id)
    finally:
        await b.close()
        store.close()


@pytest.mark.asyncio
async def test_b_rejects_oversized_atomic_group_without_skipping_source(tmp_path):
    b, provider, store, _ = engine(tmp_path)
    try:
        store.append_message(
            b.session_id, "run", ChatMessage(role=Role.ASSISTANT, content="X" * 50000)
        )
        result = await b.compact(13, [1])
        assert not result.compacted and "atomic_group_exceeds_leaf_budget" in result.error
        assert not provider.requests
        assert len(store.load_positioned_messages(b.session_id)) == 13
    finally:
        await b.close()
        store.close()


@pytest.mark.asyncio
async def test_b_reuses_overlapping_cache_and_publishes_successive_roots(tmp_path):
    b, provider, store, _ = engine(tmp_path)
    try:
        await b.prepare_leaves(12, flush=True)
        # A smaller forced boundary may need different leaf ranges.
        await b.prepare_leaves(3, flush=True)
        result = await b.compact(6, [1])
        assert result.compacted, result.error
        first = result.compaction_id
        result = await b.compact(12, [1])
        assert result.compacted, result.error
        assert result.parent_id == first
        assert b.compactor.projection(b.session_id)["cursor_position"] == 12
        assert len(store.list_context_compactions(b.session_id)) == 2
        final_request = json.loads(provider.requests[-1].messages[-1].content)
        assert final_request["nodes"][0]["start"] == 1
        assert final_request["nodes"][-1]["end"] == 12
    finally:
        await b.close()
        store.close()
