from __future__ import annotations

import asyncio
from typing import Any

from rich.console import Console
from rich.prompt import Prompt

from bot.core.approval import ApprovalResponse, ApprovalScope
from bot.core.events import AgentEvent, EventType
from bot.policy import PolicyDecision, ToolAction


class RichEventSink:
    def __init__(self, console: Console | None = None, *, show_tool_output: bool = False) -> None:
        self.console = console or Console()
        self.show_tool_output = show_tool_output
        self._streaming = False

    async def publish(self, event: AgentEvent) -> None:
        payload = event.payload
        if event.type == EventType.ASSISTANT_DELTA:
            self.console.print(str(payload.get("text", "")), end="", markup=False, highlight=False)
            self._streaming = True
        elif event.type == EventType.ASSISTANT_MESSAGE:
            if self._streaming:
                self.console.print()
                self._streaming = False
        elif event.type == EventType.TOOL_REQUESTED:
            self._finish_stream()
            self.console.print(
                f"[cyan]→ Tool[/cyan] {payload.get('name')} "
                f"[dim]{self._short(payload.get('arguments'))}[/dim]"
            )
        elif event.type == EventType.APPROVAL_REQUESTED:
            self._finish_stream()
            self.console.print(f"[yellow]需要批准：[/yellow]{payload.get('reason')}")
        elif event.type == EventType.TOOL_COMPLETED:
            status = "[green]完成[/green]" if payload.get("success") else "[red]失败[/red]"
            self.console.print(f"  {status} {payload.get('name')}")
            if self.show_tool_output and payload.get("output"):
                self.console.print(str(payload["output"]), markup=False)
            if payload.get("error"):
                self.console.print(f"  [red]{payload['error']}[/red]")
        elif event.type == EventType.SKILL_ACTIVATED:
            self._finish_stream()
            explicit = "显式" if payload.get("explicit") else "自动"
            self.console.print(f"[magenta]✓ Skill[/magenta] {payload.get('name')} ({explicit})")
        elif event.type == EventType.RUN_FAILED:
            self._finish_stream()
            self.console.print(f"[red]运行失败：{payload.get('error')}[/red]")

    def _finish_stream(self) -> None:
        if self._streaming:
            self.console.print()
            self._streaming = False

    @staticmethod
    def _short(value: Any, limit: int = 180) -> str:
        text = repr(value)
        return text if len(text) <= limit else text[: limit - 1] + "…"


class InteractiveApprovalHandler:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    async def approve(self, action: ToolAction, decision: PolicyDecision) -> ApprovalResponse:
        prompt = (
            f"批准 Tool {action.tool_name}？\n"
            f"原因：{decision.reason}\n"
            f"参数：{action.arguments}\n"
            "选择 once/session/always/deny"
        )
        answer = await asyncio.to_thread(
            Prompt.ask,
            prompt,
            choices=["once", "session", "always", "deny"],
            default="deny",
            console=self.console,
        )
        if answer == "deny":
            return ApprovalResponse(approved=False)
        return ApprovalResponse(approved=True, scope=ApprovalScope(answer))
