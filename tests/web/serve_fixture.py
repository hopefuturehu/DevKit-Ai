"""Browser test server: production Web/Runner/tools, a local deterministic provider."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import uvicorn

import bot.cli.runtime as runtime_module
from bot.core.events import AgentEvent, EventType
from bot.core.models import ModelCapabilities, ModelEvent, ModelEventKind, Role
from bot.web.server import create_app


class BrowserProvider:
    def __init__(self, **kwargs):
        pass

    def capabilities(self, model):
        return ModelCapabilities()

    async def stream(self, request):
        users = [
            m.content
            for m in request.messages
            if m.role == Role.USER and m.content and not m.content.startswith("用户在当前运行期间")
        ]
        long = any("长任务" in text for text in users)
        handled = any(m.role == Role.TOOL and m.name == "run_command" for m in request.messages)
        if handled:
            if long:
                await asyncio.sleep(60)
            yield ModelEvent(
                kind=ModelEventKind.TEXT_DELTA,
                text=(
                    "任务已完成。\n\n- 已生成 `result.txt`\n- 已完成验证\n\n"
                    "<script>window.injected=true</script>"
                ),
            )
            yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")
            return
        yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="我先检查配置，然后执行验证。")
        yield ModelEvent(kind=ModelEventKind.REASONING_DELTA, text="提供方返回的测试记录。")
        yield ModelEvent(kind=ModelEventKind.USAGE, input_tokens=8240, output_tokens=960)
        delay = 30 if long else 0.02
        code = (
            "from pathlib import Path;import time;"
            "Path('result.txt').write_text('verified output\\n');"
            "print('starting verification',flush=True);"
            "time.sleep(0.6);print('verification in progress',flush=True);"
            f"time.sleep({delay});"
            "print('3 passed; <script>window.injected=true</script>',flush=True)"
        )
        calls = [
            (
                "plan",
                "update_plan",
                {
                    "items": [
                        {"content": "检查配置", "status": "completed"},
                        {"content": "执行验证", "status": "in_progress"},
                        {"content": "汇总结果", "status": "pending"},
                    ]
                },
            ),
            ("read", "read_file", {"path": "config.py"}),
            (
                "command",
                "run_command",
                {"argv": [sys.executable, "-c", code], "wait_seconds": 0.1 if long else 10},
            ),
        ]
        for index, (call_id, name, args) in enumerate(calls):
            yield ModelEvent(
                kind=ModelEventKind.TOOL_CALL_DELTA,
                tool_index=index,
                tool_call_id=call_id,
                tool_name=name,
                arguments_delta=json.dumps(args),
            )
        yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="tool_calls")


async def seed(service):
    session = service.runtime.store.create_session(service.runtime.workspace)
    response = await service.start(session, "检查配置并执行验证", [], "seed")
    await service.tasks[session][1]
    run = response["run_id"]
    await service.runtime.runner.event_bus.emit(
        EventType.PLAN_UPDATED,
        session_id=session,
        run_id=run,
        payload={
            "items": [
                {"content": "检查配置", "status": "completed"},
                {"content": "执行验证", "status": "completed"},
                {"content": "汇总结果", "status": "completed"},
            ]
        },
    )
    task = service.runtime.store.create_agent_task(
        task_id="browser-child",
        parent_session_id=session,
        parent_run_id=run,
        agent_name="researcher",
        objective="检查边界条件",
        constraints=[],
        acceptance_criteria=[],
        spec={},
        context_refs=[],
        required=False,
        execution="background",
        base_ref="HEAD",
        isolation="read_only",
        idempotency_key=None,
    )
    child = task["child_session_id"]
    child_run = uuid4().hex
    service.runtime.store.start_run(child, child_run)
    await service.runtime.runner.event_bus.emit(
        EventType.RUN_STARTED,
        session_id=child,
        run_id=child_run,
        payload={"prompt": "检查边界条件"},
    )
    await service.runtime.runner.event_bus.emit(
        EventType.ASSISTANT_MESSAGE,
        session_id=child,
        run_id=child_run,
        payload={"step": 1, "text": "边界条件已检查。"},
    )
    service.runtime.store.finish_run(child_run, "completed")
    await service.runtime.runner.event_bus.emit(
        EventType.RUN_FINISHED,
        session_id=child,
        run_id=child_run,
        payload={"status": "completed", "final_text": "边界条件已检查。"},
    )
    service.runtime.store.claim_agent_task(task["id"], owner_id="browser-fixture")
    service.runtime.store.finish_agent_task(
        task["id"], status="completed", result={"summary": "done"}, error=None
    )
    # Store a real compaction record with its source message range for inspection.
    end = service.runtime.store.latest_message_position(session)
    record = service.runtime.store.start_context_compaction(
        session_id=session,
        parent_id=None,
        trigger="browser-test",
        model="fixture",
        covered_start_position=1,
        covered_end_position=end,
        delta_start_position=1,
        source_sha256="fixture",
        anchor_positions=[1],
        source_chars=100,
    )
    service.runtime.store.complete_context_compaction(
        record,
        summary_text="已检查配置并完成验证。",
        summary_token_estimate=20,
        source_refs=[],
        input_tokens=100,
        output_tokens=20,
        duration_ms=12,
    )
    await seed_analysis_runs(service)
    return session


async def seed_analysis_runs(service):
    """Seed runs that exercise the analysis boundaries the console must explain.

    - ``analysis-completed``: 100s total with a 30s approval wait -> net 70s.
    - ``analysis-cancelled``: cancelled with an unpaired approval request.
    - ``analysis-open``: started but never finished -> duration unknown.
    """
    store = service.runtime.store
    bus = service.runtime.runner.event_bus
    workspace = service.runtime.workspace
    base = datetime(2026, 9, 18, 10, 0, 0, tzinfo=UTC)

    def stamp(offset):
        return (base + timedelta(seconds=offset)).isoformat()

    async def emit(kind, session, run, **payload):
        await bus.emit(kind, session_id=session, run_id=run, payload=payload)

    async def emit_at(kind, session, run, offset, **payload):
        """Emit with an explicit timestamp so it lands inside the backdated window."""
        event = AgentEvent(
            type=kind,
            session_id=session,
            run_id=run,
            timestamp=base + timedelta(seconds=offset),
            payload=payload,
        )
        await store.publish(event)

    async def backdate(run_id, started, completed):
        with store._lock, store._connection:
            store._connection.execute(
                "UPDATE runs SET started_at=?, completed_at=? WHERE id=?",
                (started, completed, run_id),
            )

    completed_session = store.create_session(workspace)
    store.start_run(completed_session, "analysis-completed")
    await emit_at(
        EventType.RUN_STARTED,
        completed_session,
        "analysis-completed",
        0,
        prompt="分析台验收：完成运行含审批等待",
    )
    await emit_at(
        EventType.APPROVAL_REQUESTED,
        completed_session,
        "analysis-completed",
        10,
        approval_id="analysis-ap-1",
        tool_call_id="analysis-t-1",
        name="run_command",
        reason="需要审批",
    )
    await emit_at(
        EventType.APPROVAL_RESOLVED,
        completed_session,
        "analysis-completed",
        40,
        approval_id="analysis-ap-1",
        tool_call_id="analysis-t-1",
        approved=True,
        scope="once",
    )
    await emit_at(
        EventType.RUN_FINISHED,
        completed_session,
        "analysis-completed",
        100,
        status="completed",
        termination_reason="completed",
    )
    store.finish_run("analysis-completed", "completed")
    await backdate("analysis-completed", stamp(0), stamp(100))

    cancelled_session = store.create_session(workspace)
    store.start_run(cancelled_session, "analysis-cancelled")
    await emit_at(
        EventType.RUN_STARTED,
        cancelled_session,
        "analysis-cancelled",
        0,
        prompt="分析台验收：取消运行含未配对审批",
    )
    await emit_at(
        EventType.APPROVAL_REQUESTED,
        cancelled_session,
        "analysis-cancelled",
        20,
        approval_id="analysis-ap-2",
        tool_call_id="analysis-t-2",
        name="apply_patch",
        reason="需要审批",
    )
    await emit_at(
        EventType.RUN_CANCELLED,
        cancelled_session,
        "analysis-cancelled",
        600,
        termination_reason="cancelled",
        error="运行已取消",
    )
    store.finish_run("analysis-cancelled", "cancelled")
    await backdate("analysis-cancelled", stamp(0), stamp(600))

    open_session = store.create_session(workspace)
    store.start_run(open_session, "analysis-open")
    await emit_at(
        EventType.RUN_STARTED,
        open_session,
        "analysis-open",
        0,
        prompt="分析台验收：缺少结束时间",
    )


def main():
    os.environ["BOT_MODEL_API_KEY"] = "browser-test-no-network"
    runtime_module.OpenAICompatibleProvider = BrowserProvider
    with tempfile.TemporaryDirectory(prefix="bot-web-browser-") as directory:
        workspace = Path(directory)
        (workspace / "config.py").write_text("retries = 3\ntimeout = 10\n")
        (workspace / ".bot").mkdir()
        config = workspace / ".bot/config.toml"
        config.write_text(
            '[model]\nname="browser-fixture"\napi_key_ref="env:BOT_MODEL_API_KEY"\n'
            '[storage]\nstate_path="./.bot/state.db"\n[memory]\nenabled=false\n'
            "[permissions]\nauto_approve=true\n[subagents]\nenabled=false\n"
        )
        app = create_app(workspace, config)
        original = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(app):
            async with original(app):
                await asyncio.wait_for(seed(app.state.workbench), timeout=15)
                yield

        app.router.lifespan_context = lifespan

        @app.post("/__test__/approval/{enabled}")
        async def approval(enabled: bool):
            app.state.workbench.runtime.config.permissions.auto_approve = not enabled
            return {"ok": True}

        uvicorn.run(
            app,
            host="127.0.0.1",
            port=int(os.environ.get("BOT_WEB_TEST_PORT", "8874")),
            log_level="warning",
        )


if __name__ == "__main__":
    main()
