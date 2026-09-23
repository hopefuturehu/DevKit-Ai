"""Real managed Native backend checks; production factory stays closed."""

import asyncio
import json
import os
import platform
import sqlite3
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


def invocation(target, work, *, session="session", run="run", call="call", **kwargs):
    return target.for_invocation(
        workspace=work, session_id=session, run_id=run, tool_call_id=call, **kwargs
    )


@pytest.mark.asyncio
async def test_invocation_rejects_foreign_process_controls_without_consuming_output(native_target):
    target, work, node = native_target
    owner = invocation(target, work)
    pid = await owner.start_process(
        ProcessSpec(
            argv=[
                str(node),
                "-e",
                "console.log('private-ready');process.stdin.on('data',d=>process.stdout.write(d));"
                "process.stdin.on('end',()=>console.log('private-end'))",
            ],
            cwd=work,
            interactive=True,
            timeout_seconds=20,
        )
    )
    async with asyncio.timeout(10):
        while "private-ready" not in (await owner.poll_process(pid, consume_output=False)).stdout:  # noqa: ASYNC110 - native output handshake
            await asyncio.sleep(0.02)
    # Releasing a tool call must leave its background process available to the
    # next authorized tool. Closing the view must not close the shared manager.
    await owner.aclose()
    assert target.has_live_processes
    with pytest.raises(SandboxError, match="已释放"):
        await owner.poll_process(pid)
    for session, run in (("other-session", "run"), ("session", "other-run")):
        foreign = invocation(target, work, session=session, run=run)
        try:
            assert await foreign.list_processes() == []
            for process_id in (pid, "unknown-process"):
                operations = (
                    (foreign.poll_process, {}),
                    (foreign.read_process_output, {}),
                    (foreign.send_process_input, {"data": "injected", "eof": True}),
                    (foreign.terminate_process, {}),
                )
                for operation, arguments in operations:
                    with pytest.raises(SandboxError) as rejected:
                        await operation(process_id, **arguments)
                    assert rejected.value.code.value == "POLICY_DENIED"
                    assert rejected.value.stage == "ownership"
        finally:
            foreign.release_invocation()
    reader = invocation(target, work, call="read")
    try:
        assert [p.process_id for p in await reader.list_processes()] == [pid]
        await reader.send_process_input(pid, "owner-input\n", eof=True)
        result = await reader.poll_process(pid, wait_seconds=10)
        assert result.status == ProcessStatus.COMPLETED, result
        assert result.stdout == "private-ready\nowner-input\nprivate-end\n"
        assert result.scope.session_id == "session" and result.scope.run_id == "run"
        assert result.scope.tool_call_id == "call"
        record = await reader.read_process_output(pid)
        assert record["stdout"] == result.stdout
        # Returned nested cleanup metadata is not authority to change the
        # manager's stored resource identity or its eventual cleanup path.
        result.execution_metadata["run_directory"]["path"] = str(work)
        fresh = await reader.poll_process(pid)
        assert fresh.execution_metadata["run_directory"]["path"] != str(work)
    finally:
        reader.release_invocation()


@pytest.mark.asyncio
async def test_invocation_rejects_identity_spoof_and_discards_unstarted_scope(native_target):
    target, work, node = native_target
    owner = invocation(target, work)
    spec = ProcessSpec(argv=[str(node), "-e", "0"], cwd=work)
    try:
        for update in ({"session_id": "other"}, {"run_id": "other"}):
            with pytest.raises(SandboxError, match="不能替换"):
                await owner.start_process(spec.model_copy(update=update))
        assert not target._bindings and not target._runs
        raw = scoped_spec(target, work, [node, "-e", "0"], call="host-call")
        with pytest.raises(SandboxError, match="不能替换"):
            await owner.start_process(raw)
        target.discard_scope(raw.scope)
        # Creating but never iterating a stream reserves a scope. Releasing
        # the view must discard it and must prevent invocation-ID replay.
        stream = owner.execute(spec)
        assert len(target._bindings) == 1
        owner.release_invocation()
        await stream.aclose()
        assert not target._bindings and not target.has_live_processes
        replay = invocation(target, work)
        try:
            with pytest.raises(SandboxError, match="不能重复"):
                await replay.start_process(spec)
        finally:
            replay.release_invocation()
        with pytest.raises(SandboxError, match="子工作区"):
            invocation(target, work.parent)
    finally:
        owner.release_invocation()


@pytest.mark.asyncio
async def test_invocation_freezes_run_permissions_and_protected_paths(native_target):
    target, work, node = native_target
    protected = work / "private.txt"
    protected.write_text("host-private")
    script = """const fs=require('fs');let readDenied=false,writeDenied=false;
try{fs.readFileSync('private.txt')}catch{readDenied=true}
try{fs.writeFileSync('marker','ok')}catch{writeDenied=true}
console.log(JSON.stringify({readDenied,writeDenied}));"""
    for readonly in (True, False):
        owner = invocation(
            target, work, run=str(readonly), read_only=readonly, denied_paths=(protected,)
        )
        try:
            pid = await owner.start_process(ProcessSpec(argv=[str(node), "-e", script], cwd=work))
            result = await owner.poll_process(pid, wait_seconds=10)
            assert result.status == ProcessStatus.COMPLETED, result
            assert json.loads(result.stdout) == {"readDenied": True, "writeDenied": readonly}
        finally:
            owner.release_invocation()
        for update in ({"read_only": not readonly}, {"denied_paths": ()}):
            changed = invocation(
                target,
                work,
                run=str(readonly),
                call="changed",
                **({"read_only": readonly, "denied_paths": (protected,)} | update),
            )
            try:
                with pytest.raises(SandboxError, match="不能改变"):
                    await changed.start_process(ProcessSpec(argv=[str(node), "-e", "0"], cwd=work))
            finally:
                changed.release_invocation()
    assert protected.read_text() == "host-private" and (work / "marker").read_text() == "ok"


@pytest.mark.asyncio
async def test_agent_binds_native_identity_and_rejects_unconfined_tools(native_target, monkeypatch):
    from bot.core import AgentRunner, RunRequest
    from bot.core.approval import AllowApprovalHandler
    from bot.core.context import ContextAssembler
    from bot.core.events import EventBus, EventType
    from bot.core.models import ModelCapabilities, ModelEvent, ModelEventKind
    from bot.policy import DefaultPolicyEngine
    from bot.providers import ModelProvider
    from bot.sessions import SQLiteSessionStore
    from bot.skills import SkillCatalog, SkillManager
    from bot.tools import ToolRegistry
    from bot.tools.builtins import ReadFileTool

    target, work, node = native_target
    calls = [
        ("command-call", "run_command", {"argv": [str(node), "-e", "console.log('agent-native')"]}),
        ("read-call", "read_file", {"path": "secret"}),
        ("custom-call", "custom_command", {"argv": [str(node), "-e", "0"]}),
    ]

    class ScriptedProvider(ModelProvider):
        def capabilities(self, model):
            return ModelCapabilities()

        async def stream(self, request):
            if calls:
                call, name, args = calls.pop(0)
                yield ModelEvent(
                    kind=ModelEventKind.TOOL_CALL_DELTA,
                    tool_index=0,
                    tool_call_id=call,
                    tool_name=name,
                    arguments_delta=json.dumps(args),
                )
                yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="tool_calls")
            else:
                yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="finished")
                yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")

    class CustomCommand(RunCommandTool):
        name = "custom_command"

        async def execute(self, context, arguments):
            pytest.fail("unreviewed subclass must not be invoked")

    async def forbidden_read(*args, **kwargs):
        pytest.fail("host file implementation must not be invoked")

    monkeypatch.setattr(ReadFileTool, "execute", forbidden_read)
    config = target.config.model_copy(deep=True)
    config.storage.state_path = str(work.parent / "state.db")
    registry = ToolRegistry()
    for tool in (RunCommandTool(), ReadFileTool(), CustomCommand()):
        registry.register(tool)
    catalog = SkillCatalog(work.parent / "skills")
    catalog.scan()
    store = SQLiteSessionStore(Path(config.storage.state_path))
    try:
        runner = AgentRunner(
            config=config,
            workspace=work,
            provider=ScriptedProvider(),
            tool_registry=registry,
            policy=DefaultPolicyEngine(config.permissions, work),
            execution_target=target,
            skills=SkillManager(catalog),
            context=ContextAssembler(workspace=work, skill_catalog=catalog),
            store=store,
            event_bus=EventBus([store]),
            approval_handler=AllowApprovalHandler(),
        )
        result = await runner.run(RunRequest(prompt="exercise command and denied entrypoints"))
        assert result.status == "completed", result
        snapshots = await target.list_processes()
        assert len(snapshots) == 1
        process = snapshots[0]
        assert process.status == ProcessStatus.COMPLETED
        assert process.session_id == result.session_id
        assert process.scope.tool_call_id == "command-call"
        events = [
            e
            for e in store.list_events(result.session_id)
            if e["type"] == EventType.TOOL_COMPLETED.value
        ]
        assert len(events) == 3
        assert all(event["run_id"] == process.run_id for event in events)
        assert events[0]["payload"]["success"] is True
        for event in events[1:]:
            assert event["payload"]["success"] is False
        with sqlite3.connect(config.storage.state_path) as database:
            rows = database.execute(
                "SELECT tool_call_id, result_json FROM tool_runs WHERE session_id = ?",
                (result.session_id,),
            ).fetchall()
        records = {call: json.loads(raw) for call, raw in rows}
        for call in ("read-call", "custom-call"):
            assert records[call]["metadata"]["stage"] == "tool_boundary"
            assert records[call]["metadata"]["executed"] is False
        assert records["command-call"]["metadata"]["execution_scope"] == process.scope.model_dump(
            mode="json"
        )
        assert not target._bindings
    finally:
        store.close()
