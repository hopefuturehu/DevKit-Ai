from pathlib import Path

import pytest

from bot.core.context import (
    CORE_POLICY,
    ContextItem,
    ContextLayer,
    ContextLimitError,
    ContextPlanner,
    ContextRetention,
    ContextSnapshot,
    ContextTrust,
    PositionedMessage,
    SnapshotBuilder,
    TokenBudget,
    TokenEstimator,
    repair_tool_protocol,
)
from bot.core.models import ChatMessage, Role, ToolCall
from bot.sessions import SQLiteSessionStore


def test_core_policy_keeps_managed_process_guidance_stable() -> None:
    assert "长命令可能返回 process_id" in CORE_POLICY
    assert "poll_process" in CORE_POLICY
    assert "只有普通 Transcript 中 role=user" in CORE_POLICY
    assert "必须核验原始 Transcript" in CORE_POLICY


def test_token_budget_reserves_output_protocol_and_safety() -> None:
    budget = TokenBudget(
        context_window_tokens=10_000,
        configured_input_limit=9_000,
        output_reserve_tokens=2_000,
        protocol_reserve_tokens=500,
        safety_margin_tokens=500,
        target_utilization=0.8,
    )

    assert budget.hard_input_limit == 7_000
    assert budget.target_input_limit == 5_600


def test_unicode_estimator_does_not_treat_cjk_as_quarter_token() -> None:
    estimator = TokenEstimator()

    assert estimator.text("中" * 1_000) >= 1_000
    assert estimator.text("a" * 1_000) < estimator.text("中" * 1_000)


def test_planner_keeps_assistant_tool_group_atomic() -> None:
    estimator = TokenEstimator()
    planner = ContextPlanner(
        TokenBudget(
            context_window_tokens=600,
            configured_input_limit=600,
            output_reserve_tokens=0,
            protocol_reserve_tokens=0,
            safety_margin_tokens=0,
            target_utilization=0.5,
        ),
        estimator,
    )
    items = [
        ContextItem(
            id="policy",
            layer=ContextLayer.CORE_POLICY,
            message=ChatMessage(role=Role.SYSTEM, content="policy"),
            source="test",
            trust=ContextTrust.TRUSTED,
            retention=ContextRetention.PINNED,
            priority=1000,
        ),
        ContextItem(
            id="assistant",
            layer=ContextLayer.RECENT_CONVERSATION,
            message=ChatMessage(
                role=Role.ASSISTANT,
                tool_calls=[ToolCall(id="call", name="demo", arguments={})],
            ),
            source="test",
            trust=ContextTrust.UNTRUSTED,
            retention=ContextRetention.CHECKPOINTED,
            priority=600,
            atomic_group="turn",
            position=1,
        ),
        ContextItem(
            id="tool",
            layer=ContextLayer.TOOL_RESULT,
            message=ChatMessage(
                role=Role.TOOL,
                tool_call_id="call",
                content="x" * 2_000,
            ),
            source="test",
            trust=ContextTrust.UNTRUSTED,
            retention=ContextRetention.CHECKPOINTED,
            priority=600,
            atomic_group="turn",
            position=2,
        ),
        ContextItem(
            id="latest-user",
            layer=ContextLayer.RECENT_CONVERSATION,
            message=ChatMessage(role=Role.USER, content="latest"),
            source="test",
            trust=ContextTrust.USER,
            retention=ContextRetention.CHECKPOINTED,
            priority=700,
            position=3,
        ),
    ]

    pack = planner.pack(items, [])

    ids = {item["id"] for item in pack.dropped_items}
    assert {"assistant", "tool"}.issubset(ids)
    assert all(message.tool_call_id != "call" for message in pack.messages)


def test_planner_renders_stable_prefix_before_history_and_volatile_suffix() -> None:
    estimator = TokenEstimator()
    planner = ContextPlanner(TokenBudget(10_000, 10_000, 0, 0, 0, 1.0), estimator)

    def item(
        identifier: str,
        layer: ContextLayer,
        *,
        position: int | None = None,
    ) -> ContextItem:
        return ContextItem(
            id=identifier,
            layer=layer,
            message=ChatMessage(role=Role.USER, content=identifier),
            source="test",
            trust=ContextTrust.USER,
            retention=ContextRetention.PINNED,
            priority=1,
            position=position,
        )

    items = [
        item("runtime", ContextLayer.RUNTIME_NOTE),
        item("history-new", ContextLayer.RECENT_CONVERSATION, position=20),
        item("automatic-memory", ContextLayer.AUTOMATIC_MEMORY),
        item("compaction", ContextLayer.COMPACTION, position=10),
        item("explicit-memory", ContextLayer.MEMORY),
        item("active-skill", ContextLayer.ACTIVE_SKILL),
        item("tool-catalog", ContextLayer.TOOL_CATALOG),
        item("skill-catalog", ContextLayer.SKILL_CATALOG),
        item("environment", ContextLayer.ENVIRONMENT),
        item("project", ContextLayer.PROJECT_INSTRUCTION),
        item("policy", ContextLayer.CORE_POLICY),
        item("history-old", ContextLayer.RECENT_CONVERSATION, position=11),
    ]

    pack = planner.pack(items, [])

    assert [message.content for message in pack.messages] == [
        "policy",
        "project",
        "environment",
        "skill-catalog",
        "tool-catalog",
        "active-skill",
        "explicit-memory",
        "automatic-memory",
        "compaction",
        "history-old",
        "history-new",
        "runtime",
    ]


def test_tool_protocol_repair_moves_steering_after_complete_tool_batch() -> None:
    assistant = ChatMessage(
        role=Role.ASSISTANT,
        tool_calls=[
            ToolCall(id="call-a", name="first", arguments={}),
            ToolCall(id="call-b", name="second", arguments={}),
        ],
    )
    steering = ChatMessage(role=Role.USER, content="change direction")
    first = ChatMessage(role=Role.TOOL, tool_call_id="call-a", content="a")
    second = ChatMessage(role=Role.TOOL, tool_call_id="call-b", content="b")

    repaired, report = repair_tool_protocol([assistant, steering, first, second])

    assert repaired == [assistant, first, second, steering]
    assert report.moved_tool_results == 2
    assert report.synthesized_tool_results == 0
    assert report.dropped_orphan_tool_results == 0


def test_tool_protocol_repair_synthesizes_missing_and_drops_orphan_results() -> None:
    assistant = ChatMessage(
        role=Role.ASSISTANT,
        tool_calls=[ToolCall(id="missing", name="demo", arguments={})],
    )
    orphan = ChatMessage(role=Role.TOOL, tool_call_id="orphan", content="unknown")

    repaired, report = repair_tool_protocol([orphan, assistant])

    assert [message.role for message in repaired] == [Role.ASSISTANT, Role.TOOL]
    assert repaired[1].tool_call_id == "missing"
    assert "结果未知" in (repaired[1].content or "")
    assert report.synthesized_tool_results == 1
    assert report.dropped_orphan_tool_results == 1


def test_tool_protocol_repair_can_leave_live_tool_call_unresolved() -> None:
    assistant = ChatMessage(
        role=Role.ASSISTANT,
        tool_calls=[ToolCall(id="live", name="demo", arguments={})],
    )

    repaired, report = repair_tool_protocol(
        [assistant],
        synthesize_missing=lambda _owner_index, _call: False,
    )

    assert repaired == [assistant]
    assert report.synthesized_tool_results == 0
    assert [issue.tool_call_id for issue in report.unresolved_calls] == ["live"]


def test_planner_reports_irreducible_pinned_overflow() -> None:
    estimator = TokenEstimator()
    planner = ContextPlanner(
        TokenBudget(100, 100, 0, 0, 0, 0.8),
        estimator,
    )
    item = ContextItem(
        id="policy",
        layer=ContextLayer.CORE_POLICY,
        message=ChatMessage(role=Role.SYSTEM, content="中" * 500),
        source="test",
        trust=ContextTrust.TRUSTED,
        retention=ContextRetention.PINNED,
        priority=1000,
    )

    with pytest.raises(ContextLimitError) as caught:
        planner.pack([item], [])

    assert caught.value.report["overflow_tokens"] > 0
    assert caught.value.report["layers"]["core_policy"] >= 500


def test_planner_repacks_when_exact_counter_exceeds_hard_limit() -> None:
    estimator = TokenEstimator()
    planner = ContextPlanner(TokenBudget(100, 100, 0, 0, 0, 0.8), estimator)
    pinned = ContextItem(
        id="policy",
        layer=ContextLayer.CORE_POLICY,
        message=ChatMessage(role=Role.SYSTEM, content="policy"),
        source="test",
        trust=ContextTrust.TRUSTED,
        retention=ContextRetention.PINNED,
        priority=1000,
    )
    optional = ContextItem(
        id="history",
        layer=ContextLayer.RECENT_CONVERSATION,
        message=ChatMessage(role=Role.USER, content="history"),
        source="test",
        trust=ContextTrust.USER,
        retention=ContextRetention.CHECKPOINTED,
        priority=500,
    )

    pack = planner.pack(
        [pinned, optional],
        [],
        exact_counter=lambda messages, tools: 200 if len(messages) > 1 else 50,
    )

    assert pack.exact_tokens == 50
    assert [message.content for message in pack.messages] == ["policy"]
    assert pack.dropped_items[-1]["reason"] == "exact_token_repack"


def test_snapshot_merges_previous_without_summary_stacking() -> None:
    estimator = TokenEstimator()
    builder = SnapshotBuilder(estimator, max_field_chars=100)
    first = builder.build(
        [PositionedMessage(1, ChatMessage(role=Role.USER, content="first objective"))]
    )
    second = builder.build(
        [PositionedMessage(2, ChatMessage(role=Role.ASSISTANT, content="done"))],
        previous=first,
    )

    assert second.cursor_position == 2
    assert second.objective == "first objective"
    assert second.completed == ["done"]
    assert second.model_message().role == Role.USER
    assert second.model_message().content.count("Context checkpoint") == 1


def test_store_supersedes_snapshots_and_reads_blob_chunks(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "state.db")
    session_id = store.create_session(tmp_path)
    first = ContextSnapshot(cursor_position=1, objective="one", token_estimate=10)
    second = ContextSnapshot(cursor_position=2, objective="two", token_estimate=10)

    first_id = store.save_context_snapshot(
        session_id=session_id,
        run_id="r1",
        snapshot=first,
    )
    second_id = store.save_context_snapshot(
        session_id=session_id,
        run_id="r2",
        snapshot=second,
    )
    reference = store.put_context_blob(
        session_id=session_id,
        run_id="r2",
        content="abcdefgh",
    )
    chunk = store.read_context_blob(session_id, reference, offset=2, limit=3)

    rows = store.list_context_snapshots(session_id)
    assert [row["status"] for row in rows] == ["superseded", "ready"]
    assert store.latest_context_snapshot(session_id)[0] == second_id
    assert first_id != second_id
    assert chunk is not None
    assert chunk["content"] == "cde"
    assert chunk["next_offset"] == 5
    assert chunk["eof"] is False
    store.close()


def test_store_searches_context_blob_with_bounded_previews(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "state.db")
    session_id = store.create_session(tmp_path)
    content = "前言\nAlpha first\n间隔\nalpha second\n结尾"
    reference = store.put_context_blob(
        session_id=session_id,
        run_id="r1",
        content=content,
    )

    result = store.search_context_blob(
        session_id,
        reference,
        query="ALPHA",
        max_matches=1,
        context_chars=4,
    )

    assert result is not None
    assert result["truncated"] is True
    assert len(result["matches"]) == 1
    match = result["matches"][0]
    assert match["line"] == 2
    assert match["byte_offset"] == len("前言\n".encode())
    assert "Alpha" in match["preview"]
    chunk = store.read_context_blob(
        session_id,
        reference,
        offset=match["load_offset"],
        limit=match["load_limit"],
    )
    assert chunk is not None
    assert chunk["content"] == match["preview"]
    store.close()


def test_blob_access_is_session_scoped_and_follows_forked_messages(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "state.db")
    source = store.create_session(tmp_path)
    unrelated = store.create_session(tmp_path)
    reference = store.put_context_blob(
        session_id=source,
        run_id="r1",
        content="private evidence",
    )
    store.append_message(
        source,
        "r1",
        ChatMessage(role=Role.USER, content=f"evidence: context_ref={reference}"),
    )

    forked = store.fork_session(source)

    assert store.read_context_blob(unrelated, reference) is None
    assert store.read_context_blob(forked, reference)["content"] == "private evidence"
    store.close()
