import asyncio
from pathlib import Path

import pytest
from test_agent_loop import (
    DestructiveTestTool,
    ScriptedProvider,
    make_test_runner,
    tool_turn,
)

from bot.core import RunRequest
from bot.core.models import ModelEvent, ModelEventKind, Role
from bot.tools.builtins import ReadFileTool

REPEATED = (Path(__file__).parents[1] / "fixtures/reliability/512k-step111-prefix.txt").read_text()
CFG = {
    "stream_guard": {"mode": "enforce"},
    "recovery": {"enabled": True},
    "finalization": {"enabled": False},
}


def repetition():
    return [
        ModelEvent(kind=ModelEventKind.TEXT_DELTA, text=REPEATED),
        ModelEvent(kind=ModelEventKind.FINISH, finish_reason="length"),
    ]


def answer():
    return [
        ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="done"),
        ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ['{"path":', "{}"])
@pytest.mark.parametrize("mixed", [False, True])
async def test_all_length_tool_buffers_quarantined_before_parsing(tmp_path, arguments, mixed):
    tool = DestructiveTestTool()
    first = tool_turn("broken", tool.name, arguments)
    if mixed:
        first.insert(
            0,
            ModelEvent(
                kind=ModelEventKind.TOOL_CALL_DELTA,
                tool_index=1,
                tool_call_id="apparently-valid",
                tool_name=tool.name,
                arguments_delta="{}",
            ),
        )
    first[-1] = ModelEvent(kind=ModelEventKind.FINISH, finish_reason="length")
    provider = ScriptedProvider([first, tool_turn("recovered", tool.name, "{}"), answer()])
    runner, store = make_test_runner(
        tmp_path,
        provider,
        tools=[tool],
        agent_config=CFG,
        model_config={"max_output_tokens": 32768},
    )
    from bot.core.approval import AllowApprovalHandler

    runner.approval_handler = AllowApprovalHandler()
    try:
        result = await runner.run(RunRequest(prompt="perform task"))
        assert result.status == "completed", result.error
        assert tool.executions == 1
        history = store.load_messages(result.session_id)
        assert not any(
            c.id in {"broken", "apparently-valid"} for m in history for c in m.tool_calls
        )
        assert provider.requests[1].tool_choice != "none"
        assert provider.requests[1].max_output_tokens == 8192
        assert provider.requests[0].messages[0] == provider.requests[1].messages[0]
        assert provider.requests[0].tools == provider.requests[1].tools
        controls = [
            m
            for m in history
            if m.name == "runtime_context" and "model_response_recovery" in (m.content or "")
        ]
        assert len(controls) == 1 and controls[0].role == Role.USER
    finally:
        store.close()


@pytest.mark.asyncio
async def test_repetition_closes_stream_then_recovers_and_stops_second_loop(tmp_path):
    class ClosingProvider(ScriptedProvider):
        closed = 0

        async def stream(self, request):
            try:
                async for event in super().stream(request):
                    yield event
            finally:
                self.closed += 1

    provider = ClosingProvider([repetition(), repetition()])
    runner, store = make_test_runner(tmp_path, provider, agent_config=CFG)
    try:
        result = await runner.run(RunRequest(prompt="investigate"))
        assert result.status == "blocked"
        assert result.termination_reason == "stream_repetition_after_recovery"
        assert provider.closed == len(provider.requests) == 2
        events = store.list_events(result.session_id)
        interrupted = [e["payload"] for e in events if e["type"] == "model.request.interrupted"]
        assert len(interrupted) == 2
        assert all(
            e["content_chars"] == 14156 and e["usage_status"] == "missing" for e in interrupted
        )
        assert all(
            e["provider_finish_reason"] is None and e["stream_close_status"] == "closed"
            for e in interrupted
        )
        assert not any(
            m.role == Role.ASSISTANT and (m.content or "").startswith("Hmm, rows")
            for m in store.load_messages(result.session_id)
        )
        # A fresh Run in the same session does not renew the episode allowance.
        provider.turns = [repetition()]
        resumed = await runner.run(RunRequest(prompt="continue", session_id=result.session_id))
        assert resumed.status == "blocked" and len(provider.requests) == 3
    finally:
        store.close()


@pytest.mark.asyncio
async def test_new_evidence_allows_second_episode_but_not_third(tmp_path):
    (tmp_path / "input.txt").write_text("diagnostic evidence")
    provider = ScriptedProvider(
        [
            repetition(),
            tool_turn("read-a", "read_file", '{"path":"input.txt"}'),
            repetition(),
            repetition(),
        ]
    )
    runner, store = make_test_runner(tmp_path, provider, tools=[ReadFileTool()], agent_config=CFG)
    try:
        result = await runner.run(RunRequest(prompt="inspect file"))
        assert result.status == "blocked"
        starts = [
            e
            for e in store.list_events(result.session_id)
            if e["type"] == "run.recovery_started" and e["payload"].get("kind") == "model_response"
        ]
        assert len(starts) == 2
        assert starts[-1]["payload"]["task_attempts"] == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_recovery_deadline_covers_tools_and_prevents_finalizer(tmp_path):
    class SlowProvider(ScriptedProvider):
        async def stream(self, request):
            if self.requests:
                self.requests.append(request)
                await asyncio.sleep(2)
            else:
                async for event in super().stream(request):
                    yield event

    provider = SlowProvider([repetition()])
    runner, store = make_test_runner(
        tmp_path,
        provider,
        agent_config={
            **CFG,
            "recovery": {"enabled": True, "max_episode_seconds": 0.05},
            "finalization": {"enabled": True},
        },
    )
    try:
        result = await runner.run(RunRequest(prompt="inspect"))
        assert result.termination_reason == "recovery_timeout"
        assert len(provider.requests) == 2
        provider.turns = [answer()]
        resumed = await runner.run(RunRequest(prompt="resume", session_id=result.session_id))
        assert resumed.termination_reason == "recovery_timeout"
        assert len(provider.requests) == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_missing_usage_keeps_fee_reservation_across_resume(tmp_path):
    class EstimatedProvider(ScriptedProvider):
        def count_tokens(self, request):
            return 100

    provider = EstimatedProvider([repetition(), answer()])
    runner, store = make_test_runner(
        tmp_path,
        provider,
        agent_config={**CFG, "max_cost_usd": 0.033},
        model_config={
            "max_output_tokens": 32768,
            "input_cost_per_million": 1,
            "output_cost_per_million": 1,
        },
    )
    try:
        result = await runner.run(RunRequest(prompt="investigate"))
        assert result.termination_reason == "max_cost_usd"
        assert len(provider.requests) == 1
        scope = runner._recovery_scope
        state = store.begin_recovery_task(
            result.session_id, scope, new_task=False, wall_seconds=None
        )
        assert len(state["pending_cost"]) == 1 and state["charged_cost"] == 0
        assert sum(state["pending_cost"].values()) == pytest.approx(0.032868)
        resumed = await runner.run(RunRequest(prompt="continue", session_id=result.session_id))
        assert resumed.termination_reason == "max_cost_usd"
        assert len(provider.requests) == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_observe_mode_records_without_interrupting(tmp_path):
    provider = ScriptedProvider(
        [
            [
                ModelEvent(kind=ModelEventKind.TEXT_DELTA, text=REPEATED),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop"),
            ]
        ]
    )
    runner, store = make_test_runner(tmp_path, provider)
    try:
        result = await runner.run(RunRequest(prompt="observe"))
        assert result.status == "completed" and result.final_text == REPEATED
        events = store.list_events(result.session_id)
        assert sum(e["type"] == "model.stream_repetition" for e in events) == 1
        assert not any(e["type"] == "model.request.interrupted" for e in events)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_finalizer_repetition_falls_back_without_recovery(tmp_path):
    provider = ScriptedProvider(
        [tool_turn("read", "read_file", '{"path":"file.txt"}'), repetition()]
    )
    (tmp_path / "file.txt").write_text("evidence")
    runner, store = make_test_runner(
        tmp_path,
        provider,
        tools=[ReadFileTool()],
        agent_config={
            **CFG,
            "max_steps": 1,
            "finalization": {"enabled": True},
        },
    )
    try:
        result = await runner.run(RunRequest(prompt="inspect"))
        assert result.status == "limit_reached"
        assert "任务尚未完整完成" in result.final_text
        assert len(provider.requests) == 2 and provider.requests[-1].tool_choice == "none"
        assert not any(
            e["type"] == "run.recovery_started" for e in store.list_events(result.session_id)
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_failed_process_cleanup_blocks_remaining_batch(tmp_path):
    from bot.execution import ProcessStatus
    from bot.tools import ToolResult

    class FailedCleanup(DestructiveTestTool):
        name = "test_cleanup"

        async def execute(self, context, arguments):
            return ToolResult(
                success=False,
                error="cleanup incomplete",
                metadata={"process_status": ProcessStatus.CLEANUP_FAILED},
            )

    tool = DestructiveTestTool()
    batch = tool_turn("cleanup", "test_cleanup", "{}")
    batch.insert(
        1,
        ModelEvent(
            kind=ModelEventKind.TOOL_CALL_DELTA,
            tool_index=1,
            tool_call_id="write",
            tool_name=tool.name,
            arguments_delta="{}",
        ),
    )
    provider = ScriptedProvider([batch])
    runner, store = make_test_runner(tmp_path, provider, tools=[FailedCleanup(), tool])
    from bot.core.approval import AllowApprovalHandler

    runner.approval_handler = AllowApprovalHandler()
    try:
        result = await runner.run(RunRequest(prompt="cleanup"))
        assert result.termination_reason == "process_cleanup_incomplete"
        assert result.status == "blocked" and tool.executions == 0
        results = [m for m in store.load_messages(result.session_id) if m.role == Role.TOOL]
        assert {m.tool_call_id for m in results} == {"cleanup", "write"}
    finally:
        store.close()


@pytest.mark.asyncio
async def test_split_raw_usage_is_settled_once(tmp_path):
    provider = ScriptedProvider(
        [
            [
                ModelEvent(
                    kind=ModelEventKind.USAGE,
                    provider_metadata={"raw_usage": {"prompt_tokens": 100}},
                ),
                ModelEvent(kind=ModelEventKind.USAGE, output_tokens=20),
                ModelEvent(kind=ModelEventKind.USAGE, input_tokens=100, output_tokens=20),
                *answer(),
            ]
        ]
    )
    runner, store = make_test_runner(
        tmp_path,
        provider,
        agent_config={"max_cost_usd": 1},
        model_config={"input_cost_per_million": 1, "output_cost_per_million": 1},
    )
    try:
        result = await runner.run(RunRequest(prompt="account usage"))
        assert result.status == "completed"
        assert result.input_tokens == 100 and result.output_tokens == 20
        state = store.begin_recovery_task(
            result.session_id, runner._recovery_scope, new_task=False, wall_seconds=None
        )
        assert state["pending_cost"] == {}
        assert state["charged_cost"] == pytest.approx(0.00012)
    finally:
        store.close()
