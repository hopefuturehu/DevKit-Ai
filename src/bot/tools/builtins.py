from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import tempfile
from asyncio import get_running_loop
from pathlib import Path
from typing import Any

import httpx

from bot.core.progress import ProgressKind, ProgressSignal
from bot.execution import ProcessEventKind, ProcessSnapshot, ProcessSpec, ProcessStatus
from bot.tools.base import (
    Tool,
    ToolAnnotations,
    ToolContext,
    ToolResult,
    ToolResultStatus,
    path_is_denied,
    resolve_path,
)


class ReadFileTool(Tool):
    name = "read_file"
    description = "读取工作区内文本文件的全部或指定行范围。"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区的文件路径"},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    annotations = ToolAnnotations(read_only=True, idempotent=True)

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        try:
            path = resolve_path(context, str(arguments["path"]), must_exist=True)
            if not path.is_file():
                return ToolResult(success=False, error=f"不是文件: {path}")
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines(keepends=True)
            start = int(arguments.get("start_line", 1))
            end = int(arguments.get("end_line", len(lines)))
            if start < 1 or end < start:
                return ToolResult(success=False, error="行范围不合法")
            selected = "".join(lines[start - 1 : end])
            raw = selected.encode()
            truncated = len(raw) > context.max_output_bytes
            if truncated:
                selected = raw[: context.max_output_bytes].decode(errors="replace")
            return ToolResult(
                success=True,
                output=selected,
                truncated=truncated,
                metadata={"path": str(path), "start_line": start, "end_line": end},
                progress=ProgressSignal(
                    kind=ProgressKind.WEAK,
                    summary=f"读取了 {path.name} 的新证据",
                    evidence_key=(
                        f"read:{path}:{start}:{end}:"
                        f"{hashlib.sha256(selected.encode()).hexdigest()}"
                    ),
                ),
            )
        except (KeyError, OSError, ValueError) as exc:
            return ToolResult(success=False, error=str(exc))


class SearchTextTool(Tool):
    name = "search_text"
    description = "在工作区文件中搜索普通文本或正则表达式，返回文件、行号和匹配行。"
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "path": {"type": "string", "default": "."},
            "regex": {"type": "boolean", "default": False},
            "glob": {"type": "string", "default": "*"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 500},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    annotations = ToolAnnotations(read_only=True, idempotent=True)

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        try:
            root = resolve_path(context, str(arguments.get("path", ".")), must_exist=True)
            query = str(arguments["query"])
            pattern = re.compile(query) if arguments.get("regex", False) else None
            glob_pattern = str(arguments.get("glob", "*"))
            max_results = int(arguments.get("max_results", 100))
            results: list[str] = []
            paths = [root] if root.is_file() else self._walk(root)
            for path in paths:
                if not fnmatch.fnmatch(path.name, glob_pattern):
                    continue
                try:
                    resolved_path = path.resolve(strict=True)
                    resolved_path.relative_to(context.workspace.resolve())
                    if path_is_denied(context, resolved_path):
                        continue
                    if resolved_path.stat().st_size > 5_000_000:
                        continue
                    for line_number, line in enumerate(
                        resolved_path.read_text(encoding="utf-8", errors="replace").splitlines(),
                        1,
                    ):
                        matched = pattern.search(line) if pattern else query in line
                        if matched:
                            relative = path.relative_to(context.workspace)
                            results.append(f"{relative}:{line_number}:{line}")
                            if len(results) >= max_results:
                                return ToolResult(
                                    success=True,
                                    output="\n".join(results),
                                    truncated=True,
                                    metadata={"result_count": len(results)},
                                    progress=self._progress_signal(query, root, results),
                                )
                except (OSError, UnicodeError, ValueError):
                    continue
            return ToolResult(
                success=True,
                output="\n".join(results) if results else "未找到匹配内容。",
                metadata={"result_count": len(results)},
                progress=self._progress_signal(query, root, results),
            )
        except (KeyError, OSError, ValueError, re.error) as exc:
            return ToolResult(success=False, error=str(exc))

    @staticmethod
    def _walk(root: Path):
        ignored = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules"}
        for directory, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in ignored]
            for filename in filenames:
                yield Path(directory) / filename

    @staticmethod
    def _progress_signal(query: str, root: Path, results: list[str]) -> ProgressSignal:
        digest = hashlib.sha256("\n".join(results).encode()).hexdigest()
        return ProgressSignal(
            kind=ProgressKind.WEAK,
            summary=f"搜索得到 {len(results)} 条证据",
            evidence_key=f"search:{root}:{query}:{digest}",
        )


class ApplyPatchTool(Tool):
    name = "apply_patch"
    description = "以精确匹配方式修改工作区文本文件；old_text 必须唯一，降低误修改风险。"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
            "create": {"type": "boolean", "default": False},
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    }
    annotations = ToolAnnotations(read_only=False, destructive=False, idempotent=False)

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        try:
            path = resolve_path(context, str(arguments["path"]), must_exist=False)
            old_text = str(arguments["old_text"])
            new_text = str(arguments["new_text"])
            create = bool(arguments.get("create", False))
            if not path.exists():
                if not create or old_text:
                    return ToolResult(success=False, error=f"文件不存在: {path}")
                original = ""
            else:
                if not path.is_file():
                    return ToolResult(success=False, error=f"不是文件: {path}")
                original = path.read_text(encoding="utf-8")
            count = original.count(old_text) if old_text else 1
            if count != 1:
                return ToolResult(
                    success=False,
                    error=f"old_text 需要恰好匹配一次，实际匹配 {count} 次",
                )
            updated = new_text if not old_text else original.replace(old_text, new_text, 1)
            before_hash = hashlib.sha256(original.encode()).hexdigest()
            after_hash = hashlib.sha256(updated.encode()).hexdigest()
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = path.stat().st_mode if path.exists() else None
            fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(updated)
                    handle.flush()
                    os.fsync(handle.fileno())
                if mode is not None:
                    os.chmod(temp_name, mode)
                os.replace(temp_name, path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            return ToolResult(
                success=True,
                output=f"已更新 {path.relative_to(context.workspace)}",
                metadata={
                    "path": str(path),
                    "bytes": len(updated.encode()),
                    "before_sha256": before_hash,
                    "after_sha256": after_hash,
                },
                progress=ProgressSignal(
                    kind=(
                        ProgressKind.STRONG
                        if before_hash != after_hash
                        else ProgressKind.NONE
                    ),
                    summary=(
                        f"文件 {path.name} 内容已变化"
                        if before_hash != after_hash
                        else f"文件 {path.name} 内容未变化"
                    ),
                    evidence_key=f"file:{path}:{after_hash}",
                ),
            )
        except (KeyError, OSError, UnicodeError, ValueError) as exc:
            return ToolResult(success=False, error=str(exc))


def _process_metadata(snapshot: ProcessSnapshot) -> dict[str, Any]:
    return {
        "process_id": snapshot.process_id,
        "process_status": snapshot.status.value,
        "returncode": snapshot.returncode,
        "argv": snapshot.argv,
        "cwd": str(snapshot.cwd),
        "elapsed_seconds": round(snapshot.elapsed_seconds, 3),
        "hard_timeout_seconds": snapshot.hard_timeout_seconds,
        "interactive": snapshot.interactive,
        "last_output_seconds_ago": (
            round(snapshot.last_output_seconds_ago, 3)
            if snapshot.last_output_seconds_ago is not None
            else None
        ),
        "termination_reason": snapshot.termination_reason,
    }


def _process_progress(snapshot: ProcessSnapshot) -> ProgressSignal:
    if snapshot.status == ProcessStatus.RUNNING:
        return ProgressSignal(
            kind=ProgressKind.WAITING,
            summary=f"进程 {snapshot.process_id} 仍在运行",
            evidence_key=f"process:{snapshot.process_id}",
            inactivity_seconds=snapshot.last_output_seconds_ago or 0,
        )
    if snapshot.status == ProcessStatus.COMPLETED:
        return ProgressSignal(
            kind=ProgressKind.WEAK,
            summary=f"进程 {snapshot.process_id} 已完成",
            evidence_key=(
                f"process:{snapshot.process_id}:completed:{snapshot.returncode}:"
                f"{hashlib.sha256((snapshot.stdout + snapshot.stderr).encode()).hexdigest()}"
            ),
        )
    return ProgressSignal(
        kind=ProgressKind.NONE,
        summary=f"进程 {snapshot.process_id} 以 {snapshot.status.value} 结束",
        evidence_key=f"process:{snapshot.process_id}:{snapshot.status.value}",
    )


def _combined_process_output(snapshot: ProcessSnapshot) -> str:
    output = snapshot.stdout
    if snapshot.stderr:
        output += ("\n" if output else "") + f"[stderr]\n{snapshot.stderr}"
    return output


def _process_tool_result(snapshot: ProcessSnapshot) -> ToolResult:
    output = _combined_process_output(snapshot)
    metadata = _process_metadata(snapshot)
    if snapshot.status == ProcessStatus.RUNNING:
        status_line = (
            f"进程仍在运行（process_id={snapshot.process_id}, "
            f"elapsed={snapshot.elapsed_seconds:.1f}s）。"
            "使用 poll_process 查看增量输出和退出状态；"
            "需要交互时使用 send_process_input，需要停止时使用 terminate_process。"
        )
        output = f"{output}\n\n{status_line}" if output else status_line
        return ToolResult(
            success=True,
            status=ToolResultStatus.RUNNING,
            output=output,
            metadata=metadata,
            progress=_process_progress(snapshot),
            truncated=snapshot.truncated,
        )
    if snapshot.status == ProcessStatus.COMPLETED:
        return ToolResult(
            success=True,
            status=ToolResultStatus.COMPLETED,
            output=output,
            metadata=metadata,
            progress=_process_progress(snapshot),
            truncated=snapshot.truncated,
        )
    result_status = {
        ProcessStatus.TIMED_OUT: ToolResultStatus.TIMED_OUT,
        ProcessStatus.CANCELLED: ToolResultStatus.CANCELLED,
    }.get(snapshot.status, ToolResultStatus.FAILED)
    error = snapshot.termination_reason
    if error is None and snapshot.returncode is not None:
        error = f"命令退出码 {snapshot.returncode}"
    return ToolResult(
        success=False,
        status=result_status,
        output=output,
        error=error or f"进程状态为 {snapshot.status.value}",
        metadata=metadata,
        progress=_process_progress(snapshot),
        truncated=snapshot.truncated,
    )


async def _emit_process_output(context: ToolContext, snapshot: ProcessSnapshot) -> None:
    await context.emit_output("stdout", snapshot.stdout)
    await context.emit_output("stderr", snapshot.stderr)


async def _wait_for_managed_process(
    context: ToolContext,
    process_id: str,
    *,
    wait_seconds: float,
) -> ProcessSnapshot:
    if wait_seconds <= 0:
        snapshot = await context.execution_target.poll_process(process_id)
        await _emit_process_output(context, snapshot)
        return snapshot

    deadline = get_running_loop().time() + wait_seconds
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    snapshot: ProcessSnapshot | None = None
    while True:
        remaining = deadline - get_running_loop().time()
        slice_seconds = max(0.0, min(0.25, remaining))
        snapshot = await context.execution_target.poll_process(
            process_id,
            wait_seconds=slice_seconds,
        )
        stdout_parts.append(snapshot.stdout)
        stderr_parts.append(snapshot.stderr)
        await _emit_process_output(context, snapshot)
        if snapshot.status != ProcessStatus.RUNNING or remaining <= 0:
            break
    return snapshot.model_copy(
        update={
            "stdout": "".join(stdout_parts),
            "stderr": "".join(stderr_parts),
        }
    )


class RunCommandTool(Tool):
    name = "run_command"
    description = (
        "在受控工作目录中启动参数数组形式的命令，不经过 Shell 解析。"
        "默认同步等待 10 秒；仍未结束时返回 process_id 而不会杀死进程，"
        "随后使用 poll_process、send_process_input 或 terminate_process 管理。"
        "timeout_seconds 是可选的进程 hard timeout，不是同步等待时间；"
        "省略时进程没有固定存活上限。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "cwd": {"type": "string", "default": "."},
            "wait_seconds": {"type": "number", "minimum": 0, "maximum": 60, "default": 10},
            "timeout_seconds": {
                "anyOf": [
                    {"type": "number", "minimum": 0.1},
                    {"type": "null"},
                ],
                "default": None,
                "description": "可选的进程绝对存活上限；null 表示不设固定上限",
            },
            "interactive": {
                "type": "boolean",
                "default": False,
                "description": "为需要后续写入 stdin 的命令保留输入管道",
            },
        },
        "required": ["argv"],
        "additionalProperties": False,
    }
    annotations = ToolAnnotations(
        read_only=False,
        destructive=False,
        idempotent=False,
        default_timeout=1800,
    )

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        try:
            argv = [str(value) for value in arguments["argv"]]
            if not argv:
                return ToolResult(success=False, error="argv 不能为空")
            cwd = resolve_path(context, str(arguments.get("cwd", ".")), must_exist=True)
            configured_timeout = arguments.get(
                "timeout_seconds", context.process_hard_timeout_seconds
            )
            spec = ProcessSpec(
                argv=argv,
                cwd=cwd,
                timeout_seconds=(
                    float(configured_timeout) if configured_timeout is not None else None
                ),
                output_limit_bytes=context.max_output_bytes,
                interactive=bool(arguments.get("interactive", False)),
            )
            if context.execution_target.supports_managed_processes:
                process_id = await context.execution_target.start_process(spec)
                snapshot = await _wait_for_managed_process(
                    context,
                    process_id,
                    wait_seconds=float(arguments.get("wait_seconds", context.process_wait_seconds)),
                )
                return _process_tool_result(snapshot)

            stdout: list[str] = []
            stderr: list[str] = []
            returncode: int | None = None
            truncated = False
            async for event in context.execution_target.execute(spec):
                truncated = truncated or event.truncated
                if event.kind == ProcessEventKind.STDOUT:
                    stdout.append(event.data)
                    await context.emit_output("stdout", event.data)
                elif event.kind == ProcessEventKind.STDERR:
                    stderr.append(event.data)
                    await context.emit_output("stderr", event.data)
                else:
                    returncode = event.returncode
            output = "".join(stdout)
            error_output = "".join(stderr)
            combined = output
            if error_output:
                combined += ("\n" if combined else "") + f"[stderr]\n{error_output}"
            return ToolResult(
                success=returncode == 0,
                output=combined,
                error=None if returncode == 0 else f"命令退出码 {returncode}",
                truncated=truncated,
                metadata={"returncode": returncode, "argv": argv, "cwd": str(cwd)},
                progress=ProgressSignal(
                    kind=(ProgressKind.WEAK if returncode == 0 else ProgressKind.NONE),
                    summary=f"命令以退出码 {returncode} 结束",
                    evidence_key=(
                        f"command:{hashlib.sha256(repr(argv).encode()).hexdigest()}:"
                        f"{returncode}:{hashlib.sha256(combined.encode()).hexdigest()}"
                    ),
                ),
            )
        except (
            KeyError,
            NotImplementedError,
            OSError,
            RuntimeError,
            ValueError,
            TimeoutError,
        ) as exc:
            return ToolResult(success=False, error=str(exc))


class RunShellTool(Tool):
    name = "run_shell"
    description = (
        "仅在确实需要管道、重定向或条件连接时启动 POSIX Shell 脚本。"
        "默认同步等待 10 秒，长任务返回 process_id 并继续受管；"
        "策略层会先拆分脚本中的命令段进行风险判断。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "script": {"type": "string", "minLength": 1},
            "cwd": {"type": "string", "default": "."},
            "wait_seconds": {"type": "number", "minimum": 0, "maximum": 60, "default": 10},
            "timeout_seconds": {
                "anyOf": [
                    {"type": "number", "minimum": 0.1},
                    {"type": "null"},
                ],
                "default": None,
            },
            "interactive": {"type": "boolean", "default": False},
        },
        "required": ["script"],
        "additionalProperties": False,
    }
    annotations = ToolAnnotations(
        read_only=False,
        destructive=False,
        idempotent=False,
        default_timeout=1800,
    )

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        command = RunCommandTool()
        forwarded: dict[str, Any] = {
            "argv": ["/bin/sh", "-c", str(arguments["script"])],
            "cwd": arguments.get("cwd", "."),
            "wait_seconds": arguments.get("wait_seconds", context.process_wait_seconds),
            "timeout_seconds": arguments.get(
                "timeout_seconds", context.process_hard_timeout_seconds
            ),
            "interactive": arguments.get("interactive", False),
        }
        return await command.execute(context, forwarded)


class PollProcessTool(Tool):
    name = "poll_process"
    description = (
        "查看受管进程的增量 stdout/stderr 和当前状态。"
        "wait_seconds 只控制本次轮询等待，不改变进程 hard timeout。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "process_id": {"type": "string", "minLength": 1},
            "wait_seconds": {"type": "number", "minimum": 0, "maximum": 60, "default": 0},
        },
        "required": ["process_id"],
        "additionalProperties": False,
    }
    annotations = ToolAnnotations(read_only=True, idempotent=False)

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        try:
            snapshot = await _wait_for_managed_process(
                context,
                str(arguments["process_id"]),
                wait_seconds=float(arguments.get("wait_seconds", 0)),
            )
            return _process_tool_result(snapshot)
        except (KeyError, NotImplementedError, ValueError) as exc:
            return ToolResult(success=False, error=str(exc))


class SendProcessInputTool(Tool):
    name = "send_process_input"
    description = "向以 interactive=true 启动的受管进程写入 stdin，可选择随后发送 EOF。"
    input_schema = {
        "type": "object",
        "properties": {
            "process_id": {"type": "string", "minLength": 1},
            "data": {"type": "string", "default": ""},
            "eof": {"type": "boolean", "default": False},
        },
        "required": ["process_id"],
        "additionalProperties": False,
    }
    annotations = ToolAnnotations(read_only=False, destructive=False, idempotent=False)

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        try:
            data = str(arguments.get("data", ""))
            eof = bool(arguments.get("eof", False))
            snapshot = await context.execution_target.send_process_input(
                str(arguments["process_id"]),
                data,
                eof=eof,
            )
            changed = bool(data) or eof
            return ToolResult(
                success=True,
                output=(
                    f"已向进程 {snapshot.process_id} 写入 "
                    f"{len(data.encode())} bytes"
                    + (" 并发送 EOF。" if eof else "。")
                ),
                metadata=_process_metadata(snapshot),
                progress=ProgressSignal(
                    kind=ProgressKind.STRONG if changed else ProgressKind.NONE,
                    summary=(
                        f"已向进程 {snapshot.process_id} 提交输入"
                        if changed
                        else f"没有向进程 {snapshot.process_id} 提交新输入"
                    ),
                    evidence_key=(
                        f"process-input:{snapshot.process_id}:"
                        f"{len(data.encode())}:{eof}"
                    ),
                ),
            )
        except (KeyError, NotImplementedError, OSError, ValueError) as exc:
            return ToolResult(success=False, error=str(exc))


class TerminateProcessTool(Tool):
    name = "terminate_process"
    description = "终止由当前 Agent Runtime 启动的受管进程及其进程组。"
    input_schema = {
        "type": "object",
        "properties": {
            "process_id": {"type": "string", "minLength": 1},
            "reason": {"type": "string"},
        },
        "required": ["process_id"],
        "additionalProperties": False,
    }
    annotations = ToolAnnotations(read_only=False, destructive=False, idempotent=True)

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        try:
            snapshot = await context.execution_target.terminate_process(
                str(arguments["process_id"]),
                reason=str(arguments["reason"]) if arguments.get("reason") else None,
            )
            await _emit_process_output(context, snapshot)
            output = _combined_process_output(snapshot)
            message = f"进程 {snapshot.process_id} 当前状态为 {snapshot.status.value}。"
            output = f"{output}\n\n{message}" if output else message
            return ToolResult(
                success=True,
                output=output,
                metadata=_process_metadata(snapshot),
                progress=ProgressSignal(
                    kind=(
                        ProgressKind.STRONG
                        if snapshot.status == ProcessStatus.CANCELLED
                        else ProgressKind.NONE
                    ),
                    summary=(
                        f"进程 {snapshot.process_id} 已终止"
                        if snapshot.status == ProcessStatus.CANCELLED
                        else f"进程 {snapshot.process_id} 已处于终态"
                    ),
                    evidence_key=f"process:{snapshot.process_id}:{snapshot.status.value}",
                ),
                truncated=snapshot.truncated,
            )
        except (KeyError, NotImplementedError, ValueError) as exc:
            return ToolResult(success=False, error=str(exc))


class ListProcessesTool(Tool):
    name = "list_processes"
    description = "列出当前 Runtime 启动过的受管进程及状态，不消费它们的输出。"
    input_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    annotations = ToolAnnotations(read_only=True, idempotent=True)

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        try:
            snapshots = await context.execution_target.list_processes()
        except NotImplementedError as exc:
            return ToolResult(success=False, error=str(exc))
        if not snapshots:
            return ToolResult(
                success=True,
                output="当前没有受管进程。",
                metadata={"count": 0},
                progress=ProgressSignal(
                    kind=ProgressKind.WEAK,
                    summary="确认当前没有受管进程",
                    evidence_key="process-list:empty",
                ),
            )
        lines = [
            (
                f"- {snapshot.process_id}: status={snapshot.status.value}, "
                f"elapsed={snapshot.elapsed_seconds:.1f}s, "
                f"last_output={snapshot.last_output_seconds_ago:.1f}s ago, "
                f"command={snapshot.argv!r}"
            )
            for snapshot in snapshots
        ]
        return ToolResult(
            success=True,
            output="\n".join(lines),
            metadata={
                "count": len(snapshots),
                "processes": [_process_metadata(snapshot) for snapshot in snapshots],
            },
            progress=ProgressSignal(
                kind=ProgressKind.WEAK,
                summary=f"检查了 {len(snapshots)} 个受管进程",
                evidence_key=(
                    "process-list:"
                    + hashlib.sha256(
                        repr(
                            [
                                (snapshot.process_id, snapshot.status.value, snapshot.returncode)
                                for snapshot in snapshots
                            ]
                        ).encode()
                    ).hexdigest()
                ),
            ),
        )


class FetchUrlTool(Tool):
    name = "fetch_url"
    description = "通过 HTTP GET 获取明确 URL 的文本内容，用于取得源码或公开资料。"
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "format": "uri"},
            "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 120},
        },
        "required": ["url"],
        "additionalProperties": False,
    }
    annotations = ToolAnnotations(read_only=True, network_access=True, idempotent=True)

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        url = str(arguments.get("url", ""))
        if not url.startswith(("https://", "http://")):
            return ToolResult(success=False, error="只支持 http:// 或 https:// URL")
        try:
            async with httpx.AsyncClient(
                timeout=float(arguments.get("timeout_seconds", 30)), follow_redirects=True
            ) as client:
                response = await client.get(url, headers={"User-Agent": "bot-cli-agent/0.1"})
                response.raise_for_status()
                raw = response.content
                truncated = len(raw) > context.max_output_bytes
                output = raw[: context.max_output_bytes].decode(errors="replace")
                return ToolResult(
                    success=True,
                    output=output,
                    truncated=truncated,
                    metadata={
                        "url": str(response.url),
                        "status_code": response.status_code,
                        "content_type": response.headers.get("content-type"),
                    },
                    progress=ProgressSignal(
                        kind=ProgressKind.WEAK,
                        summary=f"获取了 {response.url} 的网络证据",
                        evidence_key=(
                            f"fetch:{response.url}:{response.status_code}:"
                            f"{hashlib.sha256(output.encode()).hexdigest()}"
                        ),
                    ),
                )
        except httpx.HTTPError as exc:
            return ToolResult(success=False, error=f"网络请求失败: {exc}")


def register_builtin_tools(registry) -> None:
    registry.register(ReadFileTool())
    registry.register(SearchTextTool())
    registry.register(ApplyPatchTool())
    registry.register(RunCommandTool())
    registry.register(RunShellTool())
    registry.register(PollProcessTool())
    registry.register(SendProcessInputTool())
    registry.register(TerminateProcessTool())
    registry.register(ListProcessesTool())
    registry.register(FetchUrlTool())
