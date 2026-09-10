import hashlib
import sqlite3
from dataclasses import replace

import pytest

from bot.core.context import (
    ContextItem,
    ContextLayer,
    ContextLimitError,
    ContextPlanner,
    ContextRetention,
    ContextTrust,
    SkillDelivery,
    TokenBudget,
    TokenEstimator,
)
from bot.core.models import ChatMessage, ModelRequest, Role, ToolCall
from bot.observability import Redactor
from bot.sessions import SQLiteSessionStore
from bot.skills import SkillCatalog, SkillManager
from bot.skills.models import Skill
from bot.skills.runtime import RunSkillState, SkillContextError


@pytest.fixture
def state(tmp_path):
    store = SQLiteSessionStore(tmp_path / "state.db")
    session = store.create_session(tmp_path)
    catalog = SkillCatalog(tmp_path / "skills")
    catalog.skills["analysis"] = Skill(
        name="analysis",
        description="analysis",
        path=tmp_path / "SKILL.md",
        instructions="TAIL_947",
    )
    state = RunSkillState(
        SkillManager(catalog),
        session_id=session,
        run_id="run",
        mode="history",
        store=store,
        body_budget=16000,
        redactor=Redactor(),
    )
    yield state
    store.close()


def deliver(state):
    prepared = state.prepare("analysis", "test", explicit=True, history=[])
    entry = state.append_body(prepared, kind="explicit_body", key="explicit:run:analysis")
    state.commit(prepared, entry.position)
    return entry


def test_delivery_atomicity_and_idempotency(state):
    state.store._connection.execute(
        "CREATE TRIGGER fail_delivery BEFORE INSERT ON skill_deliveries "
        "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        deliver(state)
    assert not state.active
    assert state.store.load_messages(state.session_id) == []
    state.store._connection.execute("DROP TRIGGER fail_delivery")
    first = deliver(state)
    second = deliver(state)
    assert first == second
    assert len(state.store.load_messages(state.session_id)) == 1
    state.store._connection.execute(
        "UPDATE messages SET message_json = ? WHERE session_id = ?",
        (ChatMessage(role=Role.USER, content="corrupted").model_dump_json(), state.session_id),
    )
    state.store._connection.commit()
    with pytest.raises(ValueError, match="不一致"):
        deliver(state)


@pytest.mark.parametrize("damage", ["missing", "modified", "access_revoked"])
def test_missing_or_corrupt_original_prevents_recovery(state, damage):
    entry = deliver(state)
    reference = entry.skill_delivery.body_ref
    connection = state.store._connection
    if damage == "missing":
        connection.execute("DELETE FROM context_blob_access WHERE blob_id = ?", (reference,))
        connection.execute("DELETE FROM context_blobs WHERE id = ?", (reference,))
    elif damage == "modified":
        connection.execute(
            "UPDATE context_blobs SET content = ? WHERE id = ?", (b"changed", reference)
        )
    else:
        connection.execute("DELETE FROM context_blob_access WHERE blob_id = ?", (reference,))
    connection.commit()
    with pytest.raises(SkillContextError, match="skill_body_(unavailable|version_mismatch)"):
        state.prepare_history([], cursor=entry.position)
    assert len(state.store.load_messages(state.session_id)) == 1


def test_restoration_is_idempotent_and_verifies_final_content(state):
    original = deliver(state)
    restored = state.prepare_history([], cursor=original.position)
    again = state.prepare_history([], cursor=original.position)
    assert restored == again
    assert len(state.store.load_messages(state.session_id)) == 2
    request = ModelRequest(model="mock", messages=[restored[0].message])
    state.check_request(request, object())
    truncated = replace(
        restored[0], message=restored[0].message.model_copy(update={"content": "short"})
    )
    with pytest.raises(SkillContextError, match="version_mismatch"):
        state.prepare_history([truncated], cursor=original.position)
    with pytest.raises(SkillContextError, match="not_visible"):
        state.check_request(ModelRequest(model="mock", messages=[truncated.message]), object())


def test_delivery_origin_survives_all_reads_and_fork(state, tmp_path):
    original = deliver(state)
    store = state.store
    session = state.session_id
    assert store.load_run_positioned_messages("run") == [original]
    assert store.load_messages_at_positions(session, [original.position]) == [original]
    assert store.claim_skill_context_mode(session, "history") == "history"
    fork = store.fork_session(session)
    fork_entry = store.load_positioned_messages(fork)[0]
    assert fork_entry.skill_delivery == original.skill_delivery
    assert not fork_entry.is_real_user
    assert store.get_session(fork)["skill_context_mode"] == "history"
    assert store.read_context_blob(fork, original.skill_delivery.body_ref) is not None
    unrelated = store.create_session(tmp_path)
    assert store.read_context_blob(unrelated, original.skill_delivery.body_ref) is None
    # The stored hash protects provenance from silent text changes.
    store._connection.execute(
        "UPDATE messages SET message_json = ? WHERE session_id = ?",
        (ChatMessage(role=Role.USER, content="forged").model_dump_json(), session),
    )
    store._connection.commit()
    with pytest.raises(ValueError, match="version_mismatch"):
        store.load_positioned_messages(session)


def test_legacy_migration_and_new_session_layout_are_stable(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, "
            "parent_session_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO sessions VALUES ('old', ?, NULL, 'now', 'now')", (str(tmp_path),)
        )
    with_store = SQLiteSessionStore(path)
    try:
        assert with_store.claim_skill_context_mode("old", "history") == "legacy"
        new = with_store.create_session(tmp_path)
        assert with_store.claim_skill_context_mode(new, "history") == "history"
    finally:
        with_store.close()
    reopened = SQLiteSessionStore(path)
    try:
        assert reopened.claim_skill_context_mode(new, "legacy") == "history"
        assert reopened.claim_skill_context_mode("old", "history") == "legacy"
    finally:
        reopened.close()


def test_closed_scope_and_body_budget_do_not_create_bindings(state):
    state.body_budget = 1
    with pytest.raises(SkillContextError, match="budget_exceeded"):
        state.prepare("analysis", "test", explicit=False, history=[])
    assert not state.active
    state.close()
    with pytest.raises(SkillContextError, match="scope_closed"):
        state.prepare("analysis", "test", explicit=True, history=[])


def test_pending_resource_survives_compaction_until_valid_response(state):
    content = "resource instructions: " + "x" * 3000
    reference = state.store.put_context_blob(
        session_id=state.session_id, run_id="run", content=content
    )
    delivery = SkillDelivery(
        kind="resource",
        skill_name="analysis",
        body_ref=reference,
        body_bytes=len(content.encode()),
        version_hash=hashlib.sha256(content.encode()).hexdigest(),
    )
    original = state.store.append_skill_message(
        state.session_id,
        "run",
        ChatMessage(role=Role.TOOL, tool_call_id="resource", content=content),
        delivery,
        delivery_key="resource:run:call",
    )
    state.pending_resources[original.position] = original
    history = state.prepare_history([], cursor=original.position)
    assert len(history) == 1 and history[0].message.role == Role.USER
    assert not history[0].is_real_user
    assert state.prepare_history(history, cursor=original.position) == history
    state.check_request(ModelRequest(model="mock", messages=[history[0].message]), object())
    state.acknowledge_response()
    state.prepare_history(history, cursor=original.position)
    assert not state.required


@pytest.mark.parametrize("with_counter", [False, True])
def test_pinning_result_keeps_entire_tool_batch_even_during_repacking(with_counter):
    planner = ContextPlanner(
        TokenBudget(
            context_window_tokens=500,
            configured_input_limit=500,
            output_reserve_tokens=0,
            protocol_reserve_tokens=0,
            safety_margin_tokens=0,
            target_utilization=1,
        ),
        TokenEstimator(),
    )
    messages = [
        ChatMessage(
            role=Role.ASSISTANT,
            tool_calls=[
                ToolCall(id="a", name="activate_skill", arguments={}),
                ToolCall(id="b", name="read_file", arguments={}),
            ],
        ),
        ChatMessage(role=Role.TOOL, tool_call_id="a", content="Skill rules"),
        ChatMessage(role=Role.TOOL, tool_call_id="b", content="large sibling " * 300),
    ]
    items = [
        ContextItem(
            id=str(i),
            layer=ContextLayer.TOOL_RESULT if i else ContextLayer.RECENT_CONVERSATION,
            message=message,
            source="test",
            trust=ContextTrust.UNTRUSTED,
            retention=ContextRetention.PINNED if i == 1 else ContextRetention.CHECKPOINTED,
            priority=600,
            position=i + 1,
            atomic_group="batch",
            token_estimate=10 if with_counter else 0,
        )
        for i, message in enumerate(messages)
    ]
    with pytest.raises(ContextLimitError):
        planner.pack(items, [], exact_counter=(lambda m, _: 300 * len(m)) if with_counter else None)
