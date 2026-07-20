from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelConfig(StrictModel):
    provider: Literal["openai_compatible"] = "openai_compatible"
    base_url: str = ""
    api_key_ref: str = "env:BOT_MODEL_API_KEY"
    name: str = ""
    temperature: float = Field(default=0.2, ge=0, le=2)
    timeout_seconds: float = Field(default=120, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    context_window_tokens: int = Field(default=131_072, gt=0)
    input_cost_per_million: float | None = Field(default=None, ge=0)
    output_cost_per_million: float | None = Field(default=None, ge=0)


class AgentConfig(StrictModel):
    max_steps: int = Field(default=30, ge=1)
    max_wall_time_seconds: float = Field(default=1800, gt=0)
    max_tool_output_bytes: int = Field(default=1_000_000, gt=0)
    max_total_tool_output_bytes: int = Field(default=5_000_000, gt=0)
    max_consecutive_failures: int = Field(default=3, ge=1)
    max_cost_usd: float | None = Field(default=None, gt=0)


class SubagentsConfig(StrictModel):
    enabled: bool = True
    max_concurrent: int = Field(default=3, ge=1, le=32)
    max_queued: int = Field(default=32, ge=1, le=512)
    max_tasks_per_session: int = Field(default=16, ge=1, le=256)
    max_steps: int = Field(default=15, ge=1)
    max_wall_time_seconds: float = Field(default=900, gt=0)
    max_cost_usd_per_task: float | None = Field(default=None, gt=0)
    max_total_cost_usd_per_session: float | None = Field(default=None, gt=0)
    result_inline_chars: int = Field(default=8_000, ge=512, le=64_000)
    allow_worktree_writes: bool = True
    worktree_dir: str = Field(default=".bot/agent-worktrees", min_length=1)
    shutdown_grace_seconds: float = Field(default=10, ge=0, le=120)


class PermissionsConfig(StrictModel):
    mode: Literal["safe", "read-only", "full-access"] = "safe"
    workspace_only: bool = True
    network: Literal["allow", "ask", "deny"] = "ask"


class ContextConfig(StrictModel):
    max_input_tokens: int = Field(default=120_000, gt=0)
    auto_compact_threshold: float = Field(default=0.8, gt=0, lt=1)
    output_reserve_tokens: int = Field(default=4_096, ge=0)
    protocol_reserve_tokens: int = Field(default=2_048, ge=0)
    safety_margin_tokens: int = Field(default=2_048, ge=0)
    snapshot_max_tokens: int = Field(default=12_000, gt=0)
    recent_conversation_tokens: int = Field(default=48_000, gt=0)
    memory_tokens: int = Field(default=8_000, gt=0)
    active_skill_tokens: int = Field(default=16_000, gt=0)
    tool_schema_tokens: int = Field(default=16_000, gt=0)
    tool_result_inline_tokens: int = Field(default=4_000, gt=0)
    tool_result_head_chars: int = Field(default=6_000, gt=0)
    tool_result_tail_chars: int = Field(default=2_000, ge=0)


class SkillsConfig(StrictModel):
    path: str = "./skills"
    auto_activate: bool = True
    max_auto_activated: int = Field(default=3, ge=0)
    max_catalog_chars: int = Field(default=8_000, gt=0)


class DisplayConfig(StrictModel):
    tool_output: Literal["summary", "full"] = "summary"
    progress: bool = True


class StorageConfig(StrictModel):
    state_path: str = "~/.bot/state.db"


class AppConfig(StrictModel):
    model: ModelConfig = Field(default_factory=ModelConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    subagents: SubagentsConfig = Field(default_factory=SubagentsConfig)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)

    @model_validator(mode="after")
    def validate_context_budget(self) -> AppConfig:
        output_reserve = max(
            self.context.output_reserve_tokens,
            self.model.max_output_tokens or 0,
        )
        reserved = (
            output_reserve
            + self.context.protocol_reserve_tokens
            + self.context.safety_margin_tokens
        )
        if reserved >= self.model.context_window_tokens:
            raise ValueError(
                "context 的 output/protocol/safety reserve 总和必须小于 model.context_window_tokens"
            )
        if self.subagents.max_concurrent > self.subagents.max_queued:
            raise ValueError("subagents.max_concurrent 不能大于 max_queued")
        worktree_dir = Path(self.subagents.worktree_dir)
        if not worktree_dir.parts or worktree_dir.is_absolute() or ".." in worktree_dir.parts:
            raise ValueError("subagents.worktree_dir 必须是工作区内相对路径")
        return self

    def skill_path(self, workspace: Path) -> Path:
        path = Path(self.skills.path).expanduser()
        return path if path.is_absolute() else (workspace / path).resolve()

    def state_path(self, workspace: Path) -> Path:
        path = Path(self.storage.state_path).expanduser()
        return path.resolve() if path.is_absolute() else (workspace / path).resolve()
