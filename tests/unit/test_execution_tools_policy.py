import asyncio
import os
import signal
from pathlib import Path

import pytest

from bot.config.models import PermissionsConfig
from bot.execution import (
    EnvironmentCapabilities,
    ExecutionTarget,
    LocalExecutionTarget,
    ProcessEventKind,
    ProcessSpec,
    ProcessStatus,
)
from bot.policy import DefaultPolicyEngine, PolicyDecisionKind, ToolAction
from bot.tools import ToolContext, ToolResultStatus
from bot.tools.builtins import (
    ApplyPatchTool,
    ListProcessesTool,
    PollProcessTool,
    ReadFileTool,
    RunCommandTool,
    RunShellTool,
    SearchTextTool,
    SendProcessInputTool,
    TerminateProcessTool,
)
from bot.tools.kunpeng import KsysTool, TunerTool


@pytest.mark.asyncio
async def test_local_execution_uses_argv_and_captures_streams(tmp_path: Path) -> None:
    target = LocalExecutionTarget()
    events = [
        event
        async for event in target.execute(
            ProcessSpec(
                argv=["/bin/sh", "-c", "printf out; printf err >&2"],
                cwd=tmp_path,
            )
        )
    ]

    assert any(event.kind == ProcessEventKind.STDOUT and event.data == "out" for event in events)
    assert any(event.kind == ProcessEventKind.STDERR and event.data == "err" for event in events)
    assert events[-1].returncode == 0


@pytest.mark.asyncio
async def test_local_execution_timeout_terminates_process(tmp_path: Path) -> None:
    target = LocalExecutionTarget()

    with pytest.raises(TimeoutError, match="命令执行超过"):
        _ = [
            event
            async for event in target.execute(
                ProcessSpec(
                    argv=["/bin/sh", "-c", "sleep 5"],
                    cwd=tmp_path,
                    timeout_seconds=0.05,
                )
            )
        ]


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
@pytest.mark.asyncio
async def test_local_termination_signals_the_process_group(monkeypatch) -> None:
    signals: list[tuple[int, signal.Signals]] = []

    class FakeProcess:
        pid = 4242
        returncode = None

        async def wait(self):
            self.returncode = 0
            return 0

    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    await LocalExecutionTarget._terminate(FakeProcess())

    assert signals == [(4242, signal.SIGTERM)]


@pytest.mark.asyncio
async def test_run_command_forwards_streaming_tool_output(tmp_path: Path) -> None:
    chunks: list[tuple[str, str]] = []

    async def capture(stream: str, data: str) -> None:
        chunks.append((stream, data))

    context = ToolContext(
        workspace=tmp_path,
        execution_target=LocalExecutionTarget(),
        output_callback=capture,
    )
    result = await RunCommandTool().execute(
        context,
        {"argv": ["/bin/sh", "-c", "printf out; printf err >&2"]},
    )

    assert result.success
    assert ("stdout", "out") in chunks
    assert ("stderr", "err") in chunks


@pytest.mark.asyncio
async def test_run_command_yields_and_polls_managed_process(tmp_path: Path) -> None:
    target = LocalExecutionTarget()
    context = ToolContext(workspace=tmp_path, execution_target=target)

    started = await RunCommandTool().execute(
        context,
        {
            "argv": ["/bin/sh", "-c", "printf start; sleep 0.15; printf end"],
            "wait_seconds": 0.02,
            "timeout_seconds": 2,
        },
    )

    assert started.success
    assert started.status == ToolResultStatus.RUNNING
    process_id = started.metadata["process_id"]

    completed = await PollProcessTool().execute(
        context,
        {"process_id": process_id, "wait_seconds": 1},
    )

    assert completed.success
    assert completed.status == ToolResultStatus.COMPLETED
    assert completed.metadata["process_status"] == ProcessStatus.COMPLETED
    assert "start" in started.output + completed.output
    assert "end" in started.output + completed.output
    await target.aclose()


@pytest.mark.asyncio
async def test_completed_managed_process_duration_stops_increasing(tmp_path: Path) -> None:
    target = LocalExecutionTarget()
    process_id = await target.start_process(
        ProcessSpec(
            argv=["/bin/sh", "-c", "sleep 0.03"],
            cwd=tmp_path,
            timeout_seconds=2,
        )
    )

    completed = await target.poll_process(process_id, wait_seconds=1)
    await asyncio.sleep(0.05)
    observed_later = await target.poll_process(process_id)

    assert completed.status == ProcessStatus.COMPLETED
    assert observed_later.status == ProcessStatus.COMPLETED
    assert observed_later.elapsed_seconds == pytest.approx(completed.elapsed_seconds, abs=0.005)
    await target.aclose()


@pytest.mark.asyncio
async def test_managed_process_hard_timeout_is_reported(tmp_path: Path) -> None:
    target = LocalExecutionTarget()
    context = ToolContext(workspace=tmp_path, execution_target=target)

    started = await RunCommandTool().execute(
        context,
        {
            "argv": ["/bin/sh", "-c", "sleep 5"],
            "wait_seconds": 0,
            "timeout_seconds": 0.05,
        },
    )
    timed_out = await PollProcessTool().execute(
        context,
        {"process_id": started.metadata["process_id"], "wait_seconds": 1},
    )

    assert started.status == ToolResultStatus.RUNNING
    assert not timed_out.success
    assert timed_out.status == ToolResultStatus.TIMED_OUT
    assert timed_out.metadata["process_status"] == ProcessStatus.TIMED_OUT
    assert "hard timeout" in (timed_out.error or "")
    assert not target.has_live_processes
    await target.aclose()


@pytest.mark.asyncio
async def test_managed_interactive_process_accepts_input(tmp_path: Path) -> None:
    target = LocalExecutionTarget()
    context = ToolContext(workspace=tmp_path, execution_target=target)

    started = await RunCommandTool().execute(
        context,
        {
            "argv": ["/bin/sh", "-c", 'read line; printf "got:%s" "$line"'],
            "interactive": True,
            "wait_seconds": 0,
            "timeout_seconds": 2,
        },
    )
    process_id = started.metadata["process_id"]
    sent = await SendProcessInputTool().execute(
        context,
        {"process_id": process_id, "data": "hello\n", "eof": True},
    )
    completed = await PollProcessTool().execute(
        context,
        {"process_id": process_id, "wait_seconds": 1},
    )

    assert sent.success
    assert completed.success
    assert completed.status == ToolResultStatus.COMPLETED
    assert "got:hello" in completed.output
    await target.aclose()


@pytest.mark.asyncio
async def test_managed_process_can_be_listed_and_terminated(tmp_path: Path) -> None:
    target = LocalExecutionTarget()
    context = ToolContext(workspace=tmp_path, execution_target=target)

    started = await RunCommandTool().execute(
        context,
        {
            "argv": ["/bin/sh", "-c", "sleep 5"],
            "wait_seconds": 0,
            "timeout_seconds": 10,
        },
    )
    process_id = started.metadata["process_id"]
    listed = await ListProcessesTool().execute(context, {})
    terminated = await TerminateProcessTool().execute(
        context,
        {"process_id": process_id, "reason": "测试清理"},
    )

    assert process_id in listed.output
    assert terminated.success
    assert terminated.metadata["process_status"] == ProcessStatus.CANCELLED
    assert not target.has_live_processes
    await target.aclose()


@pytest.mark.asyncio
async def test_managed_process_limit_is_a_recoverable_tool_error(tmp_path: Path) -> None:
    target = LocalExecutionTarget(max_managed_processes=1)
    context = ToolContext(workspace=tmp_path, execution_target=target)

    first = await RunCommandTool().execute(
        context,
        {
            "argv": ["/bin/sh", "-c", "sleep 5"],
            "wait_seconds": 0,
            "timeout_seconds": 10,
        },
    )
    second = await RunCommandTool().execute(
        context,
        {
            "argv": ["/bin/sh", "-c", "sleep 5"],
            "wait_seconds": 0,
            "timeout_seconds": 10,
        },
    )

    assert first.status == ToolResultStatus.RUNNING
    assert not second.success
    assert second.status == ToolResultStatus.FAILED
    assert "达到上限" in (second.error or "")
    await target.aclose()
    assert not target.has_live_processes


@pytest.mark.asyncio
async def test_managed_processes_are_cleaned_up_when_target_closes(tmp_path: Path) -> None:
    target = LocalExecutionTarget()
    process_id = await target.start_process(
        ProcessSpec(
            argv=["/bin/sh", "-c", "sleep 5"],
            cwd=tmp_path,
            timeout_seconds=10,
        )
    )

    assert target.has_live_processes
    await target.aclose()

    assert not target.has_live_processes
    with pytest.raises(ValueError, match="未知受管进程"):
        await target.poll_process(process_id)


@pytest.mark.asyncio
async def test_file_tools_enforce_workspace_and_exact_patch(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("old\n", encoding="utf-8")
    context = ToolContext(
        workspace=tmp_path,
        execution_target=LocalExecutionTarget(),
        workspace_only=True,
    )
    patch = await ApplyPatchTool().execute(
        context, {"path": "a.txt", "old_text": "old", "new_text": "new"}
    )
    read = await ReadFileTool().execute(context, {"path": "a.txt"})
    escaped = await ReadFileTool().execute(context, {"path": "../outside.txt"})

    assert patch.success
    assert patch.metadata["before_sha256"] != patch.metadata["after_sha256"]
    assert read.output == "new\n"
    assert not escaped.success
    assert "超出工作区" in (escaped.error or "")


@pytest.mark.asyncio
async def test_search_skips_symlinked_files_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("external-secret-marker", encoding="utf-8")
    (tmp_path / "outside-link.txt").symlink_to(outside)
    context = ToolContext(
        workspace=tmp_path,
        execution_target=LocalExecutionTarget(),
        workspace_only=True,
    )

    result = await SearchTextTool().execute(
        context, {"query": "external-secret-marker", "path": "."}
    )

    assert result.success
    assert "external-secret-marker" not in result.output


@pytest.mark.asyncio
async def test_file_tools_deny_internal_state_paths(tmp_path: Path) -> None:
    state_path = tmp_path / ".bot" / "state.db"
    state_path.parent.mkdir()
    state_path.write_text("internal-session-secret", encoding="utf-8")
    wal_path = Path(f"{state_path}-wal")
    wal_path.write_text("internal-wal-secret", encoding="utf-8")
    context = ToolContext(
        workspace=tmp_path,
        execution_target=LocalExecutionTarget(),
        denied_paths=(state_path, wal_path),
    )

    direct = await ReadFileTool().execute(context, {"path": ".bot/state.db"})
    search = await SearchTextTool().execute(context, {"query": "internal", "path": "."})

    assert not direct.success
    assert "受保护的内部路径" in (direct.error or "")
    assert search.success
    assert "internal-session-secret" not in search.output
    assert "internal-wal-secret" not in search.output


def test_policy_requires_approval_for_unknown_command_and_denies_escape(tmp_path: Path) -> None:
    policy = DefaultPolicyEngine(PermissionsConfig(), tmp_path)
    command = ToolAction(
        tool_name="run_command",
        arguments={"argv": ["python", "script.py"]},
        annotations=ApplyPatchTool.annotations,
    )
    escape = ToolAction(
        tool_name="read_file",
        arguments={"path": "../secret"},
        annotations=ReadFileTool.annotations,
    )

    assert policy.evaluate(command).kind == PolicyDecisionKind.ASK
    assert policy.evaluate(escape).kind == PolicyDecisionKind.DENY
    sensitive = ToolAction(
        tool_name="read_file",
        arguments={"path": ".env.local"},
        annotations=ReadFileTool.annotations,
    )
    assert policy.evaluate(sensitive).kind == PolicyDecisionKind.DENY


def test_policy_separately_evaluates_shell_segments(tmp_path: Path) -> None:
    policy = DefaultPolicyEngine(PermissionsConfig(), tmp_path)
    safe_pipeline = ToolAction(
        tool_name="run_shell",
        arguments={"script": "rg TODO | head"},
        annotations=RunShellTool.annotations,
    )
    redirection = ToolAction(
        tool_name="run_shell",
        arguments={"script": "rg TODO > result.txt"},
        annotations=RunShellTool.annotations,
    )
    bypass = ToolAction(
        tool_name="run_command",
        arguments={"argv": ["/bin/sh", "-c", "rm -rf data"]},
        annotations=RunCommandTool.annotations,
    )

    assert policy.evaluate(safe_pipeline).kind == PolicyDecisionKind.ALLOW
    assert policy.evaluate(redirection).kind == PolicyDecisionKind.ASK
    assert policy.evaluate(bypass).kind == PolicyDecisionKind.DENY
    find_delete = ToolAction(
        tool_name="run_command",
        arguments={"argv": ["find", ".", "-delete"]},
        annotations=RunCommandTool.annotations,
    )
    workload = ToolAction(
        tool_name="tuner",
        arguments={"task": "top-down", "workload": ["./app"]},
        annotations=TunerTool.annotations,
    )
    assert policy.evaluate(find_delete).kind == PolicyDecisionKind.ASK
    assert policy.evaluate(workload).kind == PolicyDecisionKind.ASK


def test_policy_prevents_read_only_commands_from_escaping_workspace(tmp_path: Path) -> None:
    policy = DefaultPolicyEngine(PermissionsConfig(), tmp_path)
    absolute_escape = ToolAction(
        tool_name="run_command",
        arguments={"argv": ["head", "/etc/passwd"]},
        annotations=RunCommandTool.annotations,
    )
    relative_escape = ToolAction(
        tool_name="run_shell",
        arguments={"script": "rg secret ../outside"},
        annotations=RunShellTool.annotations,
    )
    environment_expansion = ToolAction(
        tool_name="run_shell",
        arguments={"script": "head $HOME/.ssh/id_rsa"},
        annotations=RunShellTool.annotations,
    )

    assert policy.evaluate(absolute_escape).kind == PolicyDecisionKind.DENY
    assert policy.evaluate(relative_escape).kind == PolicyDecisionKind.DENY
    assert policy.evaluate(environment_expansion).kind == PolicyDecisionKind.ASK


def test_ksys_and_tuner_build_structured_argv(tmp_path: Path) -> None:
    input_one = tmp_path / "one.json"
    input_two = tmp_path / "two.json"
    input_one.write_text("{}", encoding="utf-8")
    input_two.write_text("{}", encoding="utf-8")
    context = ToolContext(workspace=tmp_path, execution_target=LocalExecutionTarget())

    ksys = KsysTool().build_argv(
        context,
        {
            "operation": "diff",
            "input_paths": ["one.json", "two.json"],
            "output_path": "reports",
            "log_level": 2,
        },
    )
    tuner = TunerTool().build_argv(
        context,
        {
            "task": "top-down",
            "duration": 10,
            "topdown_level": 2,
            "pid": "123",
        },
    )

    assert ksys[:3] == ["ksys", "diff", "-i"]
    assert ksys[-4:] == ["-o", str(tmp_path / "reports"), "-l", "2"]
    assert tuner == ["devkit", "tuner", "top-down", "-d", "10", "-p", "123", "-L", "2"]
    with pytest.raises(ValueError, match="duration 不适用"):
        KsysTool().build_argv(
            context,
            {"operation": "report", "input_paths": ["one.json"], "duration": 10},
        )
    with pytest.raises(ValueError, match="必须指定 workload"):
        TunerTool().build_argv(context, {"task": "roofline"})


class X86LinuxTarget(ExecutionTarget):
    async def probe(self, executables=None) -> EnvironmentCapabilities:
        return EnvironmentCapabilities(
            operating_system="linux",
            architecture="x86_64",
            executables={name: f"/usr/bin/{name}" for name in executables or []},
        )

    def execute(self, spec):
        raise AssertionError("unsupported architecture must not execute")


@pytest.mark.asyncio
async def test_tuner_returns_arm_manual_command_when_target_is_x86(tmp_path: Path) -> None:
    context = ToolContext(workspace=tmp_path, execution_target=X86LinuxTarget())

    result = await TunerTool().execute(
        context,
        {"task": "top-down", "duration": 10, "pid": "123"},
    )

    assert not result.success
    assert result.metadata["manual_command"] == "devkit tuner top-down -d 10 -p 123"
    assert "鲲鹏 ARM 主机" in result.model_content()
