from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from prompt_toolkit import PromptSession
from rich.console import Console

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
        elif event.type == EventType.TOOL_OUTPUT and self.show_tool_output:
            style = "dim red" if payload.get("stream") == "stderr" else "dim"
            self.console.print(
                str(payload.get("data", "")),
                end="",
                style=style,
                markup=False,
                highlight=False,
            )
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


@dataclass
class PendingApproval:
    action: ToolAction
    decision: PolicyDecision
    future: asyncio.Future[ApprovalResponse]


class InteractiveApprovalHandler:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._requests: asyncio.Queue[PendingApproval] = asyncio.Queue()

    async def approve(self, action: ToolAction, decision: PolicyDecision) -> ApprovalResponse:
        future: asyncio.Future[ApprovalResponse] = asyncio.get_running_loop().create_future()
        await self._requests.put(PendingApproval(action=action, decision=decision, future=future))
        return await future

    async def next_request(self) -> PendingApproval:
        return await self._requests.get()

    async def resolve(
        self,
        pending: PendingApproval,
        prompt_session: PromptSession[str],
    ) -> None:
        action = pending.action
        decision = pending.decision
        prompt = (
            f"批准 Tool {action.tool_name}？\n原因：{decision.reason}\n参数：{action.arguments}"
        )
        self.console.print(prompt, markup=False)
        while True:
            answer = (
                await prompt_session.prompt_async("[approve: once/session/always/deny] ")
            ).strip()
            if answer in {"once", "session", "always", "deny"}:
                break
            self.console.print("请输入 once、session、always 或 deny。")
        if answer == "deny":
            response = ApprovalResponse(approved=False)
        else:
            response = ApprovalResponse(approved=True, scope=ApprovalScope(answer))
        if not pending.future.done():
            pending.future.set_result(response)
