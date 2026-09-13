import asyncio
import json

import pytest

from bot.compaction.service import ContextCompactor
from bot.compaction.strategies import (
    FALLBACK_SUMMARY_INSTRUCTION,
    StrategyCompactor,
    StrategyFrame,
)
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
from bot.providers import ModelProvider, ProviderError, ProviderErrorKind
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
        self.summary = SUMMARY
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
        yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text=self.summary)
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
                "compaction_max_output_tokens": 512,
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
    if strategy == "a_fallback":
        instance.frame.request.max_output_tokens = 32768
        instance.frame.evidence = store.load_positioned_messages(session)
        instance.frame.prefix_request = request.model_copy(deep=True)
        instance.frame.prefix_request.max_output_tokens = 32768
        instance.frame.prefix_request.tool_choice = "auto"
        instance.frame.prefix_positions = (None, *range(1, 13))
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
async def test_a_preserves_input_prefix_when_publishing(tmp_path):
    a, provider, store, _ = engine(tmp_path, "a")
    try:
        original = a.frame.request.model_copy(deep=True)
        result = await a.compact(12, [1])
        assert result.compacted, result.error
        assert provider.requests[0].messages[:-1] == original.messages
        assert FALLBACK_SUMMARY_INSTRUCTION not in provider.requests[0].messages[-1].content
        assert provider.requests[0].tools == original.tools
        assert a.frame.request == original
        assert a.compactor.projection(a.session_id)["cursor_position"] == 12
    finally:
        await a.close()
        store.close()


@pytest.mark.asyncio
async def test_a_rejects_unfittable_recovery_before_paying_for_summary(tmp_path):
    a, provider, store, _ = engine(tmp_path, "a", after_size=12000)
    try:
        result = await a.compact(6, [1])
        assert not result.compacted and "fixed_context_exceeds_low_water" in result.error
        assert not provider.requests
        assert a.compactor.projection(a.session_id)["cursor_position"] == 0
    finally:
        await a.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["a", "b"])
async def test_strategy_extends_coverage_to_fit_complete_tail(tmp_path, strategy):
    instance, provider, store, _ = engine(tmp_path, strategy)
    entries = store.load_positioned_messages(instance.session_id)
    instance.frame.project = lambda record: ModelRequest(
        model="mock",
        messages=[ChatMessage(role=Role.SYSTEM, content="policy")]
        + [ChatMessage(role=Role.ASSISTANT, content=record["summary_text"])]
        + [e.message for e in entries if e.position > record["covered_end_position"]],
    )
    try:
        result = await instance.compact(3, [1])
        assert result.compacted, result.error
        assert 3 < result.covered_end_position < 12
        assert result.requested_end_position == 3
        assert instance.last_metrics["after_tokens"] <= 2000
        if strategy == "b":
            sources = [
                m["position"]
                for r in provider.requests
                if json.loads(r.messages[-1].content)["kind"] == "leaf"
                for m in json.loads(r.messages[-1].content)["messages"]
            ]
            assert sources == list(range(1, result.covered_end_position + 1))
    finally:
        await instance.close()
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


class FallbackProvider(SummaryProvider):
    def __init__(self, outcomes):
        super().__init__()
        self.outcomes = iter(outcomes)

    async def stream(self, request):
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            self.requests.append(request.model_copy(deep=True))
            raise outcome
        async for event in super().stream(request):
            if event.kind == ModelEventKind.TEXT_DELTA:
                if outcome == "tool":
                    yield ModelEvent(
                        kind=ModelEventKind.TOOL_CALL_DELTA,
                        tool_index=0,
                        tool_call_id="must-not-execute",
                        tool_name="run_shell",
                        arguments_delta='{"command":"echo forbidden"}',
                    )
                if outcome == "format":
                    event.text = "# Goal\nnot enough"
                if outcome == "empty":
                    continue
            if event.kind == ModelEventKind.FINISH:
                if outcome == "incomplete":
                    continue
                event.finish_reason = {"length": "length", "tool": "tool_calls"}.get(
                    outcome, "stop"
                )
            yield event


def fallback_engine(tmp_path, outcomes):
    instance, _, store, sink = engine(tmp_path, "a_fallback")
    provider = FallbackProvider(outcomes)
    instance.provider = provider
    return instance, provider, store, sink


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcomes", "adopted_path"),
    [(["ok"], "prefix"), (["tool", "ok"], "isolated")],
)
async def test_long_complete_summary_publishes_without_body_limit(tmp_path, outcomes, adopted_path):
    instance, provider, store, _ = fallback_engine(tmp_path, outcomes)
    provider.summary = (SUMMARY + "\n- " + "verified evidence " * 1400).strip()
    instance.config.context.compaction_low_water_tokens = 12000
    try:
        assert instance.compactor._estimator.text(provider.summary) > 4000
        before = instance.compactor._digest(store.load_positioned_messages(instance.session_id))
        result = await instance.compact(12, [1])
        assert result.compacted, result.error
        assert result.request_count == len(outcomes)
        assert instance.last_metrics["adopted_path"] == adopted_path
        active = instance.compactor.projection(instance.session_id)["compaction"]
        assert active["summary_text"] == provider.summary
        assert active["summary_token_estimate"] > 4000
        assert (
            instance.compactor._digest(store.load_positioned_messages(instance.session_id))
            == before
        )
    finally:
        await instance.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["length", "full_request_budget"])
async def test_long_summary_still_requires_complete_output_and_fitting_request(tmp_path, failure):
    outcomes = ["length", "length"] if failure == "length" else ["ok", "ok"]
    instance, provider, store, _ = fallback_engine(tmp_path, outcomes)
    provider.summary = (SUMMARY + "\n- " + "verified evidence " * 1400).strip()
    if failure == "length":
        instance.config.context.compaction_low_water_tokens = 12000
    try:
        assert instance.compactor._estimator.text(provider.summary) > 4000
        result = await instance.compact(12, [1])
        assert not result.compacted
        assert result.request_count == 2
        expected = "达到输出长度限制" if failure == "length" else "low_water_not_met"
        assert expected in result.error
        assert instance.compactor.projection(instance.session_id)["cursor_position"] == 0
        assert not store.list_context_compactions(instance.session_id)
    finally:
        await instance.close()
        store.close()


@pytest.mark.asyncio
async def test_prefix_success_preserves_32k_and_uses_only_one_request(tmp_path):
    instance, provider, store, _ = fallback_engine(tmp_path, ["ok"])
    try:
        original = instance.frame.prefix_request.model_copy(deep=True)
        result = await instance.compact(12, [1])
        assert result.compacted, result.error
        assert len(provider.requests) == 1
        sent = provider.requests[0]
        assert sent.messages[:-1] == original.messages
        assert sent.model_dump(exclude={"messages"}) == original.model_dump(exclude={"messages"})
        assert sent.max_output_tokens == 32768 and sent.tool_choice == "auto"
        assert FALLBACK_SUMMARY_INSTRUCTION in sent.messages[-1].content
        assert instance.last_metrics["adopted_path"] == "prefix"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_annotated_headings_publish_from_prefix_and_preserve_both_conflict_sections(tmp_path):
    instance, provider, store, _ = fallback_engine(tmp_path, ["ok"])
    try:
        provider.summary = (
            SUMMARY.replace("# Constraints", "# Constraints（用户硬约束，原样保留）").replace(
                "# Critical Context", "# Critical Context (verified evidence)"
            )
            + "\n\n## Critical Context (unresolved conflicts)"
            "\n- Radius interpretation is unverified."
        )
        before = instance.compactor._digest(store.load_positioned_messages(instance.session_id))
        result = await instance.compact(12, [1])
        assert result.compacted, result.error
        assert result.request_count == 1 and len(provider.requests) == 1
        assert instance.last_metrics["adopted_path"] == "prefix"
        active = instance.compactor.projection(instance.session_id)["compaction"]
        assert active["summary_text"] == provider.summary
        assert instance.compactor._digest(store.load_positioned_messages(instance.session_id)) == (
            before
        )
    finally:
        await instance.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "tool",
        "format",
        "empty",
        "length",
        "incomplete",
        TimeoutError(),
        ProviderError("disconnect", kind=ProviderErrorKind.TRANSPORT),
        ProviderError("too long", kind=ProviderErrorKind.CONTEXT_LENGTH),
    ],
)
async def test_prefix_failure_uses_one_isolated_request_and_same_evidence(tmp_path, failure):
    instance, provider, store, sink = fallback_engine(tmp_path, [failure, "ok"])
    try:
        before = instance.compactor._digest(store.load_positioned_messages(instance.session_id))
        result = await instance.compact(12, [1])
        assert result.compacted, result.error
        assert len(provider.requests) == 2 and result.request_count == 2
        isolated = provider.requests[1]
        assert not isolated.tools and isolated.tool_choice is None
        assert [m.role for m in isolated.messages] == [Role.SYSTEM, Role.USER]
        assert FALLBACK_SUMMARY_INSTRUCTION in provider.requests[0].messages[-1].content
        assert FALLBACK_SUMMARY_INSTRUCTION in isolated.messages[0].content
        payload = json.loads(isolated.messages[1].content)
        assert payload["covered_range"] == [1, 12]
        assert [e["position"] for e in payload["transcript"]] == list(range(1, 13))
        assert "echo forbidden" not in isolated.messages[1].content
        assert instance.last_metrics["adopted_path"] == "isolated"
        assert (
            instance.compactor._digest(store.load_positioned_messages(instance.session_id))
            == before
        )
        events = [e for e in sink.events if e.type == EventType.CONTEXT_COMPACTION_REQUEST_FAILED]
        assert len(events) == 1 and events[0].payload["operation_id"]
        response = store.read_context_blob(instance.session_id, events[0].payload["response_ref"])
        if failure == "tool":
            assert json.loads(response["content"])["tool_calls"]["0"]["name"] == "run_shell"
        assert not any(e.type == EventType.TOOL_REQUESTED for e in sink.events)
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("thinking", ["enabled", "disabled", None])
async def test_both_summary_paths_follow_captured_main_thinking(tmp_path, thinking):
    instance, provider, store, _ = fallback_engine(tmp_path, ["length", "ok"])
    try:
        instance.frame.request.thinking = thinking
        instance.frame.prefix_request.thinking = thinking
        # Neither a legacy override nor a later config edit may change the
        # fallback away from the main request captured at this boundary.
        instance.config.context.compaction_thinking = "disabled"
        instance.config.model.thinking = "disabled" if thinking != "disabled" else "enabled"
        result = await instance.compact(12, [1])
        assert result.compacted, result.error
        assert len(provider.requests) == 2
        assert [request.thinking for request in provider.requests] == [thinking, thinking]
        assert instance.last_metrics["adopted_path"] == "isolated"
    finally:
        await instance.close()
        store.close()


@pytest.mark.asyncio
async def test_fallback_preserves_parent_tail_and_scoped_source_recovery(tmp_path):
    """Check transport/recovery across two compactions, not model factual accuracy."""
    instance, provider, store, sink = fallback_engine(tmp_path, ["ok", "tool", "ok"])
    try:
        instance.config.context.compaction_low_water_tokens = 20_000
        entries = store.load_positioned_messages(instance.session_id)
        original = instance.frame.request.model_copy(deep=True)
        digest = instance.compactor._digest(entries)

        def project(record):
            return original.model_copy(
                update={
                    "messages": original.messages[:1]
                    + instance.compactor.context_messages(instance.session_id, record)
                    + [e.message for e in entries if e.position > record["covered_end_position"]]
                },
                deep=True,
            )

        instance.frame.project = project
        provider.summary = SUMMARY + (
            "\n- 待核实：历史助手的数值解释尚无工具结果支持。"
            "\n- 已否决：重复读取整个文件，原因是没有得到新增证据。"
        )
        first = await instance.compact(6, [1])
        assert first.compacted, first.error
        assert first.covered_end_position == 6
        parent = instance.compactor.projection(instance.session_id)["compaction"]
        resumed = project(parent)
        assert resumed.messages[1] == entries[0].message  # Original user anchor.
        assert resumed.messages[3:] == [e.message for e in entries[6:]]
        assert "续跑核验" in resumed.messages[2].content
        assert parent["summary_text"] == provider.summary
        assert instance.last_metrics["after_tokens"] == instance.count(resumed)

        instance.frame.request = resumed
        instance.frame.prefix_request = resumed.model_copy(deep=True)
        instance.frame.prefix_positions = (None, 1, None, *range(7, 13))
        provider.summary = SUMMARY
        second = await instance.compact(12, [1])
        assert second.compacted, second.error
        assert second.parent_id == parent["id"] and second.request_count == 2
        assert provider.requests[1].messages[:-1] == resumed.messages
        payload = json.loads(provider.requests[2].messages[1].content)
        assert payload["previous_summary"] == parent["summary_text"]
        assert payload["transcript"] == [
            {"position": e.position, "message": e.message.model_dump(mode="json")}
            for e in entries[6:]
        ]
        # Both the superseded and active checkpoints still expose original evidence.
        for compaction_id in (first.compaction_id, second.compaction_id):
            source = instance.compactor.read_source(
                session_id=instance.session_id,
                compaction_id=compaction_id,
                start_position=2,
                end_position=3,
            )
            assert [m["position"] for m in source["messages"]] == [2, 3]
            assert source["source_verification"]["verified"] is True
        assert instance.compactor._digest(store.load_positioned_messages(instance.session_id)) == (
            digest
        )
        assert len(provider.requests) == 3
        assert not any(e.type == EventType.TOOL_REQUESTED for e in sink.events)
    finally:
        await instance.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [ProviderErrorKind.AUTHENTICATION, ProviderErrorKind.PAYMENT])
async def test_prefix_auth_and_quota_failure_do_not_fallback(tmp_path, kind):
    instance, provider, store, _ = fallback_engine(tmp_path, [ProviderError("denied", kind=kind)])
    try:
        result = await instance.compact(12, [1])
        assert not result.compacted and len(provider.requests) == 1
        assert instance.compactor.projection(instance.session_id)["cursor_position"] == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_double_failure_keeps_cursor_and_backoff(tmp_path):
    instance, provider, store, _ = fallback_engine(tmp_path, ["tool", "format"])
    try:
        result = await instance.compact(12, [1])
        assert not result.compacted and result.request_count == 2
        assert result.input_tokens == 200 and result.output_tokens == 40
        assert instance.compactor.projection(instance.session_id)["cursor_position"] == 0
        assert (await instance.compact(12, [1])).reason == "failure_backoff"
        assert len(provider.requests) == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_cancel_does_not_fallback(tmp_path):
    instance, provider, store, _ = fallback_engine(tmp_path, [asyncio.CancelledError()])
    try:
        with pytest.raises(asyncio.CancelledError):
            await instance.compact(12, [1])
        assert len(provider.requests) == 1
        assert instance.compactor.projection(instance.session_id)["cursor_position"] == 0
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["no_snapshot", "missing_source", "budget"])
async def test_fallback_preflight_never_silently_drops_source(tmp_path, problem):
    instance, provider, store, _ = fallback_engine(tmp_path, ["ok"])
    try:
        if problem == "no_snapshot":
            instance.frame.prefix_request = None
        elif problem == "missing_source":
            instance.frame.evidence = instance.frame.evidence[1:]
        else:
            instance.config.context.max_input_tokens = 10000
        result = await instance.compact(12, [1])
        if problem == "no_snapshot":
            assert result.compacted and len(provider.requests) == 1
            assert not provider.requests[0].tools
            assert instance.last_metrics["prefix_skip_reason"] == "snapshot_missing"
        else:
            assert not result.compacted and not provider.requests
            assert instance.compactor.projection(instance.session_id)["cursor_position"] == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_prefix_cost_guard_and_source_change_stop_second_request(tmp_path):
    instance, provider, store, _ = fallback_engine(tmp_path, ["tool"])
    try:
        instance.config.model.input_cost_per_million = 1
        instance.config.model.output_cost_per_million = 4
        instance.frame.remaining_cost_usd = 0.001
        result = await instance.compact(12, [1])
        assert result.error == "compaction_cost_budget" and not provider.requests
        instance.retry_after = 0
        instance.frame.remaining_cost_usd = None
        original = provider.stream

        async def changing_source(request):
            async for event in original(request):
                yield event
            store.append_message(
                instance.session_id,
                "run",
                ChatMessage(
                    role=Role.USER, content="New instruction arrived during summarization."
                ),
            )

        provider.stream = changing_source
        result = await instance.compact(12, [1])
        assert result.error == "source_changed_during_compaction"
        assert len(provider.requests) == 1
        assert instance.compactor.projection(instance.session_id)["cursor_position"] == 0
    finally:
        store.close()
