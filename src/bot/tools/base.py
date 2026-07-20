from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from bot.core.models import ToolDefinition
from bot.execution import ExecutionTarget


class ToolAnnotations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    read_only: bool = False
    destructive: bool = False
    network_access: bool = False
    secret_access: bool = False
    idempotent: bool = False
    default_timeout: float = Field(default=300, gt=0)
    output_limit: int = Field(default=1_000_000, gt=0)


class ToolContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    workspace: Path
    execution_target: ExecutionTarget
    workspace_only: bool = True
    max_output_bytes: int = Field(default=1_000_000, gt=0)
    output_callback: Callable[[str, str], Awaitable[None]] | None = None
    denied_paths: tuple[Path, ...] = ()

    async def emit_output(self, stream: str, data: str) -> None:
        if self.output_callback and data:
            await self.output_callback(stream, data)


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool
    output: str = ""
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False

    def model_content(self) -> str:
        if self.success:
            return self.output or "操作成功，无输出。"
        message = f"工具执行失败: {self.error or '未知错误'}"
        if self.output:
            message += f"\n\n{self.output}"
        return message


class Tool(ABC):
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: ToolAnnotations

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
        )

    @abstractmethod
    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        raise NotImplementedError


def resolve_path(context: ToolContext, raw_path: str, *, must_exist: bool = False) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = context.workspace / candidate
    path = candidate.resolve(strict=False)
    if context.workspace_only:
        workspace = context.workspace.resolve()
        try:
            path.relative_to(workspace)
        except ValueError as exc:
            raise ValueError(f"路径超出工作区: {raw_path}") from exc
    if must_exist:
        path = candidate.resolve(strict=True)
        if context.workspace_only:
            try:
                path.relative_to(context.workspace.resolve())
            except ValueError as exc:
                raise ValueError(f"路径通过符号链接逃逸工作区: {raw_path}") from exc
    if path_is_denied(context, path):
        raise ValueError(f"拒绝访问受保护的内部路径: {raw_path}")
    return path


def path_is_denied(context: ToolContext, path: Path) -> bool:
    resolved = path.resolve(strict=False)
    for denied_path in context.denied_paths:
        denied = denied_path.expanduser().resolve(strict=False)
        if resolved == denied:
            return True
        try:
            resolved.relative_to(denied)
        except ValueError:
            continue
        return True
    return False
