from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, TextIO

from prompt_toolkit import PromptSession
from rich.console import Console
from rich.text import Text

from bot.core.approval import ApprovalResponse, ApprovalScope
from bot.core.events import AgentEvent, EventType
from bot.policy import PolicyDecision, ToolAction


def create_cli_console(*, file: TextIO | None = None, width: int | None = None) -> Console:
    """Create a portable console that never leaks ANSI escape sequences.

    CLI output is frequently consumed through a PTY by log collectors and web
    frontends that report themselves as terminals but don't interpret ANSI.
    Keep Markdown as plain source text and leave presentation to the consumer.
    """
    return Console(
        file=file,
        width=width,
        color_system=None,
        force_terminal=False,
        highlight=False,
    )


class RichEventSink:
    def __init__(self, console: Console | None = None, *, show_tool_output: bool = False) -> None:
        self.console = console or create_cli_console()
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
            line = Text()
            line.append("→ Tool", style="cyan")
            line.append(f" {payload.get('name')} ")
            line.append(self._short(payload.get("arguments")), style="dim")
            self.console.print(line, highlight=False)
        elif event.type == EventType.APPROVAL_REQUESTED:
            self._finish_stream()
            line = Text("需要批准：", style="yellow")
            line.append(str(payload.get("reason", "")), style=None)
            self.console.print(line, highlight=False)
        elif event.type == EventType.TOOL_COMPLETED:
            success = bool(payload.get("success"))
            line = Text("  ")
            line.append("完成" if success else "失败", style="green" if success else "red")
            line.append(f" {payload.get('name')}")
            self.console.print(line, highlight=False)
            if self.show_tool_output and payload.get("output"):
                self.console.print(str(payload["output"]), markup=False, highlight=False)
            if payload.get("error"):
                self.console.print(Text(f"  {payload['error']}", style="red"), highlight=False)
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
            line = Text("✓ Skill", style="magenta")
            line.append(f" {payload.get('name')} ({explicit})")
            self.console.print(line, highlight=False)
        elif event.type == EventType.RUN_STALL_WARNING:
            self._finish_stream()
            self.console.print(
                Text(f"进展警告：{payload.get('message')}", style="yellow"),
                highlight=False,
            )
        elif event.type == EventType.RUN_RECOVERY_STARTED:
            self._finish_stream()
            self.console.print(
                Text(
                    f"正在纠偏：{payload.get('message')} ",
                    style="yellow",
                ).append(f"attempt={payload.get('recovery_attempt')}", style="dim"),
                highlight=False,
            )
        elif event.type == EventType.RUN_FINALIZING:
            self._finish_stream()
            self.console.print(Text("运行即将结束，正在生成收尾说明…", style="yellow"))
        elif event.type == EventType.RUN_BLOCKED:
            self._finish_stream()
            self.console.print(
                Text(f"运行已阻塞：{payload.get('message')}", style="yellow"),
                highlight=False,
            )
        elif event.type == EventType.RUN_LIMIT_REACHED:
            self._finish_stream()
            self.console.print(
                Text(f"运行达到策略边界：{payload.get('error')}", style="yellow"),
                highlight=False,
            )
        elif event.type == EventType.RUN_CANCELLED:
            self._finish_stream()
            self.console.print(Text("运行已取消", style="yellow"), highlight=False)
        elif event.type == EventType.RUN_FAILED:
            self._finish_stream()
            self.console.print(
                Text(f"运行失败：{payload.get('error')}", style="red"), highlight=False
            )
        elif event.type == EventType.SUBAGENT_QUEUED:
            self._finish_stream()
            line = Text("⇢ Subagent", style="blue")
            line.append(f" {payload.get('agent')} ")
            line.append(f"{str(payload.get('task_id', ''))[:8]} queued", style="dim")
            self.console.print(line, highlight=False)
        elif event.type == EventType.SUBAGENT_COMPLETED:
            self._finish_stream()
            line = Text("✓ Subagent", style="green")
            line.append(f" {str(payload.get('task_id', ''))[:8]} completed", style="dim")
            self.console.print(line, highlight=False)
        elif event.type == EventType.SUBAGENT_BLOCKED:
            self._finish_stream()
            line = Text("! Subagent", style="yellow")
            line.append(f" {str(payload.get('task_id', ''))[:8]} blocked", style="dim")
            self.console.print(line, highlight=False)
        elif event.type == EventType.SUBAGENT_WAITING_APPROVAL:
            self._finish_stream()
            line = Text("⏸ Subagent", style="yellow")
            line.append(
                f" {str(payload.get('task_id', ''))[:8]} waiting approval: {payload.get('reason')}"
            )
            self.console.print(line, highlight=False)
        elif event.type in {
            EventType.SUBAGENT_FAILED,
            EventType.SUBAGENT_CANCELLED,
            EventType.SUBAGENT_INTERRUPTED,
        }:
            self._finish_stream()
            line = Text("✗ Subagent", style="red")
            line.append(
                f" {str(payload.get('task_id', ''))[:8]} {payload.get('error') or event.type.value}"
            )
            self.console.print(line, highlight=False)

    def _finish_stream(self) -> None:
        if self._streaming:
            self.console.print()
            self._streaming = False

    @classmethod
    def _short(cls, value: Any, limit: int = 180) -> str:
        text = repr(cls._summarize_multiline(value))
        return text if len(text) <= limit else text[: limit - 1] + "…"

    @classmethod
    def _summarize_multiline(cls, value: Any) -> Any:
        if isinstance(value, str):
            if "\n" in value or len(value) > 80:
                return f"<{len(value)} chars, {value.count(chr(10)) + 1} lines>"
            return value
        if isinstance(value, dict):
            return {key: cls._summarize_multiline(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._summarize_multiline(item) for item in value]
        return value


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
        if decision.approval_pattern is not None:
            prompt += f"\n复用规则：{decision.approval_pattern.description}"
        self.console.print(prompt, markup=False)
        aliases = {
            "": "once",
            "y": "once",
            "yes": "once",
            "once": "once",
            "s": "session",
            "session": "session",
            "a": "always",
            "always": "always",
            "n": "deny",
            "no": "deny",
            "deny": "deny",
        }
        while True:
            raw_answer = await prompt_session.prompt_async(
                "[approve: Y=本次/S=本会话/A=项目永久/N=拒绝；回车=Y] "
            )
            answer = aliases.get(raw_answer.strip().casefold())
            if answer is not None:
                break
            self.console.print("请输入 Y、S、A、N，或 once、session、always、deny。")
        if answer == "deny":
            response = ApprovalResponse(approved=False)
        else:
            response = ApprovalResponse(approved=True, scope=ApprovalScope(answer))
        if not pending.future.done():
            pending.future.set_result(response)
