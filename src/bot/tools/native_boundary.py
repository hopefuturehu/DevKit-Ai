"""Host-owned execution inventory; declarations are never supplied by a plugin."""

import json
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from bot.sandbox.models import SandboxError, SandboxErrorCode
from bot.tools.base import Tool
from bot.tools.builtins import (
    ApplyPatchTool,
    FetchUrlTool,
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
from bot.tools.plan import UpdatePlanTool


class ExecutionClass(StrEnum):
    CONFINED = "confined"
    BROKERED = "brokered"
    PURE = "pure"


@dataclass(frozen=True)
class ToolAudit:
    name: str
    execution_class: ExecutionClass
    supported: bool = True
    reason: str = ""

    def metadata(self) -> dict:
        return {"name": self.name, "execution_class": self.execution_class.value}


def _contract(tool) -> str:
    return json.dumps(
        {"schema": tool.input_schema, "annotations": tool.annotations.model_dump(mode="json")},
        sort_keys=True,
        ensure_ascii=False,
    )


_IMPLEMENTATIONS = MappingProxyType(
    {
        cls: (ToolAudit(cls.name, kind, supported, reason), cls.execute, _contract(cls))
        for cls, kind, supported, reason in (
            *(
                (cls, ExecutionClass.CONFINED, True, "")
                for cls in (
                    RunCommandTool,
                    RunShellTool,
                    ReadFileTool,
                    SearchTextTool,
                    ApplyPatchTool,
                    FetchUrlTool,
                )
            ),
            *(
                (cls, ExecutionClass.BROKERED, True, "")
                for cls in (
                    PollProcessTool,
                    SendProcessInputTool,
                    TerminateProcessTool,
                    ListProcessesTool,
                    UpdatePlanTool,
                )
            ),
            *(
                (cls, ExecutionClass.CONFINED, False, "宿主设备与额外权限尚未通过原生审计")
                for cls in (KsysTool, TunerTool)
            ),
        )
    }
)

# These actions are dispatched by fixed AgentRunner handlers, outside ToolRegistry.
# Keep pending actions explicit so a guessed name cannot reach their host implementation.
INTERNAL_TOOLS = MappingProxyType(
    {
        name: ToolAudit(name, ExecutionClass.BROKERED, supported, reason)
        for names, supported, reason in (
            (
                (
                    "activate_tools",
                    "load_context_reference",
                    "search_session_history",
                    "load_compaction_source",
                ),
                True,
                "",
            ),
            (
                ("activate_skill", "load_skill_resource"),
                False,
                "Skill 资源 broker 尚未通过原生审计",
            ),
            (("search_memory", "load_memory_evidence"), False, "记忆资源 broker 尚未通过原生审计"),
            (
                (
                    "task",
                    "send_task_message",
                    "spawn_agent",
                    "get_agent_status",
                    "await_agents",
                    "cancel_agent",
                    "apply_agent_patch",
                    "cleanup_agent_worktree",
                ),
                False,
                "子 Agent/worktree broker 尚未通过原生审计",
            ),
        )
        for name in names
    }
)


def _unavailable(message: str, stage: str) -> SandboxError:
    return SandboxError(SandboxErrorCode.UNAVAILABLE, message, stage=stage, executed=False)


def assert_native_tool_supported(tool: Tool, *, stage="tool_boundary") -> ToolAudit:
    entry = _IMPLEMENTATIONS.get(type(tool))
    if entry is None:
        raise _unavailable("工具实现尚未登记原生执行分类，拒绝未审计实现或子类", stage)
    audit, execute, contract = entry
    if not audit.supported:
        raise _unavailable(f"工具 {audit.name}: {audit.reason}", stage)
    try:
        unchanged = _contract(tool) == contract
    except (AttributeError, TypeError, ValueError):
        unchanged = False
    if (
        tool.name != audit.name
        or getattr(tool.execute, "__func__", None) is not execute
        or not unchanged
    ):
        raise _unavailable(f"工具 {audit.name} 的实现、名称或参数/权限声明已改变", stage)
    return audit


def describe_native_tool(name: str) -> ToolAudit | None:
    """Display classification only; never grants permission by a supplied name."""
    if name in INTERNAL_TOOLS:
        return INTERNAL_TOOLS[name]
    return next((entry[0] for entry in _IMPLEMENTATIONS.values() if entry[0].name == name), None)


def assert_native_internal_supported(name: str) -> ToolAudit:
    audit = INTERNAL_TOOLS.get(name)
    if audit is None or not audit.supported:
        reason = audit.reason if audit else "内部动作未登记执行分类"
        raise _unavailable(f"工具 {name}: {reason}", "tool_boundary")
    return audit


def native_tool_inventory() -> list[dict]:
    return [
        {
            **audit.metadata(),
            "supported": audit.supported,
            "reason": audit.reason,
            "dispatch": dispatch,
        }
        for audits, dispatch in (
            ((entry[0] for entry in _IMPLEMENTATIONS.values()), "registry"),
            (INTERNAL_TOOLS.values(), "internal"),
        )
        for audit in audits
    ]
