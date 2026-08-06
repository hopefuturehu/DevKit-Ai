from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bot.compaction import ContextCompactor
from bot.config import AppConfig, ConfigError, load_config, resolve_api_key
from bot.core import AgentRunner
from bot.core.approval import ApprovalHandler
from bot.core.context import ContextAssembler
from bot.core.events import EventBus, EventSink
from bot.execution import LocalExecutionTarget
from bot.memory import MarkdownMemoryStore, MemoryExtractor
from bot.observability import Redactor
from bot.policy import DefaultPolicyEngine
from bot.providers import OpenAICompatibleProvider
from bot.sessions import SQLiteSessionStore
from bot.skills import SkillCatalog, SkillManager
from bot.subagents import BackgroundAgentPool, default_agent_specs
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
    memory_store: MarkdownMemoryStore | None = None
    memory_extractor: MemoryExtractor | None = None
    approval_handler: ApprovalHandler | None = None
    _closed: bool = field(default=False, init=False)

    def close(self) -> None:
        if not self._closed:
            if self.subagents.has_live_tasks:
                raise RuntimeError("存在运行中的后台子 Agent，请使用 await runtime.aclose()")
            if self.target.has_live_processes:
                raise RuntimeError("存在运行中的受管进程，请使用 await runtime.aclose()")
            if self.memory_extractor is not None and self.memory_extractor.has_live_task:
                raise RuntimeError("存在运行中的记忆提取任务，请使用 await runtime.aclose()")
            self.store.close()
            self._closed = True

    async def aclose(self) -> None:
        if self._closed:
            return
        errors: list[BaseException] = []
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
    api_key = resolve_api_key(config.model.api_key_ref)
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
        child_event_bus = EventBus([store], transform=redactor.redact_event)
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
        specs=default_agent_specs(config, tools),
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
        memory_store=memory_store,
        memory_extractor=memory_extractor,
        approval_handler=approval_handler,
    )
