from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import httpx

from bot.execution import ProcessEventKind, ProcessSpec
from bot.tools.base import (
    Tool,
    ToolAnnotations,
    ToolContext,
    ToolResult,
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
                                )
                except (OSError, UnicodeError, ValueError):
                    continue
            return ToolResult(
                success=True,
                output="\n".join(results) if results else "未找到匹配内容。",
                metadata={"result_count": len(results)},
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
            )
        except (KeyError, OSError, UnicodeError, ValueError) as exc:
            return ToolResult(success=False, error=str(exc))


class RunCommandTool(Tool):
    name = "run_command"
    description = "在受控工作目录中执行参数数组形式的本地命令，不经过 Shell 解析。"
    input_schema = {
        "type": "object",
        "properties": {
            "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "cwd": {"type": "string", "default": "."},
            "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 3600},
        },
        "required": ["argv"],
        "additionalProperties": False,
    }
    annotations = ToolAnnotations(read_only=False, destructive=False, idempotent=False)

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        try:
            argv = [str(value) for value in arguments["argv"]]
            if not argv:
                return ToolResult(success=False, error="argv 不能为空")
            cwd = resolve_path(context, str(arguments.get("cwd", ".")), must_exist=True)
            spec = ProcessSpec(
                argv=argv,
                cwd=cwd,
                timeout_seconds=float(arguments.get("timeout_seconds", 300)),
                output_limit_bytes=context.max_output_bytes,
            )
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
            )
        except (KeyError, OSError, ValueError, TimeoutError) as exc:
            return ToolResult(success=False, error=str(exc))


class RunShellTool(Tool):
    name = "run_shell"
    description = (
        "仅在确实需要管道、重定向或条件连接时执行 POSIX Shell 脚本。"
        "策略层会先拆分脚本中的命令段进行风险判断。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "script": {"type": "string", "minLength": 1},
            "cwd": {"type": "string", "default": "."},
            "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 3600},
        },
        "required": ["script"],
        "additionalProperties": False,
    }
    annotations = ToolAnnotations(read_only=False, destructive=False, idempotent=False)

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        command = RunCommandTool()
        return await command.execute(
            context,
            {
                "argv": ["/bin/sh", "-c", str(arguments["script"])],
                "cwd": arguments.get("cwd", "."),
                "timeout_seconds": arguments.get("timeout_seconds", 300),
            },
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
                )
        except httpx.HTTPError as exc:
            return ToolResult(success=False, error=f"网络请求失败: {exc}")


def register_builtin_tools(registry) -> None:
    registry.register(ReadFileTool())
    registry.register(SearchTextTool())
    registry.register(ApplyPatchTool())
    registry.register(RunCommandTool())
    registry.register(RunShellTool())
    registry.register(FetchUrlTool())
