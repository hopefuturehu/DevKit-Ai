"""Scrollback rendering. PromptSession remains the only live terminal owner."""

from __future__ import annotations

import json
import re
import shlex
from difflib import unified_diff

from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from bot.cli.render import RichEventSink
from bot.cli.terminal import STATUS_LABELS, TerminalUI, ToolView
from bot.core.events import AgentEvent, EventType


class InteractiveEventSink(RichEventSink):
    def __init__(self, console, ui: TerminalUI, *, show_tool_output=False):
        super().__init__(console, show_tool_output=show_tool_output)
        self.ui = ui
        self._markdown = ""
        self._had_delta = False
        self._raw_remainder = False
        self._tool_streaming = False

    async def publish(self, event: AgentEvent) -> None:
        if event.session_id != self.ui.state.session_id:
            # Children have their own transcript. Parent subagent events provide
            # their lifecycle and progress without interleaving assistant text.
            return
        item = self.ui.state.observe(event)
        kind, payload = event.type, event.payload
        if kind == EventType.MODEL_REQUEST_STARTED:
            self._finish_stream()
            self._had_delta = False
        if self.ui.enhanced and kind == EventType.ASSISTANT_DELTA:
            self._had_delta = True
            self._markdown += str(payload.get("text", ""))
            self._flush_paragraphs()
            return
        if self.ui.enhanced and kind == EventType.ASSISTANT_MESSAGE:
            if not self._had_delta:
                self._markdown = str(payload.get("content") or payload.get("text") or "")
            self._finish_stream()
            self._had_delta = False
            return
        if kind == EventType.TOOL_REQUESTED and item:
            self._finish_stream()
            summary = self._action(item)
            self.console.print(Text(f"→ [{item.number}] {item.name} · {summary}", style="cyan"))
            return
        if kind in {EventType.TOOL_COMPLETED, EventType.TOOL_RESULT} and item:
            if not item.reported:
                self._finish_stream()
                status = STATUS_LABELS.get(item.status, item.status)
                duration = f" · {item.duration:.1f}s" if item.duration is not None else ""
                style = (
                    "red"
                    if item.status == "failed"
                    else "cyan"
                    if (item.status == "running")
                    else "green"
                    if item.status == "completed"
                    else "yellow"
                )
                self.console.print(
                    Text(
                        f"  [{item.number}] {status}{duration} · /details {item.number}",
                        style=style,
                    )
                )
                if item.error:
                    excerpt = "\n".join(item.error.splitlines()[-6:])[-1000:]
                    self.console.print(Text(excerpt, style="red"))
                    if item.excerpt and item.excerpt != item.error:
                        self.console.print(
                            Text(
                                "\n".join(item.excerpt.splitlines()[-4:])[-600:],
                                style="dim red",
                            )
                        )
                elif item.excerpt and item.status != "running":
                    excerpt = " ".join(item.excerpt.split())[:180]
                    self.console.print(Text(f"  {excerpt}", style="dim"))
                if self.show_tool_output and not item.output_streamed:
                    runtime = self.ui.runtime
                    blob = (
                        runtime.store.read_context_blob(
                            self.ui.state.session_id,
                            item.reference,
                            limit=16_000,
                        )
                        if runtime and item.reference
                        else None
                    )
                    if blob and not blob["eof"]:
                        blob = runtime.store.read_context_blob(
                            self.ui.state.session_id,
                            item.reference,
                            limit=blob["byte_count"],
                        )
                    self.console.print(
                        blob["content"] if blob else item.excerpt,
                        markup=False,
                        highlight=False,
                    )
                item.reported = True
            return
        if kind == EventType.TOOL_OUTPUT:
            # Retain a bounded tail for details while running; completion can
            # load the persisted result, so a full log is never kept in RAM.
            if self.show_tool_output:
                if not self._tool_streaming:
                    self._finish_stream()
                if item:
                    item.output_streamed = True
                await super().publish(event)
                self._tool_streaming = not str(payload.get("data", "")).endswith("\n")
            return
        if kind == EventType.SUBAGENT_PROGRESS:
            return
        if kind == EventType.MODEL_REQUEST_RETRY:
            self._finish_stream()
        await super().publish(event)
        if kind in {EventType.RUN_COMPLETED, EventType.RUN_FINISHED}:
            self._finish_stream()

    @staticmethod
    def _action(item: ToolView) -> str:
        args = item.arguments
        if isinstance(args.get("argv"), list):
            action = shlex.join(str(value) for value in args["argv"])
        else:
            action = str(args.get("command") or args.get("path") or args.get("query") or "")
            if not action:
                action = RichEventSink._short(args, 140)
        return " ".join(action.split())[:160]

    def _flush_paragraphs(self) -> None:
        # Render only complete Markdown blocks; keep fences intact across deltas.
        fence = None
        boundary = 0
        offset = 0
        for line in self._markdown.splitlines(keepends=True):
            match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
            if match:
                marker = match.group(1)
                if fence is None:
                    fence = marker
                elif marker[0] == fence[0] and len(marker) >= len(fence):
                    fence = None
            offset += len(line)
            if line.endswith("\n") and not line.strip() and fence is None:
                boundary = offset
        if boundary:
            self._print_markdown(self._markdown[:boundary])
            self._markdown = self._markdown[boundary:]
        # Bound buffering for very long paragraphs/code blocks. Preserve their
        # source and streaming responsiveness rather than truncating content.
        if len(self._markdown) > 8192 or self._raw_remainder:
            self.console.print(self._markdown, end="", markup=False, highlight=False)
            self._markdown = ""
            self._raw_remainder = self._streaming = True

    def _print_markdown(self, text: str) -> None:
        if self._raw_remainder:
            self.console.print(text, end="", markup=False, highlight=False)
        elif text.strip():
            self.console.print(Markdown(text, code_theme="ansi_dark", hyperlinks=False))

    def _finish_stream(self) -> None:
        if self._tool_streaming:
            self.console.print()
            self._tool_streaming = False
        if self._markdown:
            self._print_markdown(self._markdown)
            self._markdown = ""
        super()._finish_stream()
        self._raw_remainder = False

    def show_details(self, number: int | None = None, offset: int = 0, *, output_only=False):
        self._finish_stream()
        items = list(self.ui.state.tools.values())
        if number is None:
            table = Table("编号", "工具", "状态", "耗时", "操作")
            for item in items:
                table.add_row(
                    str(item.number),
                    item.name,
                    STATUS_LABELS.get(item.status, item.status),
                    f"{item.duration:.1f}s" if item.duration is not None else "—",
                    self._action(item),
                )
            self.console.print(table if items else "本次 CLI 尚无工具记录。")
            return
        item = next((entry for entry in items if entry.number == number), None)
        if item is None:
            self.console.print("未找到该编号；/details 可查看最近 200 个工具记录。")
            return
        if not output_only:
            self.console.print(Text(f"[{item.number}] {item.name} · {item.status}"))
            self._print_source(json.dumps(item.arguments, ensure_ascii=False, indent=2), "json")
            if item.name == "apply_patch" and "old_text" in item.arguments:
                path = str(item.arguments.get("path", "file"))
                diff = "".join(
                    unified_diff(
                        str(item.arguments["old_text"]).splitlines(keepends=True),
                        str(item.arguments.get("new_text", "")).splitlines(keepends=True),
                        fromfile=f"a/{path}",
                        tofile=f"b/{path}",
                    )
                )
                if diff:
                    self.console.print("请求修改的片段差异：")
                    self._print_source(diff, "diff")
            if item.process_id:
                self.console.print(Text(f"进程：{item.process_id}（状态为最近一次事件报告）"))
        runtime = self.ui.runtime
        blob = (
            runtime.store.read_context_blob(
                self.ui.state.session_id,
                item.reference,
                offset=offset,
                limit=16_004,
            )
            if runtime and item.reference
            else None
        )
        if blob:
            content = blob["content"]
            # Peek past the page boundary, then cut at a UTF-8 character
            # boundary. Following the suggested offsets never splits Chinese
            # characters or emoji across two pages.
            if len(content.encode("utf-8")) > 16_000:
                content = content.encode("utf-8")[:16_000].decode("utf-8", errors="ignore")
                blob["next_offset"] = offset + len(content.encode("utf-8"))
                blob["eof"] = blob["next_offset"] >= blob["byte_count"]
            lexer = (
                "diff" if content.startswith(("diff --git", "--- ", "*** Begin Patch")) else "text"
            )
            self._print_source(content, lexer)
            if not blob["eof"]:
                self.console.print(
                    Text(f"后续内容：/details {item.number} {blob['next_offset']}（字节偏移）")
                )
        else:
            self.console.print(Text(item.excerpt or item.error or "尚无输出。"))
        if item.truncated:
            self.console.print("工具输出已被执行层截断；这里展示的是保留内容。")

    def _print_source(self, content: str, lexer: str):
        if self.ui.enhanced:
            self.console.print(Syntax(content, lexer, theme="ansi_dark", word_wrap=True))
        else:
            self.console.print(content, markup=False, highlight=False)
