from __future__ import annotations

import asyncio
import json
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from prompt_toolkit import PromptSession
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from bot.cli.interrupts import PromptInterrupted, read_prompt
from bot.core.approval import ApprovalResponse, ApprovalScope
from bot.core.events import AgentEvent, EventType
from bot.observability import Redactor
from bot.policy import PolicyDecision, ToolAction


def create_cli_console(
    *,
    file: TextIO | None = None,
    width: int | None = None,
    enhanced: bool = False,
) -> Console:
    """ANSI is opt-in here; log/web consumers can explicitly select plain mode."""
    return Console(
        file=file,
        width=width,
        color_system="auto" if enhanced else None,
        force_terminal=enhanced,
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
        elif event.type == EventType.MODEL_REQUEST_RETRY:
            self._finish_stream()
            self.console.print(
                Text(
                    "模型请求中断，正在重试"
                    f"（{payload.get('retry_count')}/{payload.get('max_retries')}）；"
                    "此前未完成的输出已丢弃。",
                    style="yellow",
                ),
                highlight=False,
            )
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
            status = payload.get("status")
            label = {"running": "后台运行中", "cancelled": "已取消", "timed_out": "已超时"}.get(
                status, "完成" if success else "失败"
            )
            line = Text("  ")
            line.append(label, style="green" if success else "red")
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
        elif event.type == EventType.PLAN_UPDATED:
            self._finish_stream()
            explanation = payload.get("explanation")
            heading = "TODO list 已更新"
            if explanation:
                heading += f"：{explanation}"
            self.console.print(Text(heading, style="blue"), highlight=False)
            markers = {"pending": "○", "in_progress": "●", "completed": "✓"}
            for item in payload.get("items", []):
                status = str(item.get("status", "pending"))
                marker = markers.get(status, "○")
                style = "green" if status == "completed" else "cyan"
                self.console.print(
                    Text(f"  {marker} {item.get('content', '')}", style=style),
                    highlight=False,
                )
        elif event.type == EventType.SKILL_ACTIVATED:
            self._finish_stream()
            explicit = "显式" if payload.get("explicit") else "自动"
            line = Text("✓ Skill", style="magenta")
            line.append(f" {payload.get('name')} ({explicit})")
            self.console.print(line, highlight=False)
        elif event.type == EventType.CONTEXT_COMPACTION_REQUEST_STARTED:
            self._finish_stream()
            source_range = payload.get("source_range") or ["?", "?"]
            self.console.print(
                Text(
                    "压缩上下文："
                    f"{source_range[0]}–{source_range[-1]} "
                    f"{payload.get('phase')}，"
                    f"input≈{payload.get('planned_input_tokens', 0)} tokens",
                    style="cyan",
                ),
                highlight=False,
            )
        elif event.type == EventType.CONTEXT_COMPACTION_REQUEST_COMPLETED:
            self._finish_stream()
            self.console.print(
                Text(
                    "压缩请求完成："
                    f"{payload.get('phase')}，"
                    f"duration={float(payload.get('duration_ms') or 0) / 1000:.1f}s，"
                    f"output={payload.get('visible_summary_tokens', 0)} tokens",
                    style="green",
                ),
                highlight=False,
            )
        elif event.type == EventType.CONTEXT_COMPACTION_REQUEST_FAILED:
            self._finish_stream()
            self.console.print(
                Text(
                    f"压缩请求失败：{payload.get('error_class')}，{payload.get('error')}",
                    style="yellow",
                ),
                highlight=False,
            )
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
        elif event.type == EventType.SUBAGENT_WAITING_PARENT:
            self._finish_stream()
            line = Text("? Subagent", style="yellow")
            line.append(f" {str(payload.get('task_id', ''))[:8]} needs input", style="dim")
            questions = payload.get("questions") or []
            if questions:
                line.append(f": {questions[0]}")
            self.console.print(line, highlight=False)
        elif event.type == EventType.SUBAGENT_PROGRESS:
            self._finish_stream()
            line = Text("… Subagent", style="blue")
            line.append(f" {str(payload.get('task_id', ''))[:8]} ", style="dim")
            line.append(str(payload.get("summary") or "progress"))
            self.console.print(line, highlight=False)
        elif event.type == EventType.SUBAGENT_PATCH_APPLIED:
            self._finish_stream()
            self.console.print(
                f"[green]✓ Subagent patch[/green] {str(payload.get('task_id', ''))[:8]} applied"
            )
        elif event.type == EventType.SUBAGENT_WORKTREE_CLEANED:
            self._finish_stream()
            self.console.print(
                f"[dim]Subagent worktree {str(payload.get('task_id', ''))[:8]} cleaned[/dim]"
            )
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
    def __init__(self, console: Console | None = None, *, workspace: Path | None = None) -> None:
        self.console = console or Console()
        self.workspace = workspace.resolve() if workspace else None
        self.redactor = Redactor()
        self._requests: asyncio.Queue[PendingApproval] = asyncio.Queue()

    async def approve(self, action: ToolAction, decision: PolicyDecision) -> ApprovalResponse:
        future: asyncio.Future[ApprovalResponse] = asyncio.get_running_loop().create_future()
        await self._requests.put(PendingApproval(action=action, decision=decision, future=future))
        return await future

    async def next_request(self) -> PendingApproval:
        while True:
            pending = await self._requests.get()
            if not pending.future.done():
                return pending

    async def resolve(
        self,
        pending: PendingApproval,
        prompt_session: PromptSession[str],
        *,
        on_interrupt: Callable[[], None] | None = None,
    ) -> None:
        if pending.future.done():
            return
        try:
            await self._resolve_prompt(pending, prompt_session)
        except (PromptInterrupted, EOFError):
            if on_interrupt is not None:
                on_interrupt()
            raise
        finally:
            # EOF, interruption and shutdown must never strand an approval waiter.
            if not pending.future.done():
                pending.future.set_result(ApprovalResponse(approved=False))

    async def _resolve_prompt(
        self,
        pending: PendingApproval,
        prompt_session: PromptSession[str],
    ) -> None:
        action = pending.action
        decision = pending.decision
        pattern = decision.approval_pattern
        workspace = Path(pattern.workspace) if pattern else self.workspace
        cwd = action.arguments.get("cwd")
        directory = Path(str(cwd)).expanduser() if cwd else workspace
        if directory is not None and not directory.is_absolute() and workspace:
            directory = workspace / directory
        lines = [f"工具：{action.tool_name}", f"原因：{decision.reason}"]
        if action.session_id:
            lines.append(f"会话：{action.session_id[:8]} · 调用：{action.tool_call_id or '—'}")
        if directory is not None:
            lines.append(f"工作目录：{directory.resolve()}")
        argv = action.arguments.get("argv")
        if isinstance(argv, list):
            lines.append(f"完整命令：{shlex.join(str(value) for value in argv)}")
        script = action.arguments.get("script") or action.arguments.get("command")
        if script:
            lines.append(f"完整脚本：\n{script}")
        lines.append("完整参数：\n" + json.dumps(action.arguments, ensure_ascii=False, indent=2))
        if pattern is not None:
            lines.append(f"复用规则：{pattern.description}")
            lines.append(f"匹配类型：{pattern.kind.value} · 限定工作区：{pattern.workspace}")
            if pattern.command_prefix:
                lines.append("命令前缀：" + shlex.join(pattern.command_prefix))
                lines.append("前缀匹配可覆盖不同参数；具体范围以以上规则为准。")
            else:
                lines.append("精确匹配参数：" + json.dumps(pattern.arguments, ensure_ascii=False))
        else:
            lines.append("无可复用规则；S/A 也仅批准本次调用。")
        lines.append("Y 本次调用 · S 当前会话复用 · A 当前项目持久保存 · N 拒绝")
        content = Text(self.redactor.redact_text("\n".join(lines)))
        self.console.print(
            Panel(content, title="执行审批", border_style="yellow")
            if self.console.is_terminal
            else content
        )
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
            raw_answer = await read_prompt(
                prompt_session,
                "[approve: Y=本次/S=本会话/A=项目永久/N=拒绝；回车=Y] ",
                approval=True,
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
