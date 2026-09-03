import asyncio
from pathlib import Path

import pytest

from bot.cli.runtime import build_runtime
from bot.core.events import EventType
from bot.core.models import RunResult


@pytest.mark.asyncio
async def test_background_result_auto_resumes_idle_parent_when_opted_in(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text("BOT_MODEL_API_KEY=test-key\n", encoding="utf-8")
    runtime = build_runtime(
        workspace=tmp_path,
        config_overrides={
            "model": {"base_url": "https://unused", "name": "mock"},
            "agents": {"auto_resume_background": True},
            "memory": {"enabled": False},
            "storage": {"state_path": str(tmp_path / ".bot" / "state.db")},
        },
    )
    parent_session_id = runtime.store.create_session(tmp_path)
    spec = runtime.agent_catalog.agents["explorer"]
    task = runtime.store.create_agent_task(
        task_id="background-auto-resume",
        parent_session_id=parent_session_id,
        parent_run_id="parent-run",
        agent_name=spec.name,
        objective="检查后台结果",
        constraints=[],
        acceptance_criteria=[],
        spec=spec.model_dump(mode="json"),
        context_refs=[],
        required=False,
        execution="background",
        base_ref="HEAD",
        isolation="read_only",
        idempotency_key="background-auto-resume",
    )
    runtime.store.append_agent_task_message(
        task_id=task["id"],
        direction="child_to_parent",
        kind="result",
        payload={"status": "completed", "summary": "后台结果"},
    )
    resumed = asyncio.Event()
    release = asyncio.Event()
    requests = []

    async def fake_run(request):
        requests.append(request)
        resumed.set()
        await release.wait()
        return RunResult(session_id=parent_session_id, status="completed")

    runtime.runner.run = fake_run
    await runtime.runner.event_bus.emit(
        EventType.SUBAGENT_COMPLETED,
        session_id=parent_session_id,
        run_id=f"subagent:{task['id']}",
        payload={"task_id": task["id"]},
    )
    await asyncio.wait_for(resumed.wait(), timeout=1)

    assert requests[0].session_id == parent_session_id
    assert "mailbox" in requests[0].prompt
    release.set()
    await runtime.aclose()
