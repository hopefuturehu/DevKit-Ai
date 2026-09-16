import asyncio
import importlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from test_agent_loop import ScriptedProvider, SteerableProvider, make_test_runner

from bot.cli.app import _interactive_loop, _run, _run_with_steering, _with_runtime_shutdown
from bot.cli.render import InteractiveApprovalHandler
from bot.cli.runtime import Runtime
from bot.core.events import CallbackEventSink, EventType
from bot.core.models import ModelEvent, ModelEventKind, RunRequest
from bot.execution import ProcessStatus
from bot.tools import Tool, ToolAnnotations
from bot.tools.builtins import RunCommandTool


class NoSubagents:
    has_live_tasks = False

    async def shutdown(self):
        pass


class ReadyPrompt(PromptSession):
    def __init__(self, pipe):
        super().__init__(input=pipe, output=DummyOutput())
        self.ready = asyncio.Event()
        self.approval_ready = asyncio.Event()

    async def prompt_async(self, message, **kwargs):
        def ready():
            self.ready.set()
            if message.startswith("[approve:"):
                self.approval_ready.set()

        return await super().prompt_async(message, pre_run=ready, **kwargs)


class BlockingTool(Tool):
    name = "wait_for_interrupt"
    description = "Wait without external effects"
    annotations = ToolAnnotations(read_only=True)
    input_schema = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(self, *, approval=False):
        self.started = asyncio.Event()
        self.annotations = ToolAnnotations(read_only=True, destructive=approval)

    async def execute(self, context, arguments):
        self.started.set()
        await asyncio.Event().wait()


def make_runtime(path, provider, tools=()):
    runner, store = make_test_runner(path, provider, tools=tools)
    runner.config.subagents.enabled = False
    approvals = InteractiveApprovalHandler(Console(quiet=True))
    runner.approval_handler = approvals
    return Runtime(
        config=runner.config,
        workspace=path,
        catalog=None,
        skills=None,
        tools=None,
        target=runner.execution_target,
        context=None,
        store=store,
        compactor=None,
        runner=runner,
        subagents=NoSubagents(),
        approval_handler=approvals,
    )


def tool_provider(tool, arguments=None):
    return ScriptedProvider(
        [
            [
                ModelEvent(
                    kind=ModelEventKind.TOOL_CALL_DELTA,
                    tool_index=0,
                    tool_call_id="wait-1",
                    tool_name=tool.name,
                    arguments_delta=json.dumps(arguments or {}),
                ),
                ModelEvent(kind=ModelEventKind.FINISH, finish_reason="tool_calls"),
            ]
        ]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["model", "tool", "approval", "process"])
async def test_ctrl_c_cancels_and_persists_before_store_closes(tmp_path, phase):
    tool = RunCommandTool() if phase == "process" else BlockingTool(approval=phase == "approval")
    arguments = (
        {"argv": [sys.executable, "-c", "import time; print('ready',flush=True); time.sleep(20)"]}
        if phase == "process"
        else None
    )
    provider = SteerableProvider() if phase == "model" else tool_provider(tool, arguments)
    runtime = make_runtime(tmp_path, provider, [tool])
    runtime.config.permissions.auto_approve = phase == "process"
    process_ready = asyncio.Event()

    async def output(event):
        if event.type == EventType.TOOL_OUTPUT and "ready" in event.payload.get("data", ""):
            process_ready.set()

    runtime.runner.event_bus.add_sink(CallbackEventSink(output))
    session = runtime.store.create_session(tmp_path)
    with create_pipe_input() as pipe:
        prompt = ReadyPrompt(pipe)

        async def interrupt():
            await prompt.ready.wait()
            if phase == "model":
                await provider.started.wait()
            elif phase == "tool":
                await tool.started.wait()
            elif phase == "process":
                await process_ready.wait()
            else:
                await prompt.approval_ready.wait()
            pipe.send_text("\x03")

        async def exercise():
            driver = asyncio.create_task(interrupt())
            try:
                result = await _run_with_steering(
                    runtime, prompt, RunRequest(prompt="wait", session_id=session)
                )
                assert result.status == "cancelled"
                assert not runtime._closed
                assert not runtime.runner.is_session_running(session)
                if phase == "process":
                    processes = await runtime.target.list_processes()
                    assert len(processes) == 1
                    assert processes[0].status == ProcessStatus.CANCELLED
                    assert processes[0].cleanup_status == "observed_empty"
                    assert not processes[0].remaining_processes
                if phase == "model":
                    # The same interactive session remains usable after Ctrl+C.
                    next_result = await _run_with_steering(
                        runtime, prompt, RunRequest(prompt="continue", session_id=session)
                    )
                    assert next_result.status == "completed"
            finally:
                driver.cancel()
                await asyncio.gather(driver, return_exceptions=True)

        async with asyncio.timeout(5):
            await _with_runtime_shutdown(runtime, exercise())

    assert runtime._closed
    with sqlite3.connect(tmp_path / "state.db") as db:
        assert db.execute("SELECT status FROM runs ORDER BY rowid").fetchone() == ("cancelled",)
        if phase in {"tool", "process"}:
            assert db.execute("SELECT status FROM tool_runs").fetchall() == [("cancelled",)]
            payload = db.execute(
                "SELECT payload_json FROM events WHERE type=?", (EventType.TOOL_COMPLETED.value,)
            ).fetchone()
            assert json.loads(payload[0])["status"] == "cancelled"
        if phase == "approval":
            assert not tool.started.is_set()
            assert db.execute("SELECT decision FROM approvals").fetchall() == [("deny",)]
            payload = db.execute(
                "SELECT payload_json FROM events WHERE type=?", (EventType.APPROVAL_RESOLVED.value,)
            ).fetchone()
            assert json.loads(payload[0])["cancelled"]


@pytest.mark.asyncio
@pytest.mark.parametrize("keys,exit_code", [("\x03", 130), ("\x04", 0), ("/exit\n", 0)])
async def test_idle_exit_closes_runtime(tmp_path, monkeypatch, keys, exit_code):
    runtime = make_runtime(tmp_path, SteerableProvider())
    session = runtime.store.create_session(tmp_path)
    with create_pipe_input() as pipe:
        prompt = ReadyPrompt(pipe)
        monkeypatch.setattr(importlib.import_module("bot.cli.app"), "PromptSession", lambda: prompt)

        async def interrupt():
            await prompt.ready.wait()
            pipe.send_text(keys)

        driver = asyncio.create_task(interrupt())
        try:
            async with asyncio.timeout(5):
                if exit_code:
                    with pytest.raises(typer.Exit) as exc:
                        await _with_runtime_shutdown(runtime, _interactive_loop(runtime, session))
                    assert exc.value.exit_code == exit_code
                else:
                    await _with_runtime_shutdown(runtime, _interactive_loop(runtime, session))
        finally:
            driver.cancel()
            await asyncio.gather(driver, return_exceptions=True)
    assert runtime._closed


@pytest.mark.asyncio
async def test_outer_cancellation_waits_for_runner_cleanup(tmp_path):
    provider = SteerableProvider()
    runtime = make_runtime(tmp_path, provider)
    session = runtime.store.create_session(tmp_path)
    cleaning = asyncio.Event()
    release = asyncio.Event()
    original_cleanup = runtime.runner._cleanup_owned_processes

    async def cleanup(*args):
        cleaning.set()
        await release.wait()
        return await original_cleanup(*args)

    runtime.runner._cleanup_owned_processes = cleanup
    with create_pipe_input() as pipe:
        prompt = ReadyPrompt(pipe)
        task = asyncio.create_task(
            _with_runtime_shutdown(
                runtime,
                _run_with_steering(runtime, prompt, RunRequest(prompt="wait", session_id=session)),
            )
        )
        try:
            async with asyncio.timeout(5):
                await provider.started.wait()
                task.cancel()
                await cleaning.wait()
                task.cancel()  # Repeated cancellation must not hit the Runner again.
                assert not runtime._closed
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert runtime._closed
    with sqlite3.connect(tmp_path / "state.db") as db:
        assert db.execute("SELECT status FROM runs").fetchall() == [("cancelled",)]


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
@pytest.mark.parametrize("signal_name", ["SIGINT", "SIGTERM", "SIGHUP"])
@pytest.mark.parametrize("interactive", [False, True])
def test_real_signals_preserve_exit_code_and_finish_cleanup(tmp_path, signal_name, interactive):
    result = subprocess.run(
        [sys.executable, __file__, str(tmp_path), signal_name, str(int(interactive))],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 128 + getattr(signal, signal_name), result.stdout + result.stderr
    assert "CLEANUP_FINISHED" in result.stdout
    assert not result.stderr
    with sqlite3.connect(tmp_path / "state.db") as db:
        assert db.execute("SELECT status FROM runs").fetchall() == [("cancelled",)]


def signal_scenario(path, signal_name, interactive):
    provider = SteerableProvider()
    runtime = make_runtime(path, provider)
    original_close = runtime.aclose

    async def close():
        # Neither SIGINT nor another termination signal may interrupt shutdown.
        os.kill(os.getpid(), signal.SIGINT)
        os.kill(os.getpid(), getattr(signal, signal_name))
        await asyncio.sleep(0.01)
        await original_close()
        print("CLEANUP_FINISHED", flush=True)

    runtime.aclose = close

    async def main(prompt):
        async def interrupt():
            await provider.started.wait()
            os.kill(os.getpid(), getattr(signal, signal_name))

        driver = asyncio.create_task(interrupt())
        try:
            request = RunRequest(prompt="wait", session_id=runtime.store.create_session(path))
            work = (
                _run_with_steering(runtime, prompt, request)
                if interactive
                else runtime.runner.run(request)
            )
            return await _with_runtime_shutdown(runtime, work)
        finally:
            await driver

    try:
        with create_pipe_input() as pipe:
            result = _run(main(ReadyPrompt(pipe)))
    except typer.Exit as exc:
        return exc.exit_code
    return 130 if result.status == "cancelled" else 0


if __name__ == "__main__":
    raise SystemExit(signal_scenario(Path(sys.argv[1]), sys.argv[2], bool(int(sys.argv[3]))))
