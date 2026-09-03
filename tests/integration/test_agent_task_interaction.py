import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from bot.config.models import AppConfig
from bot.core.events import EventBus
from bot.core.models import RunRequest, RunResult, ToolCall
from bot.execution import EnvironmentCapabilities, ExecutionTarget, ProcessEvent, ProcessSpec
from bot.sessions import SQLiteSessionStore
from bot.subagents import AgentSpec, BackgroundAgentPool, WorkerIsolation


class NoExecutionTarget(ExecutionTarget):
    async def probe(self, executables=None) -> EnvironmentCapabilities:
        return EnvironmentCapabilities(operating_system="test", architecture="test", executables={})

    def execute(self, spec: ProcessSpec) -> AsyncIterator[ProcessEvent]:
        raise AssertionError(spec.argv)


class ConversationalRunnerFactory:
    def __init__(self) -> None:
        self.calls: list[RunRequest] = []

    def __call__(self, spec, workspace, approval_handler):
        factory = self

        class Runner:
            async def run(self, request: RunRequest) -> RunResult:
                factory.calls.append(request)
                if len(factory.calls) == 1:
                    output = {
                        "status": "needs_input",
                        "summary": "需要兼容策略",
                        "questions": ["是否兼容旧 token？"],
                        "findings": [],
                        "evidence_refs": [],
                        "verification": [],
                        "risks": [],
                        "unresolved": [],
                    }
                else:
                    output = {
                        "status": "completed",
                        "summary": "已按兼容策略完成",
                        "questions": [],
                        "findings": ["保留旧 token 读取"],
                        "evidence_refs": [],
                        "verification": ["tests passed"],
                        "risks": [],
                        "unresolved": [],
                    }
                return RunResult(
                    session_id=request.session_id or "missing",
                    status="completed",
                    final_text=json.dumps(output, ensure_ascii=False),
                )

        return Runner()


@pytest.mark.asyncio
async def test_foreground_task_can_pause_for_input_and_resume_same_child_session(
    tmp_path: Path,
) -> None:
    config = AppConfig.model_validate(
        {
            "subagents": {"allow_worktree_writes": False},
            "agents": {"required_wait_timeout_seconds": 5},
        }
    )
    store = SQLiteSessionStore(tmp_path / "state.db")
    parent = store.create_session(tmp_path)
    factory = ConversationalRunnerFactory()
    pool = BackgroundAgentPool(
        config=config,
        workspace=tmp_path,
        store=store,
        event_bus=EventBus([store]),
        execution_target=NoExecutionTarget(),
        specs=[
            AgentSpec(
                name="reviewer",
                description="test",
                instructions="review",
                isolation=WorkerIsolation.READ_ONLY,
            )
        ],
        runner_factory=factory,
    )
    try:
        first = await pool.execute(
            ToolCall(
                id="task-first",
                name="task",
                arguments={"agent": "reviewer", "prompt": "检查 token"},
            ),
            parent_session_id=parent,
            parent_run_id="parent-1",
        )
        assert first.success, first.error
        first_payload = json.loads(first.output)
        task = first_payload["task"]
        assert task["status"] == "waiting_parent"
        assert task["result"]["questions"] == ["是否兼容旧 token？"]
        child_session = task["child_session_id"]

        second = await pool.execute(
            ToolCall(
                id="task-second",
                name="task",
                arguments={
                    "task_id": task["id"],
                    "prompt": "兼容旧 token 读取，但新 token 使用 SHA-256",
                },
            ),
            parent_session_id=parent,
            parent_run_id="parent-2",
        )
        assert second.success, second.error
        second_payload = json.loads(second.output)
        assert second_payload["task"]["status"] == "completed"
        assert second_payload["task"]["child_session_id"] == child_session
        assert len(store.list_agent_task_runs(task["id"])) == 2
        messages = store.list_agent_task_messages(task["id"])
        assert [item["kind"] for item in messages] == [
            "instruction",
            "question",
            "instruction",
            "result",
        ]
        assert factory.calls[0].session_id == factory.calls[1].session_id == child_session
        assert "父 Agent 本轮消息" in factory.calls[1].prompt
    finally:
        await pool.shutdown()
        store.close()


@pytest.mark.asyncio
async def test_background_task_result_enters_persistent_parent_inbox(tmp_path: Path) -> None:
    config = AppConfig.model_validate({"subagents": {"allow_worktree_writes": False}})
    store = SQLiteSessionStore(tmp_path / "state.db")
    parent = store.create_session(tmp_path)
    factory = ConversationalRunnerFactory()
    pool = BackgroundAgentPool(
        config=config,
        workspace=tmp_path,
        store=store,
        event_bus=EventBus([store]),
        execution_target=NoExecutionTarget(),
        specs=[AgentSpec(name="reviewer", description="test", instructions="review")],
        runner_factory=factory,
    )
    try:
        started = await pool.execute(
            ToolCall(
                id="task-bg",
                name="task",
                arguments={
                    "agent": "reviewer",
                    "prompt": "后台检查",
                    "execution": "background",
                },
            ),
            parent_session_id=parent,
            parent_run_id="parent-bg",
        )
        task_id = json.loads(started.output)["task"]["id"]
        await pool.execute(
            ToolCall(
                id="wait-bg",
                name="await_agents",
                arguments={"task_ids": [task_id], "timeout_seconds": 5},
            ),
            parent_session_id=parent,
            parent_run_id="parent-bg",
        )
        inbox = pool.collect_inbox(parent)
        assert len(inbox) == 1
        assert inbox[0]["task_id"] == task_id
        assert inbox[0]["kind"] == "question"
    finally:
        await pool.shutdown()
        store.close()
