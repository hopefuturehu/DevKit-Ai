from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bot.config import AppConfig, ConfigError, load_config, resolve_api_key
from bot.core import AgentRunner
from bot.core.approval import ApprovalHandler
from bot.core.context import ContextAssembler
from bot.core.events import EventBus, EventSink
from bot.execution import LocalExecutionTarget
from bot.observability import Redactor
from bot.policy import DefaultPolicyEngine
from bot.providers import OpenAICompatibleProvider
from bot.sessions import SQLiteSessionStore
from bot.skills import SkillCatalog, SkillManager
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
    store: SQLiteSessionStore
    runner: AgentRunner

    def close(self) -> None:
        self.store.close()


def build_runtime(
    *,
    workspace: Path,
    config_path: Path | None = None,
    event_sinks: list[EventSink] | None = None,
    approval_handler: ApprovalHandler | None = None,
) -> Runtime:
    workspace = workspace.resolve()
    config = load_config(workspace, config_path=config_path)
    if not config.model.name:
        raise ConfigError("model.name 未配置")
    api_key = resolve_api_key(config.model.api_key_ref)
    provider = OpenAICompatibleProvider(
        base_url=config.model.base_url,
        api_key=api_key,
        timeout_seconds=config.model.timeout_seconds,
    )
    redactor = Redactor([api_key])
    target = LocalExecutionTarget()
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
    )
    return Runtime(
        config=config,
        workspace=workspace,
        catalog=catalog,
        skills=skills,
        tools=tools,
        target=target,
        store=store,
        runner=runner,
    )
