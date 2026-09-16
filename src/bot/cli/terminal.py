"""Terminal input and event-derived state; never owns the agent lifecycle."""

from __future__ import annotations

import hashlib
import os
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.filters import Condition, is_done, is_searching
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output.plain_text import PlainTextOutput
from prompt_toolkit.styles import Style

from bot.cli.commands import CommandCompleter
from bot.core.events import AgentEvent, EventType
from bot.observability import Redactor

STATUS_LABELS = {
    "requested": "等待执行",
    "running": "后台运行中",
    "executing": "执行中",
    "completed": "完成",
    "failed": "失败",
    "cancelled": "已取消",
    "blocked": "已阻塞",
    "denied": "已拒绝",
    "interrupted": "已中断",
    "timed_out": "已超时",
    "terminating": "正在终止",
    "cleanup_failed": "清理失败",
}


class DisplayMode(StrEnum):
    AUTO = "auto"
    TERMINAL = "terminal"
    PLAIN = "plain"


def terminal_enabled(mode: str, *, interactive: bool, json_output: bool = False) -> bool:
    if json_output or not interactive or mode == "plain":
        return False
    if mode == "terminal":
        return True
    return sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


class SafeHistory(FileHistory):
    """Do not retain recognized credentials, including in the suggestion cache."""

    def __init__(self, filename: Path, redactor: Redactor):
        self.redactor = redactor
        filename.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(filename, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        super().__init__(filename)

    def append_string(self, string: str) -> None:
        if string.strip() and self.redactor.redact_text(string) == string:
            try:
                super().append_string(string)
            except OSError:
                pass  # History is optional; a full disk must not lose submitted input.

    def load_history_strings(self):
        try:
            for string in super().load_history_strings():
                if self.redactor.redact_text(string) == string:
                    yield string
        except OSError:
            return


class SafeMemoryHistory(InMemoryHistory):
    def __init__(self, redactor: Redactor):
        self.redactor = redactor
        super().__init__()

    def append_string(self, string: str) -> None:
        if string.strip() and self.redactor.redact_text(string) == string:
            super().append_string(string)


@dataclass
class ToolView:
    number: int
    run_id: str
    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    status: str = "requested"
    started: float | None = None
    duration: float | None = None
    reference: str | None = None
    excerpt: str = ""
    error: str = ""
    truncated: bool = False
    reported: bool = False
    process_id: str | None = None
    output_streamed: bool = False


@dataclass
class TerminalState:
    session_id: str = ""
    phase: str = "就绪"
    started: float | None = None
    elapsed: float = 0
    context_tokens: int | None = None
    tools: OrderedDict[tuple[str, str], ToolView] = field(default_factory=OrderedDict)
    next_number: int = 1
    background: dict[str, str] = field(default_factory=dict)

    def select_session(self, session_id: str) -> None:
        if session_id != self.session_id:
            self.session_id = session_id
            self.phase, self.started, self.elapsed = "就绪", None, 0
            self.context_tokens = None
            self.tools.clear()
            self.background.clear()
            self.next_number = 1

    def observe(self, event: AgentEvent) -> ToolView | None:
        if event.session_id != self.session_id:
            return None
        kind, payload = event.type, event.payload
        phases = {
            EventType.MODEL_REQUEST_STARTED: "等待模型",
            EventType.ASSISTANT_DELTA: "生成回答",
            EventType.APPROVAL_REQUESTED: "等待审批",
            EventType.APPROVAL_RESOLVED: "继续执行",
            EventType.TOOL_STARTED: "执行工具",
            EventType.MODEL_REQUEST_RETRY: "重试模型",
            EventType.RUN_CANCEL_REQUESTED: "取消并清理",
            EventType.RUN_FINALIZING: "生成收尾说明",
            EventType.CONTEXT_COMPACTION_REQUEST_STARTED: "压缩上下文",
        }
        if kind == EventType.RUN_STARTED:
            self.phase, self.started = "准备上下文", time.monotonic()
        elif kind in phases:
            self.phase = phases[kind]
        elif kind in {
            EventType.RUN_COMPLETED,
            EventType.RUN_FAILED,
            EventType.RUN_CANCELLED,
            EventType.RUN_BLOCKED,
        }:
            self.phase = {
                EventType.RUN_COMPLETED: "完成",
                EventType.RUN_FAILED: "失败",
                EventType.RUN_CANCELLED: "已取消",
                EventType.RUN_BLOCKED: "已阻塞",
            }[kind]
        elif kind == EventType.RUN_FINISHED:
            status = str(payload.get("status", "completed"))
            self.phase = STATUS_LABELS.get(status, status)
            if self.started is not None:
                self.elapsed = time.monotonic() - self.started
                self.started = None
            for item in self.tools.values():
                if item.run_id == event.run_id and item.status in {"requested", "executing"}:
                    item.status = status if status in {"cancelled", "failed"} else "interrupted"
        elif kind == EventType.CONTEXT_COMPACTION_REQUEST_COMPLETED:
            self.phase = "准备上下文" if self.started is not None else "就绪"
        elif kind == EventType.CONTEXT_COMPACTION_REQUEST_FAILED:
            self.phase = "压缩失败"
        if kind == EventType.MODEL_REQUEST_STARTED:
            estimate = payload.get("input_token_estimate") or {}
            self.context_tokens = estimate.get("tokens")
        if kind.value.startswith("subagent.") and payload.get("task_id"):
            self.background[str(payload["task_id"])] = str(
                payload.get("summary") or kind.value.removeprefix("subagent.")
            )[:200]
            if len(self.background) > 100:
                del self.background[next(iter(self.background))]
        if kind in {EventType.PROCESS_UPDATED, EventType.PROCESS_CLEANUP_FINISHED} and payload.get(
            "process_id"
        ):
            for item in self.tools.values():
                if item.process_id == payload["process_id"]:
                    item.status = str(payload.get("status") or item.status)
        call_id = payload.get("tool_call_id")
        if not call_id or kind not in {
            EventType.TOOL_REQUESTED,
            EventType.TOOL_STARTED,
            EventType.TOOL_COMPLETED,
            EventType.TOOL_RESULT,
            EventType.TOOL_OUTPUT,
        }:
            return None
        key = (event.run_id, str(call_id))
        item = self.tools.get(key)
        if item is None:
            item = ToolView(self.next_number, event.run_id, str(call_id), str(payload.get("name")))
            self.next_number += 1
            self.tools[key] = item
            if len(self.tools) > 200:
                self.tools.popitem(last=False)
        if kind == EventType.TOOL_REQUESTED:
            item.arguments = payload.get("arguments") or {}
            item.started = event.timestamp.timestamp()
        elif kind == EventType.TOOL_STARTED:
            item.status, item.started = "executing", event.timestamp.timestamp()
        elif kind in {EventType.TOOL_COMPLETED, EventType.TOOL_RESULT}:
            item.status = payload.get("status") or (
                "completed" if payload.get("success") else "failed"
            )
            if payload.get("executed") is False and payload.get("reason_code") in {
                "policy_denied",
                "approval_denied",
            }:
                item.status = "denied"
            if item.duration is None and item.started is not None:
                item.duration = max(0, event.timestamp.timestamp() - item.started)
            item.reference = payload.get("context_ref") or item.reference
            item.excerpt = str(payload.get("output_excerpt") or "")[:4000]
            item.error = str(payload.get("error") or "")
            item.truncated = bool(payload.get("truncated"))
            item.process_id = payload.get("process_id") or item.process_id
            if item.process_id and payload.get("process_status"):
                for previous in self.tools.values():
                    if previous is not item and previous.process_id == item.process_id:
                        previous.status = str(payload["process_status"])
            self.phase = "处理工具结果"
        elif kind == EventType.TOOL_OUTPUT:
            item.excerpt = (item.excerpt + str(payload.get("data", "")))[-4000:]
        return item


class TerminalUI:
    def __init__(self, *, enhanced: bool):
        self.enhanced = enhanced
        self.state = TerminalState()
        self.runtime = None
        self.sink = None

    def toolbar(self):
        runtime, state = self.runtime, self.state
        elapsed = time.monotonic() - state.started if state.started is not None else state.elapsed
        model = runtime.config.model.name or "未配置模型"
        # Configured model names and other display values are untrusted text.
        model = runtime.runner.redactor.redact_text(model).replace("\n", " ")
        context = (
            f"上下文 ≈{state.context_tokens:,}/{runtime.config.model.context_window_tokens:,}"
            if state.context_tokens is not None
            else "上下文 —"
        )
        width = self.prompt_session.output.get_size().columns
        phase = "等待审批" if self.prompt_session.approval_mode else state.phase
        parts = [f" {phase} {elapsed:.0f}s", runtime.config.permissions.mode]
        if runtime.config.permissions.auto_approve:
            parts.append("自动审批")
        if width >= 65:
            parts += [model[:30], context]
        if width >= 115:
            parts += ["Tab 补全 · Alt+Enter 换行 · Ctrl+R 历史"]
        return [("class:bottom-toolbar", " │ ".join(parts))]

    def create_prompt(self, runtime, **kwargs):
        self.runtime = runtime
        history = SafeMemoryHistory(runtime.runner.redactor)
        if runtime.config.display.history:
            digest = hashlib.sha256(str(runtime.workspace.resolve()).encode()).hexdigest()[:16]
            try:
                history = SafeHistory(
                    Path.home() / ".bot" / "history" / f"{digest}.history",
                    runtime.runner.redactor,
                )
            except OSError:
                # A read-only home directory must not prevent running the CLI.
                history = SafeMemoryHistory(runtime.runner.redactor)
        bindings = KeyBindings()
        session: PromptSession[str]

        @bindings.add("enter", filter=~is_searching)
        def submit(event):
            event.current_buffer.validate_and_handle()

        @bindings.add("escape", "enter", filter=Condition(lambda: not session.approval_mode))
        def newline(event):
            event.current_buffer.insert_text("\n")

        @bindings.add(
            "tab",
            filter=Condition(lambda: self.enhanced and not session.approval_mode) & ~is_searching,
        )
        def complete(event):
            buffer = event.current_buffer
            if buffer.complete_state:
                buffer.complete_next()
                return
            choices = list(CommandCompleter().get_completions(buffer.document, CompleteEvent()))
            if len(choices) == 1:
                # Finish unique completion in this key event so a following
                # Enter cannot overtake an asynchronous completion task.
                buffer.apply_completion(choices[0])
            elif choices:
                buffer.start_completion(select_first=True)
            elif not buffer.text.startswith("/"):
                buffer.insert_text("    ")

        if not self.enhanced and "output" not in kwargs:
            kwargs["output"] = PlainTextOutput(sys.stdout)
        session = PromptSession(
            history=history,
            completer=CommandCompleter() if self.enhanced else None,
            complete_while_typing=self.enhanced,
            auto_suggest=AutoSuggestFromHistory() if self.enhanced else None,
            multiline=True,
            prompt_continuation="· ",
            key_bindings=bindings,
            reserve_space_for_menu=4 if self.enhanced else 0,
            refresh_interval=0.5 if self.enhanced else 0,
            style=Style.from_dict({"bottom-toolbar": "reverse", "completion-menu": "reverse"}),
            **kwargs,
        )
        session.approval_mode = False
        self.prompt_session = session
        if self.enhanced and runtime.config.display.progress:
            # Keep the footer with the input, without an alternate screen or a
            # CPR dependency (multiplexers/logging PTYs may not answer queries).
            session.layout.container = HSplit(
                [
                    session.layout.container,
                    ConditionalContainer(
                        Window(
                            FormattedTextControl(self.toolbar),
                            height=1,
                            style="class:bottom-toolbar",
                        ),
                        filter=~is_done,
                    ),
                ]
            )
        return session
