from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from bot.subagents.models import AgentSource, AgentSpec, ExecutionMode, WorkerIsolation
from bot.tools import ToolRegistry


class AgentExecutionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: ExecutionMode = ExecutionMode.FOREGROUND
    allowed: list[ExecutionMode] = Field(
        default_factory=lambda: [ExecutionMode.FOREGROUND, ExecutionMode.BACKGROUND]
    )


class AgentLimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_steps: int | None = Field(default=None, ge=1)
    max_wall_time_seconds: float | None = Field(default=None, gt=0)
    max_cost_usd: float | None = Field(default=None, gt=0)


class AgentInteractionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    can_request_input: bool = True


class AgentOutputConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: str = "structured"

    @field_validator("format")
    @classmethod
    def validate_format(cls, value: str) -> str:
        if value not in {"structured", "text"}:
            raise ValueError("output.format 只支持 structured 或 text")
        return value


class AgentFrontmatter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    name: str
    description: str
    model: str | None = None
    tools: list[str] = Field(default_factory=list)
    isolation: WorkerIsolation = WorkerIsolation.READ_ONLY
    skills: list[str] = Field(default_factory=list)
    execution: AgentExecutionConfig = Field(default_factory=AgentExecutionConfig)
    limits: AgentLimitsConfig = Field(default_factory=AgentLimitsConfig)
    interaction: AgentInteractionConfig = Field(default_factory=AgentInteractionConfig)
    output: AgentOutputConfig = Field(default_factory=AgentOutputConfig)

    @field_validator("schema_version")
    @classmethod
    def validate_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("只支持 schema_version: 1")
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value or len(value) > 64:
            raise ValueError("Agent name 长度必须为 1-64")
        allowed = "abcdefghijklmnopqrstuvwxyz0123456789-_"
        if any(character not in allowed for character in value):
            raise ValueError("Agent name 只能包含小写字母、数字、连字符和下划线")
        return value

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Agent description 不能为空")
        return value


class AgentDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path
    level: str
    message: str


class AgentCatalog:
    """Discover validated Agent Markdown without silently widening permissions."""

    def __init__(
        self,
        *,
        builtin_root: Path,
        user_root: Path,
        project_root: Path,
        tools: ToolRegistry,
        project_trusted: bool,
        allow_worktree_writes: bool,
    ) -> None:
        self.builtin_root = builtin_root.resolve()
        self.user_root = user_root.expanduser().resolve()
        self.project_root = project_root.resolve()
        self.tools = tools
        self.project_trusted = project_trusted
        self.allow_worktree_writes = allow_worktree_writes
        self.agents: dict[str, AgentSpec] = {}
        self.diagnostics: list[AgentDiagnostic] = []
        self.project_digest = self.compute_project_digest(self.project_root)

    @staticmethod
    def compute_project_digest(root: Path) -> str:
        digest = hashlib.sha256()
        if not root.is_dir():
            return digest.hexdigest()
        for path in sorted(root.glob("*.md")):
            if path.is_symlink() or not path.is_file():
                continue
            name = path.name.encode()
            try:
                content = path.read_bytes()
            except OSError:
                continue
            digest.update(len(name).to_bytes(8, "big"))
            digest.update(name)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
        return digest.hexdigest()

    def scan(self) -> None:
        self.agents.clear()
        self.diagnostics.clear()
        self.project_digest = self.compute_project_digest(self.project_root)
        discovered: dict[str, list[AgentSpec]] = {}
        for root, source in (
            (self.builtin_root, AgentSource.BUILTIN),
            (self.user_root, AgentSource.USER),
            (self.project_root, AgentSource.PROJECT),
        ):
            if source == AgentSource.PROJECT and not self.project_trusted:
                if root.is_dir() and any(root.glob("*.md")):
                    self.diagnostics.append(
                        AgentDiagnostic(
                            path=root,
                            level="warning",
                            message="项目 Agent 尚未信任，未加载其名称、说明和指令",
                        )
                    )
                continue
            if not root.exists():
                continue
            if not root.is_dir():
                self.diagnostics.append(
                    AgentDiagnostic(path=root, level="error", message="Agent 路径不是目录")
                )
                continue
            for path in sorted(root.glob("*.md")):
                try:
                    if path.is_symlink():
                        raise ValueError("Agent Markdown 不允许使用符号链接")
                    spec = self._load(path, source)
                    self._validate_capabilities(spec)
                    discovered.setdefault(spec.name, []).append(spec)
                except (OSError, UnicodeError, ValueError, yaml.YAMLError, ValidationError) as exc:
                    self.diagnostics.append(
                        AgentDiagnostic(path=path, level="error", message=str(exc))
                    )
        for name, matches in discovered.items():
            if len(matches) > 1:
                for match in matches:
                    self.diagnostics.append(
                        AgentDiagnostic(
                            path=match.definition_path or Path(name),
                            level="error",
                            message=f"Agent 名称重复，已禁用且不会静默覆盖: {name}",
                        )
                    )
                continue
            self.agents[name] = matches[0]

    def _load(self, path: Path, source: AgentSource) -> AgentSpec:
        content = path.read_text(encoding="utf-8")
        if not content.startswith("---\n"):
            raise ValueError("Agent Markdown 缺少 YAML frontmatter")
        try:
            _, frontmatter, body = content.split("---", 2)
        except ValueError as exc:
            raise ValueError("Agent Markdown frontmatter 未闭合") from exc
        raw: Any = yaml.safe_load(frontmatter) or {}
        if not isinstance(raw, dict):
            raise ValueError("Agent Markdown frontmatter 必须是对象")
        data = AgentFrontmatter.model_validate(raw)
        instructions = body.strip()
        if not instructions:
            raise ValueError("Agent Markdown 正文指令不能为空")
        if data.execution.default not in data.execution.allowed:
            raise ValueError("execution.default 必须包含在 execution.allowed 中")
        model = None if data.model in {None, "inherit"} else data.model
        sha256 = hashlib.sha256(content.encode()).hexdigest()
        return AgentSpec(
            name=data.name,
            description=data.description,
            instructions=instructions,
            allowed_tools=list(dict.fromkeys(data.tools)),
            isolation=data.isolation,
            max_steps=data.limits.max_steps,
            max_wall_time_seconds=data.limits.max_wall_time_seconds,
            max_cost_usd=data.limits.max_cost_usd,
            explicit_skills=list(dict.fromkeys(data.skills)),
            model=model,
            source=source,
            definition_path=path.resolve(),
            definition_sha256=sha256,
            default_execution=data.execution.default,
            allowed_execution=list(dict.fromkeys(data.execution.allowed)),
            can_request_input=data.interaction.can_request_input,
            output_format=data.output.format,
        )

    def _validate_capabilities(self, spec: AgentSpec) -> None:
        if spec.isolation == WorkerIsolation.WORKTREE and not self.allow_worktree_writes:
            raise ValueError("当前全局配置禁止 worktree 写入型 Agent")
        for name in spec.allowed_tools:
            tool = self.tools.get(name)
            if tool is None:
                raise ValueError(f"未知 Tool: {name}")
            if spec.isolation == WorkerIsolation.READ_ONLY and not tool.annotations.read_only:
                raise ValueError(f"read_only Agent 不允许声明写入型 Tool: {name}")

    def list(self) -> list[AgentSpec]:
        return sorted(self.agents.values(), key=lambda item: item.name)

    def summary(self) -> list[dict[str, Any]]:
        return [
            {
                "name": item.name,
                "description": item.description,
                "source": item.source.value,
                "model": item.model or "inherit",
                "isolation": item.isolation.value,
                "default_execution": item.default_execution.value,
                "allowed_execution": [mode.value for mode in item.allowed_execution],
                "tools": item.allowed_tools,
                "path": str(item.definition_path) if item.definition_path else None,
            }
            for item in self.list()
        ]
