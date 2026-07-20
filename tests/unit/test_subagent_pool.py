from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from bot.config.models import AppConfig
from bot.core.approval import ApprovalResponse
from bot.core.events import EventBus, MemoryEventSink
from bot.core.models import RunRequest, RunResult, ToolCall
from bot.execution import EnvironmentCapabilities, ExecutionTarget, ProcessEvent, ProcessSpec
from bot.policy import PolicyDecision, PolicyDecisionKind, ToolAction
from bot.sessions import SQLiteSessionStore
from bot.subagents import AgentSpec, BackgroundAgentPool, WorkerIsolation, WorkerStatus
from bot.tools import ToolAnnotations


class NoCommandExecutionTarget(ExecutionTarget):
    async def probe(self, executables=None) -> EnvironmentCapabilities:
        return EnvironmentCapabilities(
            operating_system="test",
            architecture="test",
            executables={name: None for name in executables or []},
        )

    def execute(self, spec: ProcessSpec) -> AsyncIterator[ProcessEvent]:
        raise AssertionError(f"read-only pool test must not execute commands: {spec.argv}")


class ControlledRunnerFactory:
    """Factory whose runners stay alive until the test releases one shared event."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.changed = asyncio.Event()
        self.started = 0
        self.active = 0
        self.max_active = 0
        self.cancelled = 0
        self.requests: list[RunRequest] = []

    def __call__(self, spec: AgentSpec, workspace: Path, approval_handler):
        factory = self

        class ControlledRunner:
            async def run(self, request: RunRequest) -> RunResult:
                factory.requests.append(request)
                factory.started += 1
                factory.active += 1
                factory.max_active = max(factory.max_active, factory.active)
                factory.changed.set()
                try:
                    await factory.release.wait()
                except asyncio.CancelledError:
                    factory.cancelled += 1
                    raise
                finally:
                    factory.active -= 1
                    factory.changed.set()
                return RunResult(
                    session_id=request.session_id or "missing-child-session",
                    status="completed",
                    final_text=f"completed: {request.prompt}",
                )

        return ControlledRunner()

    async def wait_started(self, count: int) -> None:
        async def wait() -> None:
            while self.started < count:
                self.changed.clear()
                if self.started >= count:
                    return
                await self.changed.wait()

        await asyncio.wait_for(wait(), timeout=1.0)


class PoolHarness:
    def __init__(self, tmp_path: Path, *, max_concurrent: int = 1) -> None:
        self.config = AppConfig.model_validate(
            {
                "subagents": {
                    "max_concurrent": max_concurrent,
                    "max_queued": 16,
                    "max_tasks_per_session": 16,
                    "allow_worktree_writes": False,
                    "shutdown_grace_seconds": 0.2,
                }
            }
        )
        self.store = SQLiteSessionStore(tmp_path / "state.db")
        self.parent_a = self.store.create_session(tmp_path)
        self.parent_b = self.store.create_session(tmp_path)
        self.runner_factory = ControlledRunnerFactory()
        self.event_sink = MemoryEventSink()
        self.pool = BackgroundAgentPool(
            config=self.config,
            workspace=tmp_path,
            store=self.store,
            event_bus=EventBus([self.event_sink]),
            execution_target=NoCommandExecutionTarget(),
            specs=[
                AgentSpec(
                    name="explorer",
                    description="test explorer",
                    instructions="read only",
                    isolation=WorkerIsolation.READ_ONLY,
                )
            ],
            runner_factory=self.runner_factory,
        )

    async def execute(
        self,
        name: str,
        arguments: dict,
        *,
        parent_session_id: str | None = None,
        call_id: str | None = None,
    ):
        return await self.pool.execute(
            ToolCall(
                id=call_id or f"call-{name}",
                name=name,
                arguments=arguments,
            ),
            parent_session_id=parent_session_id or self.parent_a,
            parent_run_id="parent-run",
        )

    async def spawn(self, objective: str, *, call_id: str) -> dict:
        result = await self.execute(
            "spawn_agent",
            {"agent": "explorer", "objective": objective},
            call_id=call_id,
        )
        assert result.success, result.error
        return json.loads(result.output or "{}")

    async def close(self) -> None:
        await self.pool.shutdown()
        self.store.close()


def task_id(payload: dict) -> str:
    return str(payload["task"]["id"])


@pytest.mark.asyncio
async def test_spawn_is_non_blocking_while_worker_is_still_running(tmp_path: Path) -> None:
    harness = PoolHarness(tmp_path)
    try:
        payload = await asyncio.wait_for(
            harness.spawn("inspect the repository", call_id="spawn-non-blocking"),
            timeout=0.2,
        )

        assert payload["task"]["status"] == WorkerStatus.QUEUED.value
        await harness.runner_factory.wait_started(1)
        assert harness.runner_factory.active == 1
        assert not harness.runner_factory.release.is_set()
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_pool_enforces_max_concurrent_workers(tmp_path: Path) -> None:
    harness = PoolHarness(tmp_path, max_concurrent=2)
    try:
        payloads = [
            await harness.spawn(f"task {index}", call_id=f"spawn-{index}") for index in range(3)
        ]
        ids = [task_id(payload) for payload in payloads]

        await harness.runner_factory.wait_started(2)
        assert harness.runner_factory.active == 2
        assert harness.runner_factory.max_active == 2
        assert harness.store.get_agent_task(ids[2])["status"] == WorkerStatus.QUEUED.value

        harness.runner_factory.release.set()
        result = await harness.execute(
            "await_agents",
            {"task_ids": ids, "timeout_seconds": 1},
        )
        assert result.success, result.error
        awaited = json.loads(result.output or "{}")
        assert awaited["completed"] is True
        assert {task["status"] for task in awaited["tasks"]} == {WorkerStatus.COMPLETED.value}
        assert harness.runner_factory.max_active == 2
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_await_timeout_does_not_cancel_worker(tmp_path: Path) -> None:
    harness = PoolHarness(tmp_path)
    try:
        spawned = await harness.spawn("slow task", call_id="spawn-slow")
        identifier = task_id(spawned)
        await harness.runner_factory.wait_started(1)

        result = await harness.execute(
            "await_agents",
            {"task_ids": [identifier], "timeout_seconds": 0.1},
        )
        assert result.success, result.error
        awaited = json.loads(result.output or "{}")
        assert awaited["completed"] is False
        assert awaited["timed_out"] is True
        assert awaited["tasks"][0]["status"] == WorkerStatus.RUNNING.value
        assert harness.runner_factory.active == 1
        assert harness.runner_factory.cancelled == 0

        harness.runner_factory.release.set()
        completed = await harness.execute(
            "await_agents",
            {"task_ids": [identifier], "timeout_seconds": 1},
        )
        assert completed.success, completed.error
        assert json.loads(completed.output or "{}")["completed"] is True
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_cancel_running_and_queued_tasks_is_idempotent(tmp_path: Path) -> None:
    harness = PoolHarness(tmp_path, max_concurrent=1)
    try:
        running = task_id(await harness.spawn("running", call_id="spawn-running"))
        await harness.runner_factory.wait_started(1)
        queued = task_id(await harness.spawn("queued", call_id="spawn-queued"))
        assert harness.store.get_agent_task(queued)["status"] == WorkerStatus.QUEUED.value

        queued_cancellations = []
        for _ in range(2):
            response = await harness.execute(
                "cancel_agent",
                {"task_id": queued, "reason": "not needed"},
            )
            assert response.success, response.error
            queued_cancellations.append(json.loads(response.output or "{}"))
        assert [item["cancel_status"] for item in queued_cancellations] == [
            WorkerStatus.CANCELLED.value,
            WorkerStatus.CANCELLED.value,
        ]

        first_running_cancel = await harness.execute(
            "cancel_agent",
            {"task_id": running, "reason": "stop running"},
        )
        assert first_running_cancel.success, first_running_cancel.error
        assert json.loads(first_running_cancel.output or "{}")["cancel_status"] in {
            WorkerStatus.CANCELLING.value,
            WorkerStatus.CANCELLED.value,
        }
        terminal = await harness.execute(
            "await_agents",
            {"task_ids": [running], "timeout_seconds": 1},
        )
        assert terminal.success, terminal.error
        assert json.loads(terminal.output or "{}")["tasks"][0]["status"] == (
            WorkerStatus.CANCELLED.value
        )

        second_running_cancel = await harness.execute(
            "cancel_agent",
            {"task_id": running, "reason": "stop again"},
        )
        assert second_running_cancel.success, second_running_cancel.error
        assert json.loads(second_running_cancel.output or "{}")["cancel_status"] == (
            WorkerStatus.CANCELLED.value
        )
        assert harness.runner_factory.cancelled == 1
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_parent_session_cannot_query_await_or_cancel_another_parents_task(
    tmp_path: Path,
) -> None:
    harness = PoolHarness(tmp_path)
    try:
        identifier = task_id(await harness.spawn("private task", call_id="spawn-private"))
        await harness.runner_factory.wait_started(1)

        attempts = [
            await harness.execute(
                "get_agent_status",
                {"task_ids": [identifier]},
                parent_session_id=harness.parent_b,
            ),
            await harness.execute(
                "await_agents",
                {"task_ids": [identifier], "timeout_seconds": 0.1},
                parent_session_id=harness.parent_b,
            ),
            await harness.execute(
                "cancel_agent",
                {"task_id": identifier},
                parent_session_id=harness.parent_b,
            ),
        ]

        assert all(not attempt.success for attempt in attempts)
        assert all("不属于当前父会话" in (attempt.error or "") for attempt in attempts)
        assert harness.store.get_agent_task(identifier)["status"] == WorkerStatus.RUNNING.value
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_shutdown_leaves_no_in_memory_tasks(tmp_path: Path) -> None:
    harness = PoolHarness(tmp_path, max_concurrent=1)
    try:
        await harness.spawn("running during shutdown", call_id="spawn-shutdown-running")
        await harness.runner_factory.wait_started(1)
        await harness.spawn("queued during shutdown", call_id="spawn-shutdown-queued")

        await harness.pool.shutdown()

        assert harness.pool._scheduled == {}
        assert harness.runner_factory.active == 0
        assert not [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and task.get_name().startswith("subagent-")
        ]
        statuses = {
            WorkerStatus(record["status"])
            for record in harness.store.list_agent_tasks(harness.parent_a)
        }
        assert statuses <= {
            WorkerStatus.CANCELLED,
            WorkerStatus.INTERRUPTED,
        }
        assert len(statuses) >= 1
    finally:
        await harness.close()


class GatedApprovalHandler:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def approve(self, action, decision) -> ApprovalResponse:
        self.entered.set()
        await self.release.wait()
        return ApprovalResponse(approved=False)


class ApprovalAwareRunnerFactory:
    def __init__(self) -> None:
        self.second_completed = asyncio.Event()

    def __call__(self, spec: AgentSpec, workspace: Path, approval_handler):
        factory = self

        class Runner:
            async def run(self, request: RunRequest) -> RunResult:
                if "需要审批" in request.prompt:
                    await approval_handler.approve(
                        ToolAction(
                            tool_name="network_probe",
                            arguments={},
                            annotations=ToolAnnotations(network_access=True),
                        ),
                        PolicyDecision(
                            kind=PolicyDecisionKind.ASK,
                            reason="测试后台审批等待",
                        ),
                    )
                else:
                    factory.second_completed.set()
                return RunResult(
                    session_id=request.session_id or "missing",
                    status="completed",
                    final_text="done",
                )

        return Runner()


@pytest.mark.asyncio
async def test_waiting_approval_releases_concurrency_slot(tmp_path: Path) -> None:
    config = AppConfig.model_validate(
        {
            "subagents": {
                "max_concurrent": 1,
                "allow_worktree_writes": False,
                "shutdown_grace_seconds": 0.2,
            }
        }
    )
    store = SQLiteSessionStore(tmp_path / "state.db")
    parent = store.create_session(tmp_path)
    approval = GatedApprovalHandler()
    factory = ApprovalAwareRunnerFactory()
    pool = BackgroundAgentPool(
        config=config,
        workspace=tmp_path,
        store=store,
        event_bus=EventBus([store]),
        execution_target=NoCommandExecutionTarget(),
        specs=[
            AgentSpec(
                name="explorer",
                description="test",
                instructions="test",
                isolation=WorkerIsolation.READ_ONLY,
            )
        ],
        runner_factory=factory,
        approval_handler=approval,
    )

    async def spawn(call_id: str, objective: str) -> str:
        response = await pool.execute(
            ToolCall(
                id=call_id,
                name="spawn_agent",
                arguments={"agent": "explorer", "objective": objective},
            ),
            parent_session_id=parent,
            parent_run_id="parent-run",
        )
        assert response.success, response.error
        return json.loads(response.output)["task"]["id"]

    try:
        waiting_id = await spawn("approval-first", "需要审批")
        await asyncio.wait_for(approval.entered.wait(), timeout=1)
        assert store.get_agent_task(waiting_id)["status"] == "waiting_approval"

        second_id = await spawn("approval-second", "无需审批")
        await asyncio.wait_for(factory.second_completed.wait(), timeout=1)
        assert store.get_agent_task(second_id)["status"] == "completed"
        assert store.get_agent_task(waiting_id)["status"] == "waiting_approval"

        approval.release.set()
        awaited = await pool.execute(
            ToolCall(
                id="approval-await",
                name="await_agents",
                arguments={"task_ids": [waiting_id, second_id], "timeout_seconds": 1},
            ),
            parent_session_id=parent,
            parent_run_id="parent-run",
        )
        assert awaited.success, awaited.error
        assert json.loads(awaited.output)["completed"] is True
    finally:
        await pool.shutdown()
        store.close()
