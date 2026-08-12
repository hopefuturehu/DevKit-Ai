from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from bot.core.models import ChatMessage, Role, ToolCall
from bot.sessions import SQLiteSessionStore


def _create_task(
    store: SQLiteSessionStore,
    *,
    parent_session_id: str,
    task_id: str,
    context_refs: list[str] | None = None,
) -> dict:
    return store.create_agent_task(
        task_id=task_id,
        parent_session_id=parent_session_id,
        parent_run_id=f"run-{parent_session_id}",
        agent_name="researcher",
        objective="检查持久化边界",
        constraints=["只读"],
        acceptance_criteria=["返回证据"],
        spec={
            "name": "researcher",
            "description": "只读研究 Agent",
            "instructions": "定位并报告证据",
            "allowed_tools": ["read_file"],
            "isolation": "read_only",
            "max_steps": 8,
            "max_wall_time_seconds": 120,
            "max_cost_usd": None,
            "explicit_skills": [],
        },
        context_refs=context_refs or [],
        required=True,
        isolation="read_only",
        idempotency_key=f"idem-{task_id}",
    )


def test_agent_task_round_trip_survives_store_reopen(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    store = SQLiteSessionStore(database)
    parent_session_id = store.create_session(tmp_path)
    created = _create_task(
        store,
        parent_session_id=parent_session_id,
        task_id="task-round-trip",
    )
    assert store.claim_agent_task(created["id"], owner_id="worker-one")
    result = {
        "summary": "证据已确认",
        "evidence_refs": ["file:src/example.py:10"],
        "input_tokens": 17,
        "output_tokens": 9,
        "cost_usd": 0.003,
    }
    assert store.finish_agent_task(created["id"], status="completed", result=result, error=None)
    store.close()

    reopened = SQLiteSessionStore(database)
    persisted = reopened.get_agent_task(created["id"], parent_session_id=parent_session_id)

    assert persisted is not None
    assert persisted["status"] == "completed"
    assert persisted["child_session_id"] == created["child_session_id"]
    assert persisted["objective"] == "检查持久化边界"
    assert persisted["constraints"] == ["只读"]
    assert persisted["acceptance_criteria"] == ["返回证据"]
    assert persisted["spec"]["name"] == "researcher"
    assert persisted["result"] == result
    assert persisted["started_at"] is not None
    assert persisted["completed_at"] is not None
    assert reopened.agent_task_usage(parent_session_id) == {
        "input_tokens": 17,
        "output_tokens": 9,
        "cost_usd": 0.003,
    }
    reopened.close()


def test_blocked_agent_task_is_a_terminal_result(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "state.db")
    parent_session_id = store.create_session(tmp_path)
    task = _create_task(
        store,
        parent_session_id=parent_session_id,
        task_id="task-blocked",
    )
    assert store.claim_agent_task(task["id"], owner_id="worker-one")

    assert store.finish_agent_task(
        task["id"],
        status="blocked",
        result={"summary": "缺少外部输入"},
        error="no_progress_after_recovery",
    )

    persisted = store.get_agent_task(task["id"])
    assert persisted is not None
    assert persisted["status"] == "blocked"
    assert store.count_agent_tasks(parent_session_id) == 0
    store.close()


def test_agent_tasks_are_isolated_by_parent_session(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "state.db")
    first_parent = store.create_session(tmp_path)
    second_parent = store.create_session(tmp_path)
    first = _create_task(
        store,
        parent_session_id=first_parent,
        task_id="task-first-parent",
    )
    second = _create_task(
        store,
        parent_session_id=second_parent,
        task_id="task-second-parent",
    )

    assert store.get_agent_task(first["id"], parent_session_id=second_parent) is None
    assert [task["id"] for task in store.list_agent_tasks(first_parent)] == [first["id"]]
    assert [task["id"] for task in store.list_agent_tasks(second_parent)] == [second["id"]]
    assert (
        store.request_agent_task_cancel(
            first["id"], parent_session_id=second_parent, reason="越权取消"
        )
        is None
    )
    assert store.get_agent_task(first["id"])["status"] == "queued"
    store.close()


def test_claim_agent_task_is_cas_across_store_connections(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    first_store = SQLiteSessionStore(database)
    parent_session_id = first_store.create_session(tmp_path)
    task = _create_task(
        first_store,
        parent_session_id=parent_session_id,
        task_id="task-claim-cas",
    )
    second_store = SQLiteSessionStore(database)
    barrier = Barrier(2)

    def claim(store: SQLiteSessionStore, owner_id: str) -> bool:
        barrier.wait()
        return store.claim_agent_task(task["id"], owner_id=owner_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(
            executor.map(
                lambda item: claim(*item),
                ((first_store, "worker-one"), (second_store, "worker-two")),
            )
        )

    assert sorted(claims) == [False, True]
    assert first_store.get_agent_task(task["id"])["status"] == "running"
    first_store.close()
    second_store.close()


def test_cancel_complete_race_cannot_regress_a_terminal_task(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    first_store = SQLiteSessionStore(database)
    parent_session_id = first_store.create_session(tmp_path)
    task = _create_task(
        first_store,
        parent_session_id=parent_session_id,
        task_id="task-cancel-complete-race",
    )
    assert first_store.claim_agent_task(task["id"], owner_id="worker-one")
    second_store = SQLiteSessionStore(database)
    barrier = Barrier(2)

    def cancel() -> str | None:
        barrier.wait()
        return first_store.request_agent_task_cancel(
            task["id"], parent_session_id=parent_session_id, reason="用户取消"
        )

    def complete() -> bool:
        barrier.wait()
        return second_store.finish_agent_task(
            task["id"],
            status="completed",
            result={"summary": "任务已在取消生效前完成"},
            error=None,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        cancel_future = executor.submit(cancel)
        complete_future = executor.submit(complete)
        cancel_result = cancel_future.result()
        complete_result = complete_future.result()

    if complete_result:
        assert cancel_result == "completed"
        expected_status = "completed"
        assert first_store.get_agent_task(task["id"])["result"] == {
            "summary": "任务已在取消生效前完成"
        }
    else:
        assert cancel_result == "cancelling"
        assert first_store.finish_agent_task(
            task["id"], status="cancelled", result=None, error="用户取消"
        )
        expected_status = "cancelled"

    # Whichever terminal transition won cannot be overwritten by a stale result.
    assert not second_store.finish_agent_task(
        task["id"],
        status="completed",
        result={"summary": "迟到结果"},
        error=None,
    )
    assert first_store.get_agent_task(task["id"])["status"] == expected_status
    first_store.close()
    second_store.close()


def test_recovery_interrupts_running_but_preserves_queued_tasks(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    store = SQLiteSessionStore(database)
    parent_session_id = store.create_session(tmp_path)
    running = _create_task(
        store,
        parent_session_id=parent_session_id,
        task_id="task-running-at-crash",
    )
    queued = _create_task(
        store,
        parent_session_id=parent_session_id,
        task_id="task-still-queued",
    )
    assert store.claim_agent_task(running["id"], owner_id="dead-worker")
    store.close()

    recovered = SQLiteSessionStore(database)
    interrupted = recovered.interrupt_recoverable_agent_tasks(workspace=tmp_path)

    assert interrupted == [running["id"]]
    assert recovered.get_agent_task(running["id"])["status"] == "interrupted"
    assert recovered.get_agent_task(running["id"])["completed_at"] is not None
    assert recovered.get_agent_task(queued["id"])["status"] == "queued"
    recovered.close()


def test_child_session_requires_explicit_context_blob_grant(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "state.db")
    parent_session_id = store.create_session(tmp_path)
    unrelated_session_id = store.create_session(tmp_path)
    reference = store.put_context_blob(
        session_id=parent_session_id,
        run_id="parent-run",
        content="仅允许显式委托的证据",
    )
    task = _create_task(
        store,
        parent_session_id=parent_session_id,
        task_id="task-explicit-context-grant",
        context_refs=[reference],
    )
    child_session_id = task["child_session_id"]

    assert store.read_context_blob(child_session_id, reference) is None
    assert (
        store.grant_context_blob_access(
            source_session_id=unrelated_session_id,
            target_session_id=child_session_id,
            reference=reference,
        )
        is False
    )
    assert store.read_context_blob(child_session_id, reference) is None

    assert store.grant_context_blob_access(
        source_session_id=parent_session_id,
        target_session_id=child_session_id,
        reference=reference,
    )
    assert store.read_context_blob(child_session_id, reference)["content"] == (
        "仅允许显式委托的证据"
    )
    assert store.read_context_blob(unrelated_session_id, reference) is None
    store.close()


def test_recovery_and_queue_queries_are_scoped_to_workspace(tmp_path: Path) -> None:
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    store = SQLiteSessionStore(tmp_path / "global-state.db")
    first_parent = store.create_session(first_workspace)
    second_parent = store.create_session(second_workspace)
    first_running = _create_task(
        store,
        parent_session_id=first_parent,
        task_id="first-running",
    )
    first_queued = _create_task(
        store,
        parent_session_id=first_parent,
        task_id="first-queued",
    )
    second_running = _create_task(
        store,
        parent_session_id=second_parent,
        task_id="second-running",
    )
    second_queued = _create_task(
        store,
        parent_session_id=second_parent,
        task_id="second-queued",
    )
    assert store.claim_agent_task(first_running["id"], owner_id="first-owner")
    assert store.claim_agent_task(second_running["id"], owner_id="second-owner")

    interrupted = store.interrupt_recoverable_agent_tasks(workspace=first_workspace)

    assert interrupted == [first_running["id"]]
    assert store.get_agent_task(first_running["id"])["status"] == "interrupted"
    assert store.get_agent_task(second_running["id"])["status"] == "running"
    assert [
        task["id"]
        for task in store.list_agent_tasks(
            workspace=first_workspace,
            statuses=["queued"],
        )
    ] == [first_queued["id"]]
    assert store.count_agent_tasks(workspace=first_workspace) == 1
    assert store.count_agent_tasks(workspace=second_workspace) == 2
    assert store.get_agent_task(second_queued["id"])["status"] == "queued"
    store.close()


def test_parent_result_delivery_and_report_ack_are_atomic_and_scoped(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "state.db")
    parent_session_id = store.create_session(tmp_path)
    other_parent_id = store.create_session(tmp_path)
    completed = _create_task(
        store,
        parent_session_id=parent_session_id,
        task_id="completed-for-parent",
    )
    queued = _create_task(
        store,
        parent_session_id=parent_session_id,
        task_id="queued-for-parent",
    )
    foreign = _create_task(
        store,
        parent_session_id=other_parent_id,
        task_id="completed-for-other-parent",
    )
    for task in (completed, foreign):
        assert store.claim_agent_task(task["id"], owner_id="worker")
        assert store.finish_agent_task(
            task["id"],
            status="completed",
            result={"summary": "done"},
            error=None,
        )

    tool_call_id = "required-agents-call"
    positions = store.append_messages_and_mark_agent_tasks_reported(
        session_id=parent_session_id,
        run_id="parent-run",
        messages=[
            ChatMessage(
                role=Role.ASSISTANT,
                tool_calls=[
                    ToolCall(
                        id=tool_call_id,
                        name="await_agents",
                        arguments={"task_ids": [completed["id"]]},
                    )
                ],
            ),
            ChatMessage(
                role=Role.TOOL,
                name="await_agents",
                tool_call_id=tool_call_id,
                content='{"status":"completed"}',
            ),
        ],
        task_ids=[completed["id"], queued["id"], foreign["id"]],
    )

    assert positions == [1, 2]
    messages = store.load_messages(parent_session_id)
    assert [message.role for message in messages] == [Role.ASSISTANT, Role.TOOL]
    assert store.get_agent_task(completed["id"])["reported_at"] is not None
    assert store.get_agent_task(queued["id"])["reported_at"] is None
    assert store.get_agent_task(foreign["id"])["reported_at"] is None
    store.close()
