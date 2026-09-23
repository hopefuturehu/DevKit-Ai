"""Audited native entrypoints while the production release gate is closed.

Classification is chosen by host implementation identity, never a name supplied
by a model, a project setting, or an arbitrary subclass claiming to be confined.
The worker and broker phases extend this inventory with their actual adapters.
"""

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


def assert_native_tool_supported(tool: Tool) -> None:
    if type(tool) not in {
        RunCommandTool,
        RunShellTool,
        PollProcessTool,
        SendProcessInputTool,
        TerminateProcessTool,
        ListProcessesTool,
        ReadFileTool,
        SearchTextTool,
        ApplyPatchTool,
        FetchUrlTool,
    }:
        raise SandboxError(
            SandboxErrorCode.UNAVAILABLE,
            f"工具 {tool.name} 尚未接入经过审计的原生执行路径",
            stage="tool_boundary",
        )
