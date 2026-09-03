from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bot.compaction import ContextCompactor
from bot.config import AppConfig, ConfigError, load_config, resolve_model_api_key
from bot.core import AgentRunner
from bot.core.approval import ApprovalHandler
from bot.core.context import ContextAssembler
from bot.core.events import CallbackEventSink, EventBus, EventSink, EventType
from bot.core.models import RunRequest
from bot.execution import LocalExecutionTarget
from bot.memory import MarkdownMemoryStore, MemoryExtractor
from bot.observability import Redactor
from bot.policy import DefaultPolicyEngine
from bot.providers import OpenAICompatibleProvider
from bot.sessions import SQLiteSessionStore
from bot.skills import SkillCatalog, SkillManager
from bot.subagents import AgentCatalog, BackgroundAgentPool
from bot.tools import ToolRegistry, register_builtin_tools
from bot.tools.kunpeng import register_kunpeng_tools


@dataclass
class Runtime:
    config: AppConfig
    workspace: Path
    catalog: SkillCatalog
    skills: SkillManager
    tools: ToolRegistry
    target: LocalExecutionTarget
    context: ContextAssembler
    store: SQLiteSessionStore
    compactor: ContextCompactor
    runner: AgentRunner
    subagents: BackgroundAgentPool
    agent_catalog: AgentCatalog | None = None
    memory_store: MarkdownMemoryStore | None = None
    memory_extractor: MemoryExtractor | None = None
    approval_handler: ApprovalHandler | None = None
    _auto_resume_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    _closed: bool = field(default=False, init=False)

    def close(self) -> None:
        if not self._closed:
            if self.subagents.has_live_tasks:
                raise RuntimeError("存在运行中的后台子 Agent，请使用 await runtime.aclose()")
            if self.target.has_live_processes:
                raise RuntimeError("存在运行中的受管进程，请使用 await runtime.aclose()")
            if self.memory_extractor is not None and self.memory_extractor.has_live_task:
                raise RuntimeError("存在运行中的记忆提取任务，请使用 await runtime.aclose()")
            if any(not task.done() for task in self._auto_resume_tasks.values()):
                raise RuntimeError("存在等待自动续跑的父 Agent，请使用 await runtime.aclose()")
            self.store.close()
            self._closed = True

    async def aclose(self) -> None:
        if self._closed:
            return
        errors: list[BaseException] = []
        auto_resume_tasks = list(self._auto_resume_tasks.values())
        for task in auto_resume_tasks:
            if not task.done():
                task.cancel()
        if auto_resume_tasks:
            await asyncio.gather(*auto_resume_tasks, return_exceptions=True)
        self._auto_resume_tasks.clear()
        try:
            await self.subagents.shutdown()
        except BaseException as exc:
            errors.append(exc)
        if self.memory_extractor is not None:
            try:
                await self.memory_extractor.shutdown()
            except BaseException as exc:
                errors.append(exc)
        try:
            await self.target.aclose()
        except BaseException as exc:
            errors.append(exc)
        if errors:
            primary, *additional = errors
            for error in additional:
                primary.add_note(f"额外的 Runtime 清理错误: {type(error).__name__}: {error}")
            raise primary
        self.store.close()
        self._closed = True


def build_runtime(
    *,
    workspace: Path,
    config_path: Path | None = None,
    event_sinks: list[EventSink] | None = None,
    approval_handler: ApprovalHandler | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> Runtime:
    workspace = workspace.resolve()
    config = load_config(workspace, config_path=config_path, overrides=config_overrides)
    if not config.model.name:
        raise ConfigError("model.name 未配置")
    api_key = resolve_model_api_key(config.model, workspace=workspace)
    provider = OpenAICompatibleProvider(
        base_url=config.model.base_url,
        api_key=api_key,
        timeout_seconds=config.model.timeout_seconds,
    )
    redactor = Redactor([api_key])
    target = LocalExecutionTarget(
        max_managed_processes=config.agent.max_managed_processes,
    )
    catalog = SkillCatalog(config.skill_path(workspace))
    catalog.scan()
    skills = SkillManager(catalog, max_auto_activated=config.skills.max_auto_activated)
    tools = ToolRegistry()
    register_builtin_tools(tools)
    register_kunpeng_tools(tools)
    store = SQLiteSessionStore(config.state_path(workspace), sanitizer=redactor.redact)
    event_bus = EventBus(
        [store, *(event_sinks or [])],
        transform=redactor.redact_event,
    )
    builtin_agents = Path(__file__).resolve().parents[1] / "assets" / "agents"
    try:
        project_agent_path = config.project_agent_path(workspace)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    project_digest = AgentCatalog.compute_project_digest(project_agent_path)
    project_trusted = store.is_agent_workspace_trusted(workspace, project_digest)
    agent_catalog = AgentCatalog(
        builtin_root=builtin_agents,
        user_root=config.user_agent_path(),
        project_root=project_agent_path,
        tools=tools,
        project_trusted=project_trusted,
        allow_worktree_writes=(
            config.subagents.allow_worktree_writes and config.permissions.mode != "read-only"
        ),
    )
    agent_catalog.scan()
    if not agent_catalog.agents:
        diagnostics = "; ".join(item.message for item in agent_catalog.diagnostics)
        raise ConfigError(f"没有可用 Agent 定义: {diagnostics or '未找到 Markdown'}")
    specs = agent_catalog.list()
    for spec in specs:
        step_limits = [
            value
            for value in (spec.max_steps, config.agent.max_steps, config.subagents.max_steps)
            if value is not None
        ]
        spec.max_steps = min(step_limits) if step_limits else None
        wall_time_limits = [
            value
            for value in (
                spec.max_wall_time_seconds,
                config.agent.max_wall_time_seconds,
                config.subagents.max_wall_time_seconds,
            )
            if value is not None
        ]
        spec.max_wall_time_seconds = min(wall_time_limits) if wall_time_limits else None
        configured_costs = [
            value
            for value in (
                spec.max_cost_usd,
                config.agent.max_cost_usd,
                config.subagents.max_cost_usd_per_task,
                config.subagents.max_total_cost_usd_per_session,
            )
            if value is not None
        ]
        spec.max_cost_usd = min(configured_costs) if configured_costs else None
    policy = DefaultPolicyEngine(config.permissions, workspace)
    context = ContextAssembler(
        workspace=workspace,
        skill_catalog=catalog,
        max_skill_catalog_chars=config.skills.max_catalog_chars,
    )
    memory_store: MarkdownMemoryStore | None = None
    if config.memory.enabled:
        memory_path = config.memory_path(workspace)
        try:
            workspace.relative_to(memory_path)
        except ValueError:
            pass
        else:
            raise ConfigError("memory.path 不能等于工作区或位于工作区上层")
        memory_store = MarkdownMemoryStore(
            memory_path,
            sanitizer=redactor.redact,
        )
        migrated = memory_store.import_legacy(store.list_memories())
        for memory_id in migrated:
            store.delete_memory(memory_id)
    protected_state_paths = (
        store.path,
        Path(f"{store.path}-wal"),
        Path(f"{store.path}-shm"),
        Path(f"{store.path}-journal"),
        *((memory_store.root,) if memory_store is not None else ()),
    )
    compactor = ContextCompactor(
        config=config,
        provider=provider,
        store=store,
        event_bus=event_bus,
    )

    def child_runner_factory(spec, child_workspace, child_approval_handler):
        child_config = config.model_copy(deep=True)
        if spec.model:
            child_config.model.name = spec.model
        child_config.agent.max_steps = spec.max_steps
        child_config.agent.max_wall_time_seconds = spec.max_wall_time_seconds
        child_config.agent.max_cost_usd = spec.max_cost_usd
        if spec.isolation.value == "read_only":
            child_config.permissions.mode = "read-only"
            child_config.permissions.network = "deny"
        child_tools = tools.subset(spec.allowed_tools)
        child_skills = SkillManager(
            catalog,
            max_auto_activated=config.skills.max_auto_activated,
        )
        child_context = ContextAssembler(
            workspace=child_workspace,
            skill_catalog=catalog,
            max_skill_catalog_chars=config.skills.max_catalog_chars,
        )
        child_policy = DefaultPolicyEngine(child_config.permissions, child_workspace)

        async def forward_child_progress(event):
            task = store.get_agent_task_by_child_session(event.session_id)
            if task is None or event.type not in {
                EventType.TOOL_STARTED,
                EventType.TOOL_COMPLETED,
                EventType.RUN_PROGRESS,
                EventType.ASSISTANT_MESSAGE,
            }:
                return
            if event.type == EventType.TOOL_STARTED:
                summary = f"正在调用 {event.payload.get('name') or event.payload.get('tool')}"
            elif event.type == EventType.TOOL_COMPLETED:
                summary = f"已完成 {event.payload.get('name') or event.payload.get('tool')}"
            elif event.type == EventType.ASSISTANT_MESSAGE:
                summary = "子 Agent 已生成阶段结果"
            else:
                summary = str(event.payload.get("summary") or "子 Agent 正在推进")
            await event_bus.emit(
                EventType.SUBAGENT_PROGRESS,
                session_id=str(task["parent_session_id"]),
                run_id=f"subagent:{task['id']}",
                payload={
                    "task_id": task["id"],
                    "child_session_id": task["child_session_id"],
                    "child_event": event.type.value,
                    "summary": summary,
                },
            )

        child_event_bus = EventBus(
            [store, CallbackEventSink(forward_child_progress)],
            transform=redactor.redact_event,
        )
        child_compactor = ContextCompactor(
            config=child_config,
            provider=provider,
            store=store,
            event_bus=child_event_bus,
        )
        return AgentRunner(
            config=child_config,
            workspace=child_workspace,
            provider=provider,
            tool_registry=child_tools,
            policy=child_policy,
            execution_target=target,
            skills=child_skills,
            context=child_context,
            store=store,
            event_bus=child_event_bus,
            approval_handler=child_approval_handler,
            redactor=redactor,
            context_compactor=child_compactor,
            denied_tool_paths=protected_state_paths,
            memory_store=memory_store,
        )

    subagents = BackgroundAgentPool(
        config=config,
        workspace=workspace,
        store=store,
        event_bus=event_bus,
        execution_target=target,
        specs=specs,
        runner_factory=child_runner_factory,
        approval_handler=approval_handler,
    )
    memory_extractor = (
        MemoryExtractor(
            config=config,
            workspace=workspace,
            provider=provider,
            store=store,
            memory_store=memory_store,
            event_bus=event_bus,
        )
        if memory_store is not None
        else None
    )
    runner = AgentRunner(
        config=config,
        workspace=workspace,
        provider=provider,
        tool_registry=tools,
        policy=policy,
        execution_target=target,
        skills=skills,
        context=context,
        store=store,
        event_bus=event_bus,
        approval_handler=approval_handler,
        redactor=redactor,
        subagent_controller=subagents if config.subagents.enabled else None,
        context_compactor=compactor,
        memory_store=memory_store,
        memory_extractor=memory_extractor,
        denied_tool_paths=protected_state_paths,
    )
    auto_resume_tasks: dict[str, asyncio.Task[None]] = {}
    if config.agents.auto_resume_background:

        async def auto_resume_parent(event) -> None:
            if event.type not in {
                EventType.SUBAGENT_COMPLETED,
                EventType.SUBAGENT_WAITING_PARENT,
                EventType.SUBAGENT_FAILED,
                EventType.SUBAGENT_BLOCKED,
                EventType.SUBAGENT_CANCELLED,
                EventType.SUBAGENT_INTERRUPTED,
            }:
                return
            task_id = str(event.payload.get("task_id") or "")
            record = store.get_agent_task(task_id)
            if record is None or record.get("execution") != "background":
                return
            parent_session_id = str(record["parent_session_id"])
            existing = auto_resume_tasks.get(parent_session_id)
            if existing is not None and not existing.done():
                return

            async def resume() -> None:
                await runner.wait_until_idle(parent_session_id)
                if not subagents.collect_inbox(parent_session_id):
                    return
                await runner.run(
                    RunRequest(
                        session_id=parent_session_id,
                        prompt=(
                            "后台子 Agent 已有新结果或问题。"
                            "请读取已自动投递的 Agent mailbox，继续当前任务。"
                        ),
                    )
                )

            scheduled = asyncio.create_task(
                resume(), name=f"agent-auto-resume-{parent_session_id[:8]}"
            )
            auto_resume_tasks[parent_session_id] = scheduled

            def completed(done: asyncio.Task[None]) -> None:
                if auto_resume_tasks.get(parent_session_id) is done:
                    auto_resume_tasks.pop(parent_session_id, None)
                if not done.cancelled():
                    done.exception()

            scheduled.add_done_callback(completed)

        event_bus.add_sink(CallbackEventSink(auto_resume_parent))
    return Runtime(
        config=config,
        workspace=workspace,
        catalog=catalog,
        skills=skills,
        tools=tools,
        target=target,
        context=context,
        store=store,
        compactor=compactor,
        runner=runner,
        subagents=subagents,
        agent_catalog=agent_catalog,
        memory_store=memory_store,
        memory_extractor=memory_extractor,
        approval_handler=approval_handler,
        _auto_resume_tasks=auto_resume_tasks,
    )
