import asyncio
import fcntl
import importlib
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time
from io import StringIO

import pytest
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from test_agent_loop import SteerableProvider
from test_cli_interrupts import make_runtime

from bot.cli.app import _run_with_steering, _with_runtime_shutdown
from bot.cli.presentation import InteractiveEventSink
from bot.cli.render import create_cli_console
from bot.cli.terminal import TerminalUI
from bot.core.models import RunRequest


@pytest.mark.asyncio
async def test_running_commands_never_enter_steering_and_enhanced_input_cancels(
    tmp_path, monkeypatch
):
    provider = SteerableProvider()
    runtime = make_runtime(tmp_path, provider)
    runtime.config.display.history = False
    runtime.cli_ui = ui = TerminalUI(enhanced=True)
    ui.runtime = runtime
    stream = StringIO()
    console = create_cli_console(file=stream)
    monkeypatch.setattr(importlib.import_module("bot.cli.app"), "console", console)
    ui.sink = InteractiveEventSink(console, ui)
    runtime.runner.event_bus.add_sink(ui.sink)
    session_id = runtime.store.create_session(tmp_path)
    received = []
    original_steer = runtime.runner.steer

    async def capture(session, text):
        received.append(text)
        return await original_steer(session, text)

    monkeypatch.setattr(runtime.runner, "steer", capture)
    with create_pipe_input() as pipe:
        prompt = ui.create_prompt(runtime, input=pipe, output=DummyOutput())

        async def driver():
            await provider.started.wait()
            pipe.send_text("/status\r/model\r/permissions full-access\r/typo\r补充要求\r/cancel\r")

        async def exercise():
            task = asyncio.create_task(driver())
            try:
                result = await _run_with_steering(
                    runtime,
                    prompt,
                    RunRequest(prompt="wait", session_id=session_id),
                )
                assert result.status == "cancelled"
                assert received == ["补充要求"]
                assert runtime.config.permissions.mode == "safe"
                assert ui.state.phase == "已取消"
                assert "会话累计用量" in stream.getvalue()
                assert "未知命令" in stream.getvalue()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        async with asyncio.timeout(8):
            await _with_runtime_shutdown(runtime, exercise())


@pytest.mark.parametrize("mode", ["terminal", "plain"])
def test_real_pty_menu_resize_and_clean_exit(tmp_path, mode):
    """No provider calls: exercise the installed CLI and an actual terminal FD."""
    config = tmp_path / "config.toml"
    config.write_text(
        '[model]\nname="test-model"\napi_key="fake-cli-test-key"\n'
        'base_url="http://127.0.0.1:1/v1"\n'
        f'[storage]\nstate_path="{tmp_path / "state.db"}"\n'
        "[display]\nhistory=false\n[subagents]\nenabled=false\n",
    )
    config.chmod(0o600)
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 120, 0, 0))
    process = subprocess.Popen(
        [sys.executable, "-m", "bot", "-C", str(tmp_path), "--config", str(config), "--ui", mode],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env={**os.environ, "TERM": "xterm-256color", "PROMPT_TOOLKIT_NO_CPR": "1"},
    )
    os.close(slave)
    output = bytearray()

    def until(text):
        deadline = time.monotonic() + 8
        while text.encode() not in output:
            assert time.monotonic() < deadline, output.decode(errors="replace")[-3000:]
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b""
                assert chunk, output.decode(errors="replace")[-3000:]
                output.extend(chunk)

    try:
        until("/help 命令")
        if mode == "terminal":
            until("就绪")
        # Tab completion must insert /status without a provider request.
        os.write(master, b"/stat\t\r" if mode == "terminal" else b"/status\r")
        until("会话累计用量")
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 55, 0, 0))
        os.write(master, b"/help\r")
        until("/compact rollback")
        os.write(master, b"/exit\r")
        assert process.wait(timeout=8) == 0
        assert b"Traceback" not in output
        if mode == "plain":
            assert b"\x1b" not in output
        else:
            assert b"\x1b" in output
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        os.close(master)
