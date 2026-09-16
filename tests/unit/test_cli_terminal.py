import asyncio
import importlib
import json
from datetime import UTC, datetime, timedelta
from io import StringIO
from types import SimpleNamespace

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from bot.cli.app import _dispatch_command, _parse_prompt
from bot.cli.commands import CommandCompleter
from bot.cli.interrupts import read_prompt
from bot.cli.presentation import InteractiveEventSink
from bot.cli.render import create_cli_console
from bot.cli.terminal import SafeHistory, TerminalState, TerminalUI, terminal_enabled
from bot.config import AppConfig
from bot.core.events import AgentEvent, EventType
from bot.observability import Redactor
from bot.sessions import SQLiteSessionStore


def event(kind, payload=None, *, session="session", seconds=0):
    return AgentEvent(
        type=kind,
        session_id=session,
        run_id="run",
        payload=payload or {},
        timestamp=datetime(2026, 9, 16, tzinfo=UTC) + timedelta(seconds=seconds),
    )


def runtime_stub(path):
    config = AppConfig()
    config.display.history = False
    config.model.name = "test-model"
    return SimpleNamespace(
        config=config,
        workspace=path,
        runner=SimpleNamespace(redactor=Redactor(["private-key"])),
    )


def test_mode_detection_never_enables_terminal_for_json_or_redirected_output(monkeypatch):
    module = importlib.import_module("bot.cli.terminal")
    monkeypatch.setattr(module.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(module.sys, "stdout", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setenv("TERM", "xterm-256color")
    assert terminal_enabled("auto", interactive=True)
    assert not terminal_enabled("plain", interactive=True)
    assert not terminal_enabled("terminal", interactive=True, json_output=True)
    assert not terminal_enabled("terminal", interactive=False)
    monkeypatch.setenv("TERM", "dumb")
    assert not terminal_enabled("auto", interactive=True)
    monkeypatch.setattr(module.sys, "stdout", SimpleNamespace(isatty=lambda: False))
    assert not terminal_enabled("auto", interactive=True)


def test_command_menu_and_arguments():
    completer = CommandCompleter()
    menu = list(completer.get_completions(Document("/"), CompleteEvent()))
    assert {"/status", "/details", "/cancel", "/help"} <= {item.text for item in menu}
    assert all(item.display_meta for item in menu)
    modes = list(completer.get_completions(Document("/permissions "), CompleteEvent()))
    assert {item.text for item in modes} == {
        "/permissions safe",
        "/permissions read-only",
        "/permissions full-access",
    }
    assert not list(completer.get_completions(Document("hello"), CompleteEvent()))


def test_multiline_task_preserves_code_and_skill_prefix():
    text = '请检查：\n```python\nif True:\n    print("a  b")\n```'
    assert _parse_prompt("$first $second\n" + text) == (text, ["first", "second"])
    assert _parse_prompt(text) == (text, [])
    assert _parse_prompt("$first") == ("", ["first"])
    code = "    print('indented')\n"
    assert _parse_prompt(code) == (code, [])
    assert _parse_prompt("$first\n" + code) == (code, ["first"])


@pytest.mark.asyncio
async def test_history_persists_multiline_and_excludes_credentials(tmp_path):
    path = tmp_path / "history" / "workspace.history"
    history = SafeHistory(path, Redactor(["private-key"]))
    history.append_string("hello\nworld")
    history.append_string("api_key=secret-value")
    history.append_string("use private-key")
    assert history.get_strings() == ["hello\nworld"]
    restored = SafeHistory(path, Redactor(["private-key"]))
    assert [text async for text in restored.load()] == ["hello\nworld"]
    assert path.stat().st_mode & 0o077 == 0
    assert "secret-value" not in path.read_text()


async def wait_for_input(session):
    if session.app.is_running:
        return
    ready = asyncio.Event()

    def on_render(app):
        ready.set()

    session.app.before_render += on_render
    try:
        await asyncio.wait_for(ready.wait(), 2)
    finally:
        session.app.before_render -= on_render


@pytest.mark.asyncio
async def test_real_key_bindings_multiline_bracketed_paste_and_approval_history(tmp_path):
    ui = TerminalUI(enhanced=True)
    runtime = runtime_stub(tmp_path)
    with create_pipe_input() as pipe:
        session = ui.create_prompt(runtime, input=pipe, output=DummyOutput())
        task = asyncio.create_task(read_prompt(session, "> "))
        await wait_for_input(session)
        pipe.send_text("one\x1b\rtwo\r")  # Alt+Enter inserts a newline; Enter sends.
        assert await asyncio.wait_for(task, 2) == "one\ntwo"
        task = asyncio.create_task(read_prompt(session, "> "))
        await wait_for_input(session)
        pipe.send_text("\x1b[200~if True:\n    print('ok')\n\x1b[201~")
        await asyncio.sleep(0.05)
        assert not task.done(), "A multiline paste must not submit the task."
        pipe.send_text("\r")
        assert await asyncio.wait_for(task, 2) == "if True:\n    print('ok')\n"
        history = session.history
        task = asyncio.create_task(read_prompt(session, "[approve: ] ", approval=True))
        await wait_for_input(session)
        assert session.completer is None and session.auto_suggest is None
        assert session.multiline is False
        pipe.send_text("y\r")
        assert await asyncio.wait_for(task, 2) == "y"
        assert "y" not in history.get_strings()
        assert session.history is history and session.completer is not None


@pytest.mark.asyncio
async def test_draft_survives_approval_and_history_search_works(tmp_path):
    ui = TerminalUI(enhanced=True)
    with create_pipe_input() as pipe:
        session = ui.create_prompt(runtime_stub(tmp_path), input=pipe, output=DummyOutput())
        task = asyncio.create_task(read_prompt(session, "> "))
        await wait_for_input(session)
        pipe.send_text("unfinished draft")
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        approval = asyncio.create_task(read_prompt(session, "approve ", approval=True))
        await wait_for_input(session)
        pipe.send_text("n\r")
        await asyncio.wait_for(approval, 2)
        task = asyncio.create_task(read_prompt(session, "> "))
        await wait_for_input(session)
        pipe.send_text(" done\r")
        assert await asyncio.wait_for(task, 2) == "unfinished draft done"
        task = asyncio.create_task(read_prompt(session, "> "))
        await wait_for_input(session)
        pipe.send_text("\x12unfinished\r\r")  # Ctrl+R, select match, submit.
        assert await asyncio.wait_for(task, 2) == "unfinished draft done"


@pytest.mark.asyncio
async def test_busy_commands_are_local_and_configuration_changes_are_blocked(tmp_path, monkeypatch):
    module = importlib.import_module("bot.cli.app")
    stream = StringIO()
    monkeypatch.setattr(module, "console", create_cli_console(file=stream))
    runtime = runtime_stub(tmp_path)
    for command in ("/model", "/model changed", "/new", "/permissions full-access", "/unknown"):
        assert await _dispatch_command(runtime, "session", command, running=True) == (
            "session",
            False,
        )
    assert runtime.config.model.name == "test-model"
    assert runtime.config.permissions.mode == "safe"
    assert "等待运行结束" in stream.getvalue() and "未知命令" in stream.getvalue()


def test_state_uses_request_context_not_cumulative_usage_and_ignores_children():
    state = TerminalState(session_id="session")
    state.observe(event(EventType.RUN_STARTED))
    state.observe(
        event(EventType.MODEL_REQUEST_STARTED, {"input_token_estimate": {"tokens": 1200}})
    )
    state.observe(event(EventType.MODEL_USAGE, {"input_tokens": 99_999}))
    state.observe(event(EventType.APPROVAL_REQUESTED, session="child"))
    assert state.phase == "等待模型" and state.context_tokens == 1200
    state.observe(event(EventType.RUN_CANCELLED))
    state.observe(event(EventType.RUN_FINISHED, {"status": "cancelled"}))
    assert state.phase == "已取消" and state.started is None


@pytest.mark.asyncio
@pytest.mark.parametrize("enhanced", [True, False])
async def test_tool_summary_exact_status_duration_details_and_no_duplicate_results(
    tmp_path, enhanced
):
    store = SQLiteSessionStore(tmp_path / "state.db")
    session_id = store.create_session(tmp_path)
    ui = TerminalUI(enhanced=enhanced)
    ui.state.select_session(session_id)
    ui.runtime = SimpleNamespace(store=store)
    stream = StringIO()
    sink = InteractiveEventSink(create_cli_console(file=stream, width=120), ui)
    payload = {"tool_call_id": "call", "name": "run_command", "arguments": {"argv": ["sleep", "5"]}}
    reference = store.put_context_blob(
        session_id=session_id, run_id="run", content="x" * 16000 + "尾页"
    )
    try:
        await sink.publish(event(EventType.TOOL_REQUESTED, payload, session=session_id))
        await sink.publish(event(EventType.TOOL_STARTED, payload, session=session_id, seconds=1))
        result = {**payload, "success": True, "status": "running", "context_ref": reference}
        await sink.publish(event(EventType.TOOL_COMPLETED, result, session=session_id, seconds=3))
        await sink.publish(event(EventType.TOOL_RESULT, result, session=session_id, seconds=3))
        output = stream.getvalue()
        assert output.count("后台运行中") == 1 and "2.0s" in output
        assert "完成" not in output
        sink.show_details(1)
        assert "/details 1 16000" in stream.getvalue()
        sink.show_details(1, 16000)
        assert "尾页" in stream.getvalue()
        chinese = "中" * 6000
        reference = store.put_context_blob(
            session_id=session_id, run_id="run", content=chinese,
        )
        next(iter(ui.state.tools.values())).reference = reference
        stream.truncate(0)
        stream.seek(0)
        sink.show_details(1)
        assert "/details 1 15999" in stream.getvalue()
        sink.show_details(1, 15999)
        assert "�" not in stream.getvalue()
        assert stream.getvalue().count("中") == 6000
        if not enhanced:
            assert "\x1b" not in stream.getvalue()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_markdown_streaming_retains_fences_and_filters_child_noise():
    ui = TerminalUI(enhanced=True)
    ui.state.select_session("session")
    stream = StringIO()
    sink = InteractiveEventSink(create_cli_console(file=stream, width=100), ui)
    await sink.publish(event(EventType.ASSISTANT_DELTA, {"text": "第一段\n\n```python\n"}))
    assert "第一段" in stream.getvalue()
    assert "```" not in stream.getvalue()
    await sink.publish(event(EventType.ASSISTANT_DELTA, {"text": 'print("ok")\n```\n\n'}))
    await sink.publish(event(EventType.ASSISTANT_MESSAGE, {"text": "unused fallback"}))
    await sink.publish(event(EventType.ASSISTANT_DELTA, {"text": "child noise"}, session="child"))
    await sink.publish(
        event(EventType.SUBAGENT_PROGRESS, {"task_id": "child", "summary": "搜索中"})
    )
    assert 'print("ok")' in stream.getvalue()
    assert "child noise" not in stream.getvalue() and "搜索中" not in stream.getvalue()
    assert ui.state.background["child"] == "搜索中"


@pytest.mark.asyncio
async def test_plain_assistant_preserves_markdown_and_json_output_is_not_reinterpreted(tmp_path):
    ui = TerminalUI(enhanced=False)
    ui.state.select_session("session")
    stream = StringIO()
    sink = InteractiveEventSink(create_cli_console(file=stream), ui)
    markdown = "# Title\n\n```python\nprint(1)\n```"
    await sink.publish(event(EventType.ASSISTANT_DELTA, {"text": markdown}))
    await sink.publish(event(EventType.ASSISTANT_MESSAGE, {"text": markdown}))
    assert stream.getvalue() == markdown + "\n"
    # A tool's JSON document is user data, not a ToolResult envelope.
    content = json.dumps({"output": "value", "other": "must survive"})
    ui.runtime = SimpleNamespace(
        store=SimpleNamespace(
            read_context_blob=lambda *a, **kw: {
                "content": content,
                "eof": True,
            }
        )
    )
    await sink.publish(
        event(
            EventType.TOOL_RESULT,
            {
                "tool_call_id": "call",
                "name": "read_file",
                "success": True,
                "context_ref": "blob:ref",
            },
        )
    )
    sink.show_details(1)
    assert "must survive" in stream.getvalue()


def test_json_mode_uses_only_json_sink(tmp_path, monkeypatch):
    from bot.core.events import JsonlEventSink

    module = importlib.import_module("bot.cli.app")
    runtime = runtime_stub(tmp_path)
    captured = {}
    monkeypatch.setattr(module, "load_config", lambda *a, **kw: runtime.config)

    def build(**kwargs):
        captured.update(kwargs)
        return runtime

    monkeypatch.setattr(module, "build_runtime", build)
    monkeypatch.setattr(module, "console", create_cli_console(file=StringIO()))
    result = module._runtime(
        tmp_path,
        None,
        json_output=True,
        interactive=False,
        ui_mode="terminal",
    )
    assert not result.cli_ui.enhanced
    assert len(captured["event_sinks"]) == 1
    assert isinstance(captured["event_sinks"][0], JsonlEventSink)
    assert captured["approval_handler"] is None


@pytest.mark.asyncio
async def test_details_during_stream_do_not_repeat_the_whole_answer():
    ui = TerminalUI(enhanced=True)
    ui.state.select_session("session")
    stream = StringIO()
    sink = InteractiveEventSink(create_cli_console(file=stream), ui)
    await sink.publish(event(EventType.ASSISTANT_DELTA, {"text": "unique opening"}))
    sink.show_details()
    await sink.publish(event(EventType.ASSISTANT_DELTA, {"text": " ending"}))
    await sink.publish(event(EventType.ASSISTANT_MESSAGE, {"text": "unique opening ending"}))
    assert stream.getvalue().count("unique opening") == 1


@pytest.mark.asyncio
async def test_full_tool_output_remains_streamed_and_does_not_repeat():
    ui = TerminalUI(enhanced=False)
    ui.state.select_session("session")
    stream = StringIO()
    sink = InteractiveEventSink(create_cli_console(file=stream), ui, show_tool_output=True)
    base = {"tool_call_id": "call", "name": "run_command"}
    await sink.publish(event(EventType.TOOL_REQUESTED, base))
    await sink.publish(event(EventType.TOOL_OUTPUT, {**base, "data": "live line\n"}))
    assert "live line" in stream.getvalue()
    await sink.publish(
        event(
            EventType.TOOL_COMPLETED,
            {
                **base,
                "success": True,
                "status": "completed",
            },
        )
    )
    assert stream.getvalue().count("live line") == 1


def test_process_cleanup_updates_previously_running_tool():
    state = TerminalState(session_id="session")
    state.observe(
        event(
            EventType.TOOL_RESULT,
            {
                "tool_call_id": "call",
                "name": "run_command",
                "status": "running",
                "process_id": "pid",
            },
        )
    )
    state.observe(
        event(
            EventType.PROCESS_CLEANUP_FINISHED,
            {
                "process_id": "pid",
                "status": "cancelled",
            },
        )
    )
    assert next(iter(state.tools.values())).status == "cancelled"


@pytest.mark.asyncio
async def test_approval_shows_command_directory_scope_and_redacts_secrets(tmp_path):
    from bot.cli.render import InteractiveApprovalHandler
    from bot.policy import ApprovalPattern, PolicyDecision, PolicyDecisionKind, ToolAction
    from bot.tools import ToolAnnotations

    stream = StringIO()
    handler = InteractiveApprovalHandler(
        create_cli_console(file=stream, width=200), workspace=tmp_path
    )
    handler.redactor = Redactor(["private-key"])
    action = ToolAction(
        tool_name="run_command",
        arguments={"argv": ["git", "push", "[literal]", "private-key"], "cwd": "nested"},
        annotations=ToolAnnotations(network_access=True),
    )
    decision = PolicyDecision(
        kind=PolicyDecisionKind.ASK,
        reason="网络操作需要批准",
        approval_pattern=ApprovalPattern(
            kind="command_prefix",
            workspace=str(tmp_path),
            tool_name="run_command",
            command_prefix=["git", "push"],
            description="git push 命令族",
        ),
    )

    class Choice:
        async def prompt_async(self, message, **kwargs):
            return "a"

    task = asyncio.create_task(handler.approve(action, decision))
    await handler.resolve(await handler.next_request(), Choice())
    response = await task
    assert response.approved and response.scope.value == "always"
    output = stream.getvalue()
    assert str(tmp_path / "nested") in output
    assert "git push '[literal]'" in output and "private-key" not in output
    assert "command_prefix" in output and "前缀匹配可覆盖不同参数" in output
    assert "当前项目持久保存" in output
