import json
import subprocess
from pathlib import Path

import pytest

from bot.config.models import AppConfig
from bot.core.events import EventBus
from bot.core.models import RunResult, ToolCall
from bot.execution import LocalExecutionTarget
from bot.sessions import SQLiteSessionStore
from bot.subagents import AgentSpec, BackgroundAgentPool, WorkerIsolation


class WorktreeWritingRunnerFactory:
    def __call__(self, spec, workspace: Path, approval_handler):
        class Runner:
            async def run(self, request):
                (workspace / "agent-change.txt").write_text("isolated\n", encoding="utf-8")
                return RunResult(
                    session_id=request.session_id,
                    status="completed",
                    final_text="已在独立 worktree 写入并检查文件。",
                )

        return Runner()


@pytest.fixture
def git_workspace(tmp_path: Path) -> tuple[Path, str]:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)
    subprocess.run(["git", "tag", "agent-base"], cwd=tmp_path, check=True)
    (tmp_path / "later.txt").write_text("later\n", encoding="utf-8")
    subprocess.run(["git", "add", "later.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "later"], cwd=tmp_path, check=True)
    base_commit = subprocess.run(
        ["git", "rev-parse", "agent-base"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return tmp_path, base_commit


@pytest.mark.asyncio
async def test_coder_writes_only_to_detached_worktree(
    git_workspace: tuple[Path, str],
) -> None:
    workspace, base_commit = git_workspace
    config = AppConfig.model_validate(
        {
            "subagents": {
                "max_concurrent": 1,
                "allow_worktree_writes": True,
                "shutdown_grace_seconds": 1,
            }
        }
    )
    store = SQLiteSessionStore(workspace / ".bot" / "state.db")
    parent_session = store.create_session(workspace)
    pool = BackgroundAgentPool(
        config=config,
        workspace=workspace,
        store=store,
        event_bus=EventBus([store]),
        execution_target=LocalExecutionTarget(),
        specs=[
            AgentSpec(
                name="coder",
                description="isolated writer",
                instructions="write one file",
                isolation=WorkerIsolation.WORKTREE,
            )
        ],
        runner_factory=WorktreeWritingRunnerFactory(),
    )
    try:
        spawned = await pool.execute(
            ToolCall(
                id="spawn-coder",
                name="spawn_agent",
                arguments={
                    "agent": "coder",
                    "objective": "写入隔离文件",
                    "base_ref": "agent-base",
                },
            ),
            parent_session_id=parent_session,
            parent_run_id="parent-run",
        )
        assert spawned.success, spawned.error
        task_id = json.loads(spawned.output)["task"]["id"]

        awaited = await pool.execute(
            ToolCall(
                id="await-coder",
                name="await_agents",
                arguments={"task_ids": [task_id], "timeout_seconds": 5},
            ),
            parent_session_id=parent_session,
            parent_run_id="parent-run",
        )
        assert awaited.success, awaited.error
        result = json.loads(awaited.output)["tasks"][0]["result"]

        assert not (workspace / "agent-change.txt").exists()
        worktree = Path(result["worktree_path"])
        assert worktree.is_relative_to(workspace / ".bot" / "agent-worktrees")
        assert (worktree / "agent-change.txt").read_text(encoding="utf-8") == "isolated\n"
        assert not (worktree / "later.txt").exists()
        assert result["files_changed"] == ["agent-change.txt"]
        assert result["evidence_refs"]
        assert result["artifacts"][0]["type"] == "patch"
        assert result["artifacts"][0]["base_commit"] == base_commit
        diff_blob = store.read_context_blob(parent_session, result["evidence_refs"][-1])
        assert diff_blob is not None
        assert "+isolated" in diff_blob["content"]

        refused_cleanup = await pool.execute(
            ToolCall(
                id="cleanup-coder-safe",
                name="cleanup_agent_worktree",
                arguments={"task_id": task_id},
            ),
            parent_session_id=parent_session,
            parent_run_id="parent-run",
        )
        assert not refused_cleanup.success
        assert "仍有改动" in (refused_cleanup.error or "")

        adopted = await pool.execute(
            ToolCall(
                id="apply-coder-patch",
                name="apply_agent_patch",
                arguments={"task_id": task_id},
            ),
            parent_session_id=parent_session,
            parent_run_id="parent-run",
        )
        assert adopted.success, adopted.error
        adopted_payload = json.loads(adopted.output)
        assert adopted_payload["worktree_cleaned"] is True
        assert adopted_payload["cleanup_error"] is None
        assert (workspace / "agent-change.txt").read_text(encoding="utf-8") == "isolated\n"
        assert not worktree.exists()
    finally:
        await pool.shutdown()
        store.close()
