from __future__ import annotations

import hashlib
import shlex
from abc import abstractmethod
from typing import Any

from bot.core.progress import ProgressKind, ProgressSignal
from bot.execution import ProcessEventKind, ProcessSpec
from bot.tools.base import Tool, ToolContext, ToolResult


class SubprocessCliTool(Tool):
    executable: str
    required_architectures: set[str] | None = None
    required_operating_systems: set[str] | None = None

    @abstractmethod
    def build_argv(self, context: ToolContext, arguments: dict[str, Any]) -> list[str]:
        raise NotImplementedError

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        try:
            argv = self.build_argv(context, arguments)
            environment = await context.execution_target.probe([self.executable])
            architecture = environment.architecture.lower()
            operating_system = environment.operating_system.lower()
            if (
                self.required_operating_systems
                and operating_system not in self.required_operating_systems
            ):
                command = shlex.join(argv)
                return ToolResult(
                    success=False,
                    output=(
                        "当前操作系统不满足该工具要求，未执行命令。请在鲲鹏 Linux 主机上运行：\n"
                        f"{command}\n\n完成后将原始输出粘贴回当前会话。"
                    ),
                    error=f"当前操作系统 {operating_system} 不受支持",
                    metadata={"manual_command": command, "operating_system": operating_system},
                )
            if self.required_architectures and architecture not in self.required_architectures:
                command = shlex.join(argv)
                return ToolResult(
                    success=False,
                    output=(
                        "当前机器架构不满足该工具要求，未执行命令。请在鲲鹏 ARM 主机上运行：\n"
                        f"{command}\n\n完成后将原始输出粘贴回当前会话。"
                    ),
                    error=f"当前架构 {architecture} 不受支持",
                    metadata={"manual_command": command, "architecture": architecture},
                )
            if environment.executables.get(self.executable) is None:
                command = shlex.join(argv)
                return ToolResult(
                    success=False,
                    output=(
                        f"当前执行目标未找到 {self.executable}，未执行命令。"
                        "安装工具后在目标机器运行：\n"
                        f"{command}\n\n完成后将原始输出粘贴回当前会话。"
                    ),
                    error=f"命令不存在: {self.executable}",
                    metadata={"manual_command": command},
                )
            spec = ProcessSpec(
                argv=argv,
                cwd=context.workspace,
                timeout_seconds=float(
                    arguments.get("timeout_seconds", self.annotations.default_timeout)
                ),
                output_limit_bytes=min(
                    context.max_output_bytes,
                    self.annotations.output_limit,
                ),
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
            if error_output:
                output += ("\n" if output else "") + f"[stderr]\n{error_output}"
            return ToolResult(
                success=returncode == 0,
                output=output,
                error=None if returncode == 0 else f"命令退出码 {returncode}",
                truncated=truncated,
                metadata={"argv": argv, "returncode": returncode},
                progress=ProgressSignal(
                    kind=(ProgressKind.WEAK if returncode == 0 else ProgressKind.NONE),
                    summary=f"外部诊断工具以退出码 {returncode} 结束",
                    evidence_key=(
                        f"subprocess:{self.name}:{returncode}:"
                        f"{hashlib.sha256(output.encode()).hexdigest()}"
                    ),
                ),
            )
        except (KeyError, OSError, ValueError, TimeoutError) as exc:
            return ToolResult(success=False, error=str(exc))
