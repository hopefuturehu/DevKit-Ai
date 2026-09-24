"""Real managed Native backend checks; production factory stays closed."""

import asyncio
import json
import os
import platform
import shutil
import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio

from bot.config.models import AppConfig
from bot.execution.base import ProcessSpec, ProcessStatus
from bot.execution.native import NativeSandboxExecutionTarget
from bot.sandbox.models import SandboxError, SandboxErrorCode
from bot.sandbox.resources import OwnedDirectory
from bot.tools.base import ToolAnnotations, ToolContext
from bot.tools.builtins import RunCommandTool, RunShellTool
from bot.tools.subprocess_adapter import SubprocessCliTool

EXPECTED_UNRESOLVED = pytest.StashKey[set[str]]()


@pytest_asyncio.fixture
async def native_target(request, record_property):
    request.node.stash[EXPECTED_UNRESOLVED] = set()
    runtime = os.environ.get("BOT_TEST_SANDBOX_RUNTIME")
    if not runtime:
        if os.environ.get("BOT_REQUIRE_NATIVE_SANDBOX") == "1":
            pytest.fail("release validation requires BOT_TEST_SANDBOX_RUNTIME")
        pytest.skip("native runtime not selected")
    # pytest may delete old tmp_path roots on later runs. Unresolved native
    # resources must survive that rotation, so only verified teardown owns
    # removal of this separate test root.
    owned = OwnedDirectory.create(Path("/tmp").resolve(), prefix="bot-native-test-")
    tmp_path = owned.path
    work = tmp_path / "work"
    work.mkdir()
    config = AppConfig.model_validate({"sandbox": {"mode": "required"}})
    try:
        target = NativeSandboxExecutionTarget(
            config, workspace=work, runtime_root=Path(runtime), state_root=tmp_path / "runs"
        )
    except BaseException:
        owned.remove()  # Construction never starts a target or probe process.
        raise
    try:
        yield target, work, Path(runtime).resolve() / "node/bin/node"
    finally:
        try:
            await target.aclose()
        except RuntimeError as exc:
            record_property("retained_test_root", str(owned.path))
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
            owned.remove()


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
    assert result.execution_metadata["launch_status"] == {"state": "dispatched", "stage": "spawn"}
    assert result.execution_metadata["executed"] is None
    assert result.execution_metadata["capabilities"]["file_read"] == "enforced"
    assert result.execution_metadata["capabilities"]["network_filter"] == "enforced"
    assert result.execution_metadata["capabilities"]["process_isolation"] == "unverified"
    assert target.runtime.capabilities.file_read == "unverified"
    assert target.sandbox_capabilities.file_read == "enforced"
    control = Path(result.execution_metadata["control_directory"]["path"])
    assert not control.exists()
    assert Path(result.execution_metadata["run_directory"]["path"]).exists()
    assert not list(target.state_root.glob("preflight-*"))
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
async def test_native_refuses_write_grant_covering_real_loader_control_before_probe(native_target):
    target, _, _ = native_target
    controls = dict(target.runtime.loader.states)
    selected = controls["preload"] if platform.system() == "Linux" else controls["cache"]
    target.config.sandbox = target.config.sandbox.model_copy(
        update={"write_roots": (selected.path.parent,)}
    )
    with pytest.raises(SandboxError, match="加载器控制"):
        target.bind_scope(session_id="session", run_id="run", tool_call_id="call")
    assert target._preflight_gate.task is None
    assert not target.has_live_processes


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
@pytest.mark.parametrize("repeat_guard", [False, True])
async def test_agent_binds_native_identity_and_rejects_unconfined_tools(
    native_target, monkeypatch, repeat_guard
):
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
    (work / "secret").write_text("from-native-worker")
    calls = [
        ("command-call", "run_command", {"argv": [str(node), "-e", "console.log('agent-native')"]}),
        ("read-call", "read_file", {"path": "secret"}),
        ("custom-call", "custom_command", {"argv": [str(node), "-e", "0"]}),
    ]
    if repeat_guard:
        calls[2:2] = [
            (call, "read_file", {"path": "secret"})
            for call in ("read-2", "read-3", "read-after-edit")
        ]

    class ScriptedProvider(ModelProvider):
        def capabilities(self, model):
            return ModelCapabilities()

        async def stream(self, request):
            if calls:
                call, name, args = calls.pop(0)
                if call == "read-after-edit":
                    (work / "secret").write_text("after-external-edit")
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

    original_read = Path.read_bytes

    def no_host_content(path):
        if path == work / "secret":
            pytest.fail("worker file content must not be read by the host")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", no_host_content)
    config = target.config.model_copy(deep=True)
    config.agent.progress.repeat_guard_mode = "enforce"
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
        if repeat_guard:
            assert any(p.scope.tool_call_id.startswith("file-version-") for p in snapshots)
        else:
            assert len(snapshots) == 2
        process = snapshots[0]
        assert process.status == ProcessStatus.COMPLETED
        assert process.session_id == result.session_id
        assert process.scope.tool_call_id == "command-call"
        events = [
            e
            for e in store.list_events(result.session_id)
            if e["type"] == EventType.TOOL_COMPLETED.value
        ]
        assert len(events) == (6 if repeat_guard else 3)
        assert all(event["run_id"] == process.run_id for event in events)
        assert all(event["payload"]["success"] for event in events[:-1])
        assert events[-1]["payload"]["success"] is False
        with sqlite3.connect(config.storage.state_path) as database:
            rows = database.execute(
                "SELECT tool_call_id, result_json FROM tool_runs WHERE session_id = ?",
                (result.session_id,),
            ).fetchall()
        records = {call: json.loads(raw) for call, raw in rows}
        if repeat_guard:
            assert records["read-after-edit"]["output"] == "after-external-edit"
            assert not any(
                e["type"] == EventType.TOOL_REPEAT_BLOCKED.value
                for e in store.list_events(result.session_id)
            )
        assert records["read-call"]["output"] == "from-native-worker"
        assert records["read-call"]["metadata"]["execution_scope"]["tool_call_id"] == "read-call"
        for call in ("custom-call",):
            assert records[call]["metadata"]["stage"] == "tool_boundary"
            assert records[call]["metadata"]["executed"] is False
        assert records["command-call"]["metadata"]["execution_scope"] == process.scope.model_dump(
            mode="json"
        )
        assert not target._bindings
    finally:
        store.close()


@pytest.mark.asyncio
async def test_native_file_tools_share_os_boundary_and_hide_worker_controls(native_target):
    from bot.tools.builtins import ApplyPatchTool, FetchUrlTool, ReadFileTool, SearchTextTool

    target, work, _ = native_target
    outside = work.parent / "outside-secret"
    outside.write_text("external-sentinel")
    (work / "sample.txt").write_text("original line\n")
    (work / ".env").write_text("secret-sentinel")
    (work / "json.py").write_text("raise RuntimeError('project module injected')")
    (work / "outside-link").symlink_to(outside)
    cases = [
        (ReadFileTool(), {"path": "sample.txt"}, True, "original line"),
        (SearchTextTool(), {"query": "original", "glob": "*.txt"}, True, "sample.txt:1"),
        (
            ApplyPatchTool(),
            {"path": "sample.txt", "old_text": "original", "new_text": "changed"},
            True,
            "已更新",
        ),
        (ReadFileTool(), {"path": "sample.txt"}, True, "changed line"),
        (ReadFileTool(), {"path": str(outside)}, False, ""),
        (ReadFileTool(), {"path": "outside-link"}, False, ""),
        (ReadFileTool(), {"path": ".env"}, False, ""),
        (
            ApplyPatchTool(),
            {"path": str(outside), "old_text": "external", "new_text": "bad"},
            False,
            "",
        ),
        (FetchUrlTool(), {"url": "https://example.com"}, False, ""),
    ]
    for index, (tool, arguments, success, text) in enumerate(cases):
        view = invocation(target, work, call=f"worker-{index}")
        try:
            result = await tool.execute(
                ToolContext(
                    workspace=work,
                    execution_target=view,
                    session_id="session",
                    run_id="run",
                    # A permissive tool-layer path option cannot widen the OS policy.
                    workspace_only=False,
                ),
                arguments,
            )
            assert result.success is success, result
            assert text in result.output and "external-sentinel" not in result.output
            assert "secret-sentinel" not in result.output
            assert result.metadata["backend"] == "srt"
            assert result.metadata["execution_scope"]["tool_call_id"] == f"worker-{index}"
            assert await view.list_processes() == []
            latest = (await target.list_processes())[-1]
            assert latest.execution_metadata["purpose"] == "worker"
            with pytest.raises(SandboxError, match="不属于"):
                await view.poll_process(latest.process_id)
            assert not Path(latest.execution_metadata["control_directory"]["path"]).exists()
        finally:
            view.release_invocation()
    version = invocation(target, work, call="version")
    try:
        result = await version.worker_operation("file_version", {"path": "sample.txt"})
        assert result.success and result.metadata["sha256"]
    finally:
        version.release_invocation()
    readonly = invocation(target, work, call="readonly", run="readonly", read_only=True)
    try:
        result = await readonly.worker_operation(
            "apply_patch",
            {
                "path": "sample.txt",
                "old_text": "changed",
                "new_text": "bad",
            },
        )
        assert not result.success
    finally:
        readonly.release_invocation()
    assert (work / "sample.txt").read_text() == "changed line\n"
    assert outside.read_text() == "external-sentinel"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "asset",
    [
        "node/bin/node",
        "node_modules/zod/package.json",
        "host-executables.json",
        "loader-controls.json",
    ],
)
async def test_installed_runtime_tamper_refuses_launch_before_target_runs(
    native_target, tmp_path, asset
):
    original, work, _ = native_target
    runtime = tmp_path / "separate-runtime"
    shutil.copytree(original.runtime.root, runtime, symlinks=True)
    target = NativeSandboxExecutionTarget(
        original.config, workspace=work, runtime_root=runtime, state_root=tmp_path / "tamper-runs"
    )
    try:
        candidate = runtime / asset
        assert candidate.is_file()
        with candidate.open("ab") as stream:
            stream.write(b"\nchanged-after-verification\n")
        view = invocation(target, work, call="tampered-installation")
        try:
            with pytest.raises(SandboxError) as error:
                await view.start_process(
                    ProcessSpec(
                        argv=[
                            str(runtime / "node/bin/node"),
                            "-e",
                            "require('fs').writeFileSync('should-not-run','bad')",
                        ],
                        cwd=work,
                    )
                )
            assert error.value.executed is False
            assert not (work / "should-not-run").exists()
            assert await target.list_processes() == []
        finally:
            view.release_invocation()
    finally:
        await target.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unconfined", "protocol", "readonly_protocol", "leftover"])
async def test_preflight_failure_never_runs_business_command_or_retries(
    native_target, monkeypatch, failure
):
    from bot.execution.local import LocalExecutionTarget
    from bot.sandbox import preflight

    target, work, node = native_target
    leftover = None
    if failure == "unconfined":
        # Negative control: a functioning process with no OS isolation must
        # fail the real fixed checks, not produce enforced capabilities.
        monkeypatch.setattr(
            preflight._ProbeTarget, "_prepare_launch", LocalExecutionTarget._prepare_launch
        )
        monkeypatch.setattr(
            preflight._ProbeTarget, "_after_start", LocalExecutionTarget._after_start
        )
    elif failure == "protocol":
        monkeypatch.setattr(preflight, "_CHECK_JS", "console.log('{}')")
    elif failure == "readonly_protocol":
        monkeypatch.setattr(
            preflight,
            "_CANCEL_JS",
            "const c=JSON.parse(process.argv[1]);"
            "console.log(JSON.stringify({v:true,nonce:c.nonce,readonly:1}));"
            "setInterval(()=>{},1000)",
        )
    else:
        leftover = target.state_root / "preflight-synthetic-leftover"
        leftover.mkdir(mode=0o700)
    try:
        probe_task = None
        for index in range(2):
            with pytest.raises(SandboxError) as error:
                await target.start_process(
                    scoped_spec(
                        target,
                        work,
                        [node, "-e", "require('fs').writeFileSync('business-started','bad')"],
                        call=f"preflight-failure-{index}",
                    )
                )
            assert error.value.executed is False
            assert error.value.stage == "isolation_preflight"
            assert not (work / "business-started").exists()
            assert await target.list_processes() == []
            assert target.sandbox_capabilities.file_read == "unavailable"
            if probe_task is None:
                probe_task = target._preflight_gate.task
            assert target._preflight_gate.task is probe_task
        assert not target.has_live_processes
        if leftover is not None:
            assert leftover.is_dir()
            assert target._preflight_gate.instance.manager is None
    finally:
        if leftover is not None:
            # This test alone created the empty marker without starting any
            # process; production never clears arbitrary retained markers.
            leftover.rmdir()


@pytest.mark.asyncio
async def test_preflight_cancellation_reclaims_probe_and_never_runs_business(
    native_target, monkeypatch
):
    from bot.sandbox import preflight

    target, work, node = native_target
    launched = asyncio.Event()
    start = preflight._ProbeTarget.start_process

    async def observe(self, spec):
        pid = await start(self, spec)
        launched.set()
        return pid

    monkeypatch.setattr(preflight._ProbeTarget, "start_process", observe)
    monkeypatch.setattr(preflight, "_CHECK_JS", "setInterval(()=>{},1000)")
    task = asyncio.create_task(
        target.start_process(
            scoped_spec(
                target,
                work,
                [node, "-e", "require('fs').writeFileSync('business-started','bad')"],
            )
        )
    )
    async with asyncio.timeout(10):
        await launched.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not target.has_live_processes
    assert not (work / "business-started").exists()
    assert not list(target.state_root.glob("preflight-*"))
    assert await target.list_processes() == []


@pytest.mark.asyncio
async def test_trusted_launcher_policy_failure_proves_no_business_execution(
    native_target, monkeypatch
):
    target, work, node = native_target
    prepare = target._prepare_launch

    def corrupt_policy(spec):
        launch = prepare(spec)
        Path(launch.argv[6]).write_text("{invalid-policy")
        return launch

    monkeypatch.setattr(target, "_prepare_launch", corrupt_policy)
    with pytest.raises(SandboxError) as failure:
        await target.start_process(
            scoped_spec(
                target, work, [node, "-e", "require('fs').writeFileSync('business-started','bad')"]
            )
        )
    assert failure.value.code == SandboxErrorCode.LAUNCH_FAILED
    assert failure.value.stage == "launcher_policy" and failure.value.executed is False
    assert not target.has_live_processes and not (work / "business-started").exists()
    snapshot = (await target.list_processes())[0]
    assert snapshot.execution_metadata["launch_status"] == {"state": "failed", "stage": "policy"}
    assert snapshot.execution_metadata["executed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("high_descriptor", [False, True])
async def test_business_output_cannot_forge_status_or_inherit_status_pipe(
    native_target, monkeypatch, high_descriptor
):
    target, work, node = native_target
    prepare = target._prepare_launch
    reader_holds = []

    def identify_pipe(spec):
        launch = prepare(spec)
        startup = target._startups[spec.scope]
        # Keep the pipe object alive through the child's scan. Darwin's pipe
        # inode is a hash of a recyclable kernel address; a closed pipe's
        # inode can match a newly allocated, unrelated pipe in the child.
        # Holding only a non-inherited READ end does not delay protocol EOF.
        reader_holds.append(os.dup(startup.reader))
        assert not os.get_inheritable(reader_holds[-1])
        if high_descriptor:
            import fcntl

            # macOS commonly has RLIMIT_NOFILE=256, so fd 256 is invalid.
            minimum = 128 if platform.system() == "Darwin" else 256
            try:
                replacement = fcntl.fcntl(startup.writer, fcntl.F_DUPFD_CLOEXEC, minimum)
            except BaseException:
                launch.release_resources()
                raise
            os.close(startup.writer)
            startup.writer = replacement
            launch.pass_fds = (replacement,)
            launch.argv = (*launch.argv[:3], str(replacement), *launch.argv[4:])
        descriptor = launch.pass_fds[0]
        assert descriptor < 1024
        info = os.fstat(descriptor)
        launch.argv = (
            *launch.argv,
            str(descriptor),
            str(info.st_dev),
            str(info.st_ino),
            launch.argv[5],
        )
        return launch

    monkeypatch.setattr(target, "_prepare_launch", identify_pipe)
    script = r"""const fs=require('fs');
const [fd,dev,ino,id]=process.argv.slice(1);
const forged=JSON.stringify({v:1,id,state:'failed',stage:'policy'});
console.log(forged);console.error(forged);
let inherited=false;const matches=[];
// Check duplicates too: the launcher may reopen the same pipe on another fd.
for(let candidate=3;candidate<1024;candidate++)try{
  const s=fs.fstatSync(candidate,{bigint:true});
  if(s.dev.toString()===dev && s.ino.toString()===ino){
    inherited=true;
    const match={fd:candidate,mode:s.mode.toString(),isFIFO:s.isFIFO()};matches.push(match);
    try{match.written=fs.writeSync(candidate,forged+'\n')}catch(error){match.error=error.code}
  }
}catch{}
console.error(JSON.stringify({expected:{fd,dev,ino},matches}));
console.log(JSON.stringify({inherited}));"""
    try:
        pid = await target.start_process(scoped_spec(target, work, [node, "-e", script]))
        result = await target.poll_process(pid, wait_seconds=10)
    finally:
        for descriptor in reader_holds:
            os.close(descriptor)
    assert result.status == ProcessStatus.COMPLETED, result
    assert json.loads(result.stdout.splitlines()[-1]) == {"inherited": False}, result.stderr
    assert '"state":"failed"' in result.stdout and '"state":"failed"' in result.stderr
    assert result.execution_metadata["launch_status"] == {"state": "dispatched", "stage": "spawn"}
    assert result.execution_metadata["executed"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["stdout_only", "truncated", "wrong_id", "timeout"])
async def test_broken_launcher_channel_never_uses_stdout_or_assumes_nonexecution(
    native_target, monkeypatch, case
):
    from bot.sandbox import launch_status

    target, work, node = native_target
    await target.ensure_ready()
    monkeypatch.setattr(launch_status, "STARTUP_TIMEOUT", 0.3)
    prepare = target._prepare_launch
    channels = []

    def broken_launcher(spec):
        launch = prepare(spec)
        channel = target._startups[spec.scope]
        channels.append(channel)
        identifier = "wrong" if case == "wrong_id" else channel.invocation
        first = (
            json.dumps({"v": 1, "id": identifier, "state": "initializing", "stage": "initialize"})
            + "\n"
        )
        final = (
            json.dumps({"v": 1, "id": identifier, "state": "dispatched", "stage": "spawn"}) + "\n"
        )
        # Substitute only this trusted test launcher with fixed code. It never
        # executes the business argv, and stdout remains an untrusted channel.
        script = "const fs=require('fs');console.log(" + json.dumps(first + final) + ");"
        if case != "stdout_only":
            payload = first + final if case == "wrong_id" else first
            script += f"fs.writeSync({channel.writer}, {json.dumps(payload)});"
        if case == "timeout":
            script += "setInterval(()=>{},1000);"
        launch.argv = (str(node), "-e", script)
        return launch

    monkeypatch.setattr(target, "_prepare_launch", broken_launcher)
    with pytest.raises(SandboxError) as failure:
        await target.start_process(
            scoped_spec(
                target, work, [node, "-e", "require('fs').writeFileSync('business-started','bad')"]
            )
        )
    assert failure.value.code == SandboxErrorCode.LAUNCH_FAILED
    assert failure.value.stage == "launcher_status" and failure.value.executed is None
    assert not target.has_live_processes and not (work / "business-started").exists()
    assert all(pipe.reader is None and pipe.writer is None for pipe in channels)


@pytest.mark.asyncio
async def test_cancelling_status_wait_reclaims_registered_launcher(native_target, monkeypatch):
    target, work, node = native_target
    await target.ensure_ready()
    prepare = target._prepare_launch

    def waiting_launcher(spec):
        launch = prepare(spec)
        first = (
            json.dumps(
                {"v": 1, "id": launch.argv[5], "state": "initializing", "stage": "initialize"}
            )
            + "\n"
        )
        launch.argv = (
            str(node),
            "-e",
            f"require('fs').writeSync({launch.pass_fds[0]}, {json.dumps(first)});"
            "console.log('waiting');setInterval(()=>{},1000)",
        )
        return launch

    monkeypatch.setattr(target, "_prepare_launch", waiting_launcher)
    job = asyncio.create_task(target.start_process(scoped_spec(target, work, [node, "-e", "0"])))
    try:
        async with asyncio.timeout(5):
            while True:
                snapshots = await target.list_processes()
                if snapshots:
                    output = await target.read_process_output(snapshots[0].process_id)
                    if "waiting" in output["stdout"]:
                        break
                await asyncio.sleep(0.01)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert not target.has_live_processes and not target._startups
    finally:
        if not job.done():
            job.cancel()
        await asyncio.gather(job, return_exceptions=True)
