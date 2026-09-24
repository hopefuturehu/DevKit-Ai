import json
from types import MethodType, SimpleNamespace

import pytest

from bot.config.models import AppConfig
from bot.core import AgentRunner, RunRequest
from bot.core.context import ContextAssembler
from bot.core.events import EventBus, EventType
from bot.core.models import (
    ChatMessage,
    ModelCapabilities,
    ModelEvent,
    ModelEventKind,
    Role,
    ToolCall,
)
from bot.execution import EnvironmentCapabilities, LocalExecutionTarget, ProcessStatus
from bot.policy import DefaultPolicyEngine
from bot.providers import ModelProvider
from bot.sandbox.models import SandboxError
from bot.sessions import SQLiteSessionStore
from bot.skills import SkillCatalog, SkillManager
from bot.tools import ToolRegistry, register_builtin_tools
from bot.tools.builtins import ReadFileTool, RunCommandTool
from bot.tools.kunpeng import KsysTool, TunerTool
from bot.tools.native_boundary import native_tool_inventory
from bot.tools.plan import UpdatePlanTool


class Unreviewed(ReadFileTool):
    execution_class = "pure"

    async def execute(self, context, arguments):
        pytest.fail("an unreviewed implementation must never execute")


def test_required_registry_checks_implementation_and_child_registration():
    registry = ToolRegistry(requires_confinement=True)
    register_builtin_tools(registry)
    child = registry.subset(["read_file", "update_plan"])
    assert child.requires_confinement
    for selected in (registry, child):
        for tool in (Unreviewed(), KsysTool(), TunerTool()):
            with pytest.raises(SandboxError) as error:
                selected.register(tool)
            assert error.value.stage == "tool_registration" and error.value.executed is False
    inventory = {item["name"]: item for item in native_tool_inventory()}
    assert {inventory[name]["execution_class"] for name in registry.names()} == {
        "confined",
        "brokered",
    }
    assert not inventory["ksys"]["supported"]
    assert not inventory["load_skill_resource"]["supported"]


def test_existing_registry_cannot_be_upgraded_with_an_unreviewed_tool():
    registry = ToolRegistry()
    registry.register(Unreviewed())
    with pytest.raises(SandboxError):
        registry.require_confinement()
    assert not registry.requires_confinement


@pytest.mark.parametrize("mutation", ["name", "execute", "schema", "annotations"])
@pytest.mark.parametrize("access", ["definitions", "get", "subset"])
def test_post_registration_changes_are_rejected(mutation, access):
    registry = ToolRegistry(requires_confinement=True)
    tool = ReadFileTool()
    registry.register(tool)
    if mutation == "name":
        tool.name = "load_context_reference"
    elif mutation == "execute":
        tool.execute = MethodType(Unreviewed.execute, tool)
    elif mutation == "schema":
        tool.input_schema = {"type": "object"}
    else:
        tool.annotations = tool.annotations.model_copy(update={"read_only": False})
    with pytest.raises(SandboxError, match="已改变"):
        if access == "get":
            registry.get("read_file")
        elif access == "subset":
            registry.subset(["read_file"])
        else:
            registry.definitions()


@pytest.fixture
def runner(tmp_path):
    class Target(LocalExecutionTarget):
        @property
        def requires_confinement(self):
            return True

        async def probe(self, executables=None):
            return EnvironmentCapabilities(operating_system="fixture", architecture="fixture")

        async def start_process(self, spec):
            pytest.fail("session broker tests must not launch a process")

    class Provider(ModelProvider):
        def __init__(self):
            self.calls = []
            self.requests = []

        def capabilities(self, model):
            return ModelCapabilities()

        async def stream(self, request):
            self.requests.append(request)
            if self.calls:
                call = self.calls.pop(0)
                yield ModelEvent(
                    kind=ModelEventKind.TOOL_CALL_DELTA,
                    tool_index=0,
                    tool_call_id=call.id,
                    tool_name=call.name,
                    arguments_delta=json.dumps(call.arguments),
                )
                yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="tool_calls")
            else:
                yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="finished")
                yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")

    work = tmp_path / "work"
    work.mkdir()
    config = AppConfig.model_validate(
        {
            "model": {"name": "fixture"},
            "sandbox": {"mode": "required"},
            "agent": {"finalization": {"enabled": False}},
        }
    )
    catalog = SkillCatalog(tmp_path / "skills")
    catalog.scan()
    registry = ToolRegistry()
    registry.register(UpdatePlanTool())
    registry.register(RunCommandTool())
    store = SQLiteSessionStore(tmp_path / "state.db")
    item = AgentRunner(
        config=config,
        workspace=work,
        provider=Provider(),
        tool_registry=registry,
        policy=DefaultPolicyEngine(config.permissions, work),
        execution_target=Target(),
        skills=SkillManager(catalog),
        context=ContextAssembler(workspace=work, skill_catalog=catalog),
        store=store,
        event_bus=EventBus([store]),
    )
    try:
        yield item
    finally:
        store.close()


def call(name, **arguments):
    return ToolCall(id="call", name=name, arguments=arguments)


def active_scope(runner):
    session = runner.store.create_session(runner.workspace)
    runner.store.start_run(session, "active")
    return session, "active"


@pytest.mark.parametrize("state", ["foreign_session", "foreign_workspace", "finished", "missing"])
def test_broker_scope_rejects_wrong_or_retired_ownership(runner, state):
    session, run = active_scope(runner)
    if state == "foreign_session":
        session = runner.store.create_session(runner.workspace)
    elif state == "foreign_workspace":
        runner.store.update_session_workspace(session, runner.workspace.parent)
    elif state == "finished":
        runner.store.finish_run(run, "completed")
    else:
        run = "missing"
    result = runner._native_tool_preflight(
        call("search_session_history", query="secret"), session, run
    )
    assert not result.success and result.metadata["stage"] == "broker_scope"
    assert result.metadata["executed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_call",
    [
        call("activate_tools", names=["update_plan"], path="/etc/passwd"),
        call("search_session_history", query="secret", session_id="victim"),
        call("search_session_history", query="secret", limit=True),
        call("load_context_reference", reference="blob:" + "a" * 64, run_id="victim"),
        call("load_compaction_source", compaction_id="a" * 32, end_position=False),
        call("load_memory_evidence", memory="private"),
        call("load_skill_resource", skill="x", path="/etc/passwd"),
        call("spawn_agent", task="bypass"),
        call(KsysTool.name, action="inspect"),
        call(TunerTool.name, action="inspect"),
        call("update_plan", items=[], session_id="victim"),
    ],
)
async def test_model_dispatch_rejects_scope_injection_and_pending_brokers(runner, tool_call):
    runner.provider.calls = [tool_call]
    result = await runner.run(RunRequest(prompt="exercise broker boundary"))
    assert result.status == "completed", result
    events = [
        e
        for e in runner.store.list_events(result.session_id)
        if e["type"] == EventType.TOOL_RESULT.value
    ]
    assert len(events) == 1 and not events[0]["payload"]["success"]
    assert events[0]["payload"]["executed"] is False
    assert runner.store.load_plan(result.session_id) is None
    assert not runner.execution_target.has_live_processes


@pytest.mark.asyncio
async def test_native_session_plan_and_blob_grants_use_only_current_session(runner):
    owner = runner.store.create_session(runner.workspace)
    victim = runner.store.create_session(runner.workspace)
    granted = runner.store.put_context_blob(session_id=owner, run_id="old", content="owner-value")
    private = runner.store.put_context_blob(session_id=victim, run_id="old", content="victim-value")
    runner.provider.calls = [
        ToolCall(id="own", name="load_context_reference", arguments={"reference": granted}),
        ToolCall(id="foreign", name="load_context_reference", arguments={"reference": private}),
        ToolCall(
            id="plan",
            name="update_plan",
            arguments={"items": [{"content": "current", "status": "completed"}]},
        ),
    ]
    result = await runner.run(RunRequest(session_id=owner, prompt="read and plan"))
    assert result.status == "completed", result
    events = [
        e for e in runner.store.list_events(owner) if e["type"] == EventType.TOOL_RESULT.value
    ]
    assert [e["payload"]["success"] for e in events] == [True, False, True]
    assert all(e["payload"]["execution_class"] == "brokered" for e in events)
    assert "owner-value" in events[0]["payload"]["output_excerpt"]
    assert "victim-value" not in json.dumps(events)
    assert runner.store.load_plan(owner)["items"][0]["content"] == "current"
    assert runner.store.load_plan(victim) is None
    assert not runner.execution_target.has_live_processes


@pytest.mark.asyncio
async def test_session_history_output_is_bounded_and_cannot_select_other_sessions(runner):
    owner = runner.store.create_session(runner.workspace)
    victim = runner.store.create_session(runner.workspace)
    for session, content in ((owner, "needle " + "字" * 40_000), (victim, "needle victim-value")):
        runner.store.append_message(session, "old", ChatMessage(role=Role.USER, content=content))
    runner.config.agent.max_tool_output_bytes = 512
    runner.provider.calls = [call("search_session_history", query="needle")]
    result = await runner.run(RunRequest(session_id=owner, prompt="search"))
    assert result.status == "completed", result
    events = [
        e for e in runner.store.list_events(owner) if e["type"] == EventType.TOOL_RESULT.value
    ]
    assert events[0]["payload"]["success"] and events[0]["payload"]["truncated"]
    blob = runner.store.read_context_blob(owner, events[0]["payload"]["context_ref"])
    assert len(blob["content"].encode()) <= 512
    assert "victim-value" not in blob["content"]


def test_compaction_broker_checks_sqlite_session_grant_without_calling_delegate(runner):
    owner, run = active_scope(runner)
    victim = runner.store.create_session(runner.workspace)
    runner.store.append_message(victim, "old", ChatMessage(role=Role.USER, content="victim-value"))
    compaction = runner.store.start_context_compaction(
        session_id=victim,
        parent_id=None,
        trigger="test",
        model="fixture",
        covered_start_position=1,
        covered_end_position=1,
        delta_start_position=1,
        source_sha256="a" * 64,
        anchor_positions=[],
        source_chars=12,
    )
    runner.context_compactor = SimpleNamespace(
        read_source=lambda **k: pytest.fail("must use fixed SQL broker")
    )
    query = call("load_compaction_source", compaction_id=compaction)
    assert runner._native_tool_preflight(query, owner, run) is None
    result = runner._load_compaction_source(query, owner)
    assert not result.success and "victim-value" not in result.model_content()


def test_large_invalid_argument_errors_are_bounded_without_marking_them_executed(runner):
    session, run = active_scope(runner)
    query = call("search_session_history", query="字" * 100_000)
    result = runner._native_tool_preflight(query, session, run)
    assert result.metadata["executed"] is False
    bounded = runner._bound_native_broker_result(result)
    assert bounded.truncated and len(bounded.error.encode()) <= 64_000
    assert bounded.metadata["executed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership", ["own", "foreign_session", "foreign_run", "unscoped"])
async def test_auxiliary_process_notes_and_repeat_observation_are_run_scoped(
    runner, monkeypatch, ownership
):
    session, run = active_scope(runner)
    snapshot = SimpleNamespace(
        process_id="process",
        session_id=session,
        run_id=run,
        status=ProcessStatus.RUNNING,
    )
    if ownership == "foreign_session":
        snapshot.session_id = "other"
    elif ownership == "foreign_run":
        snapshot.run_id = "other"
    elif ownership == "unscoped":
        snapshot.session_id = snapshot.run_id = None

    async def snapshots():
        return [snapshot]

    monkeypatch.setattr(runner.execution_target, "list_processes", snapshots)
    notes = []
    await runner._refresh_managed_process_note(notes, session_id=session, run_id=run)
    assert bool(notes) is (ownership == "own")
    observed = []
    controller = SimpleNamespace(
        has_pending_process=lambda pid: observed.append(pid),
        repeat_guard=SimpleNamespace(records={}),
    )
    assert (
        await runner._repeat_preflight(
            call("run_command", argv=["true"]),
            session,
            run,
            controller,
            cursor=0,
            inspect_resource=True,
        )
        is None
    )
    assert observed == (["process"] if ownership == "own" else [])


@pytest.mark.asyncio
async def test_idle_native_compaction_rejects_foreign_workspace_before_backend_or_model(
    runner, monkeypatch
):
    session = runner.store.create_session(runner.workspace.parent)

    def forbidden(*args, **kwargs):
        pytest.fail("foreign session cannot trigger backend or compactor work")

    runner.context_compactor = SimpleNamespace(projection=forbidden)
    monkeypatch.setattr(runner.execution_target, "prepare_run", forbidden)
    with pytest.raises(SandboxError) as error:
        await runner.compact_session(session)
    assert error.value.stage == "broker_scope"
    assert runner.provider.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("component", ["memory", "skill", "subagent", "registry"])
async def test_pending_services_and_late_registration_fail_before_startup_or_model(
    runner, monkeypatch, component
):
    def forbidden(*args, **kwargs):
        pytest.fail("startup/model/auxiliary work must not happen")

    monkeypatch.setattr(runner.execution_target, "prepare_run", forbidden)
    if component == "memory":
        runner.memory_extractor = SimpleNamespace(schedule=forbidden)
    elif component == "skill":
        # A catalog change is host-visible configuration, not a grant for a broker.
        runner.skills.catalog.skills["pending"] = SimpleNamespace(model_copy=lambda **k: None)
    elif component == "subagent":
        runner.subagent_controller = SimpleNamespace(
            start=forbidden,
            cancel_run=lambda *a: None,
            cancel_required=lambda *a: None,
        )
    else:
        runner.tool_registry.get("update_plan").name = "changed"
    if component == "subagent":
        # Run termination uses the controller's async cancellation hooks even
        # when its startup was denied. They must not launch any child work.
        async def no_work(*args):
            return None

        runner.subagent_controller.cancel_run = no_work
        runner.subagent_controller.cancel_required = no_work
    result = await runner.run(RunRequest(prompt="must fail closed"))
    assert result.status == "failed", result
    assert runner.provider.requests == []
