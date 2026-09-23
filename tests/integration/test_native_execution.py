"""Real managed Native backend checks; production factory stays closed."""

import asyncio
import json
import os
import platform
from pathlib import Path

import pytest
import pytest_asyncio

from bot.config.models import AppConfig
from bot.execution.base import ProcessSpec, ProcessStatus
from bot.execution.native import NativeSandboxExecutionTarget
from bot.sandbox.models import SandboxError
from bot.tools.base import ToolAnnotations, ToolContext
from bot.tools.builtins import RunCommandTool, RunShellTool
from bot.tools.subprocess_adapter import SubprocessCliTool

EXPECTED_UNRESOLVED = pytest.StashKey[set[str]]()


@pytest_asyncio.fixture
async def native_target(tmp_path, request, record_property):
    request.node.stash[EXPECTED_UNRESOLVED] = set()
    runtime = os.environ.get("BOT_TEST_SANDBOX_RUNTIME")
    if not runtime:
        if os.environ.get("BOT_REQUIRE_NATIVE_SANDBOX") == "1":
            pytest.fail("release validation requires BOT_TEST_SANDBOX_RUNTIME")
        pytest.skip("native runtime not selected")
    work = tmp_path / "work"
    work.mkdir()
    config = AppConfig.model_validate({"sandbox": {"mode": "required"}})
    target = NativeSandboxExecutionTarget(
        config, workspace=work, runtime_root=Path(runtime), state_root=tmp_path / "runs"
    )
    try:
        yield target, work, Path(runtime).resolve() / "node/bin/node"
    finally:
        try:
            await target.aclose()
        except RuntimeError as exc:
            snapshots = await target.list_processes()
            exc.add_note(str([p.model_dump(mode="json") for p in snapshots]))
            exc.add_note(
                str({k: p.scope.unattributed for k, p in target._managed_processes.items()})
            )
            expected = request.node.stash[EXPECTED_UNRESOLVED]
            if not expected or {p.process_id for p in snapshots} != expected:
                raise
            # Only an explicitly asserted fail-closed result permits retention.
            # Never kill ambiguous processes or delete their resources here.
            assert all(p.status == ProcessStatus.CLEANUP_FAILED for p in snapshots)
            assert all(p.cleanup_status == "unknown" for p in snapshots)
            record_property("shutdown", "unknown; resources retained")
            assert list((tmp_path / "runs").iterdir())
        else:
            assert not list((tmp_path / "runs").iterdir())


def scoped_spec(target, work, argv, *, call="call", run="run", readonly=False, **kwargs):
    scope = target.bind_scope(
        session_id="session", run_id=run, tool_call_id=call, read_only=readonly
    )
    return ProcessSpec(
        argv=list(map(str, argv)), cwd=work, session_id="session", run_id=run, scope=scope, **kwargs
    )


@pytest.mark.asyncio
async def test_native_managed_argv_metadata_io_and_control_cleanup(native_target):
    target, work, node = native_target
    outside = work.parent / "sentinel"
    outside.write_text("preserve")
    argv = [
        node,
        "-e",
        """const fs=require('fs');fs.writeFileSync('ok','done');
let denied;try{fs.writeFileSync(process.argv[1],'bad');denied=false}catch{denied=true}
console.log(JSON.stringify({denied,args:process.argv.slice(2)}));""",
        outside,
        "",
        "中文",
        "a'b",
    ]
    spec = scoped_spec(target, work, argv)
    pid = await target.start_process(spec)
    result = await target.poll_process(pid, wait_seconds=10)
    assert result.status == ProcessStatus.COMPLETED, result
    assert result.argv == list(map(str, argv)) and result.scope == spec.scope
    assert json.loads(result.stdout) == {"denied": True, "args": ["", "中文", "a'b"]}
    assert (work / "ok").read_text() == "done" and outside.read_text() == "preserve"
    assert result.execution_metadata["backend"] == "srt"
    control = Path(result.execution_metadata["control_directory"]["path"])
    assert not control.exists()
    assert Path(result.execution_metadata["run_directory"]["path"]).exists()
    with pytest.raises(SandboxError, match="作用域"):
        await target.start_process(spec)


@pytest.mark.asyncio
async def test_native_rejects_missing_mismatched_or_stale_scope_before_execution(native_target):
    target, work, node = native_target
    argv = [node, "-e", "require('fs').writeFileSync('executed','bad')"]
    with pytest.raises(SandboxError, match="作用域"):
        await target.start_process(ProcessSpec(argv=list(map(str, argv)), cwd=work))
    spec = scoped_spec(target, work, argv)
    with pytest.raises(SandboxError, match="身份"):
        await target.start_process(spec.model_copy(update={"session_id": "other"}))
    (work / "changed-after-admission").touch()
    with pytest.raises(SandboxError, match="路径发生变化"):
        await target.start_process(spec)
    assert not (work / "executed").exists() and not target.has_live_processes


@pytest.mark.asyncio
async def test_native_interactive_input_streams_and_nonzero_exit(native_target):
    target, work, node = native_target
    spec = scoped_spec(
        target,
        work,
        [
            node,
            "-e",
            """process.stdin.on('data',d=>process.stdout.write(d));
process.stdin.on('end',()=>{console.error('diagnostic');process.exitCode=7})""",
        ],
        interactive=True,
    )
    pid = await target.start_process(spec)
    await target.send_process_input(pid, "交互输入\n", eof=True)
    result = await target.poll_process(pid, wait_seconds=10)
    assert result.status == ProcessStatus.FAILED and result.returncode == 7
    assert result.stdout == "交互输入\n" and result.stderr == "diagnostic\n"
    assert result.output_complete


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_native_timeout_and_cancel_preserve_cleanup_contract(
    native_target, cancel, request, record_property
):
    target, work, node = native_target
    spec = scoped_spec(
        target,
        work,
        [node, "-e", "console.log('ready');setInterval(()=>{},1000)"],
        timeout_seconds=10 if cancel else 0.8,
    )
    pid = await target.start_process(spec)
    if cancel:
        async with asyncio.timeout(5):
            while "ready" not in (await target.poll_process(pid, consume_output=False)).stdout:  # noqa: ASYNC110 - native output handshake
                await asyncio.sleep(0.02)
        result = await target.terminate_process(pid)
    else:
        result = await target.poll_process(pid, wait_seconds=10)
    record_property("cleanup_status", result.cleanup_status)
    record_property("process_status", result.status.value)
    if result.status == ProcessStatus.CLEANUP_FAILED:
        assert platform.system() == "Darwin", result
        assert result.cleanup_status == "unknown" and result.output_complete
        managed = target._managed_processes[pid]
        assert managed.scope.unknown and managed.scope.unattributed
        assert managed.process.root_exited.is_set() and not result.remaining_processes
        assert target.has_live_processes
        assert Path(result.execution_metadata["control_directory"]["path"]).is_dir()
        with pytest.raises(RuntimeError, match="process_cleanup_incomplete"):
            await target.start_process(scoped_spec(target, work, [node, "-e", "0"], call="next"))
        record_property("unattributed_count", len(managed.scope.unattributed))
        request.node.stash[EXPECTED_UNRESOLVED].add(pid)
        return
    assert result.status == (ProcessStatus.CANCELLED if cancel else ProcessStatus.TIMED_OUT), result
    assert result.cleanup_status == "observed_empty" and not target.has_live_processes
    assert not Path(result.execution_metadata["control_directory"]["path"]).exists()


@pytest.mark.asyncio
async def test_native_runs_have_separate_home_and_readonly_workspace(native_target):
    target, work, node = native_target
    homes = []
    for index in range(2):
        spec = scoped_spec(
            target,
            work,
            [
                node,
                "-e",
                """const fs=require('fs');fs.writeFileSync(process.env.HOME+'/own','ok');
let denied;try{fs.writeFileSync('forbidden','bad');denied=false}catch{denied=true}
console.log(JSON.stringify({home:process.env.HOME,denied}));""",
            ],
            call=f"call-{index}",
            run=f"run-{index}",
            readonly=True,
        )
        pid = await target.start_process(spec)
        result = await target.poll_process(pid, wait_seconds=10)
        assert result.status == ProcessStatus.COMPLETED, result
        output = json.loads(result.stdout)
        assert output["denied"]
        homes.append(output["home"])
    assert homes[0] != homes[1] and not (work / "forbidden").exists()


class ProbeCli(SubprocessCliTool):
    executable = "node"
    name = "probe_cli"
    annotations = ToolAnnotations()

    def build_argv(self, context, arguments):
        return ["node", "-e", "process.stdout.write('adapter-ok')"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["command", "shell", "adapter"])
async def test_native_command_tools_forward_trusted_scope(native_target, kind):
    target, work, node = native_target
    scope = target.bind_scope(session_id="session", run_id="run", tool_call_id="tool")
    context = ToolContext(
        workspace=work,
        execution_target=target,
        session_id="session",
        run_id="run",
        execution_scope=scope,
    )
    tool, arguments, expected = {
        "command": (
            RunCommandTool(),
            {"argv": [str(node), "-e", "console.log('command-ok')"]},
            "command-ok",
        ),
        "shell": (RunShellTool(), {"script": "printf shell-ok"}, "shell-ok"),
        "adapter": (ProbeCli(), {}, "adapter-ok"),
    }[kind]
    result = await tool.execute(context, arguments)
    assert result.success and expected in result.output, result
    snapshots = await target.list_processes()
    assert len(snapshots) == 1 and snapshots[0].scope == scope
    denied = await tool.execute(context, arguments)
    assert not denied.success and denied.metadata["executed"] is False
    assert denied.metadata["reason_code"] == "SANDBOX_CONFIG_INVALID"
