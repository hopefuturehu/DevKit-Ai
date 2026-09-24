import asyncio

import pytest

from bot.config.models import AppConfig
from bot.core import AgentRunner, RunRequest
from bot.core.context import ContextAssembler
from bot.core.events import EventBus, EventType
from bot.core.models import ModelCapabilities, ModelEvent, ModelEventKind
from bot.execution import LocalExecutionTarget
from bot.policy import DefaultPolicyEngine
from bot.providers import ModelProvider
from bot.sandbox.models import SandboxError, SandboxErrorCode
from bot.sessions import SQLiteSessionStore
from bot.skills import SkillCatalog, SkillManager
from bot.tools import ToolRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["ready", "sandbox", "error", "timeout", "cancel"])
async def test_run_waits_for_execution_startup_before_model_or_auxiliary_work(tmp_path, outcome):
    started, release = asyncio.Event(), asyncio.Event()
    actions = []

    class Target(LocalExecutionTarget):
        async def prepare_run(self):
            started.set()
            await release.wait()
            if outcome == "sandbox":
                raise SandboxError(
                    SandboxErrorCode.CLEANUP_FAILED, "unknown", stage="startup_recovery"
                )
            if outcome == "error":
                raise ValueError("startup failed")
            if outcome == "timeout":
                raise TimeoutError("startup deadline")
            actions.append("ready")

    class Provider(ModelProvider):
        def capabilities(self, model):
            return ModelCapabilities()

        async def stream(self, request):
            actions.append("model")
            yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="finished")
            yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")

    class Memory:
        def schedule(self, **kwargs):
            actions.append("memory")

    class Subagents:
        def definitions(self):
            return []

        async def start(self):
            actions.append("subagents")

        def collect_inbox(self, session_id):
            return []

        async def collect_required_results(self, session_id):
            return []

        async def cancel_required(self, *args):
            pass

        async def cancel_run(self, *args):
            pass

    config = AppConfig.model_validate({"model": {"name": "mock", "base_url": "https://unused"}})
    config.agent.finalization.enabled = True  # Startup failure must bypass even this model path.
    catalog = SkillCatalog(tmp_path / "skills")
    catalog.scan()
    target = Target()
    store = SQLiteSessionStore(tmp_path / "state.db")
    runner = AgentRunner(
        config=config,
        workspace=tmp_path,
        provider=Provider(),
        tool_registry=ToolRegistry(),
        policy=DefaultPolicyEngine(config.permissions, tmp_path),
        execution_target=target,
        skills=SkillManager(catalog),
        context=ContextAssembler(workspace=tmp_path, skill_catalog=catalog),
        store=store,
        event_bus=EventBus([store]),
        subagent_controller=Subagents(),
    )
    runner.memory_extractor = Memory()
    task = asyncio.create_task(runner.run(RunRequest(prompt="exercise startup")))
    try:
        await asyncio.wait_for(started.wait(), 5)
        assert actions == [] and not task.done()
        if outcome == "cancel":
            task.cancel()
        release.set()
        result = await asyncio.wait_for(task, 5)
        events = store.list_events(result.session_id)
        assert events[-1]["type"] == EventType.RUN_FINISHED.value
        if outcome == "ready":
            assert result.status == "completed"
            assert actions == ["ready", "subagents", "memory", "model"]
        else:
            assert result.status == ("cancelled" if outcome == "cancel" else "failed")
            assert actions == []
            if outcome != "cancel":
                final = next(e for e in events if e["type"] == EventType.RUN_FINALIZING.value)
                assert final["payload"]["model_finalizer"] is False
                if outcome == "sandbox":
                    assert final["payload"]["sandbox"] == {
                        "reason_code": "CLEANUP_FAILED",
                        "stage": "startup_recovery",
                        "executed": False,
                    }
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await target.aclose()
        store.close()
