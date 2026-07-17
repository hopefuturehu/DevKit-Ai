from pathlib import Path

import pytest

from bot.config.models import PermissionsConfig
from bot.execution import (
    EnvironmentCapabilities,
    ExecutionTarget,
    LocalExecutionTarget,
    ProcessEventKind,
    ProcessSpec,
)
from bot.policy import DefaultPolicyEngine, PolicyDecisionKind, ToolAction
from bot.tools import ToolContext
from bot.tools.builtins import (
    ApplyPatchTool,
    ReadFileTool,
    RunCommandTool,
    RunShellTool,
    SearchTextTool,
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
