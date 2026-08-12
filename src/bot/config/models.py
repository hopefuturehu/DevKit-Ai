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


class ProgressConfig(StrictModel):
    enabled: bool = True
    warning_after_no_progress_steps: int = Field(default=4, ge=1)
    recovery_after_no_progress_steps: int = Field(default=7, ge=2)
    finalize_after_no_progress_steps: int = Field(default=11, ge=3)
    max_recovery_attempts_per_epoch: int = Field(default=1, ge=0, le=10)
    exact_failure_warning: int = Field(default=2, ge=1)
    exact_failure_recovery: int = Field(default=3, ge=2)
    same_tool_failure_warning: int = Field(default=3, ge=1)
    same_tool_failure_recovery: int = Field(default=5, ge=2)
    idempotent_repeat_warning: int = Field(default=2, ge=1)
    idempotent_repeat_recovery: int = Field(default=3, ge=2)
    cycle_window_size: int = Field(default=16, ge=4, le=256)
    max_cycle_period: int = Field(default=4, ge=1, le=32)
    cycles_before_warning: int = Field(default=2, ge=2)
    cycles_before_recovery: int = Field(default=3, ge=2)

    @model_validator(mode="after")
    def validate_thresholds(self) -> ProgressConfig:
        if not (
            self.warning_after_no_progress_steps
            < self.recovery_after_no_progress_steps
            < self.finalize_after_no_progress_steps
        ):
            raise ValueError("progress 的 warning/recovery/finalize 阈值必须严格递增")
        for warning, recovery, name in (
            (self.exact_failure_warning, self.exact_failure_recovery, "exact_failure"),
            (
                self.same_tool_failure_warning,
                self.same_tool_failure_recovery,
                "same_tool_failure",
            ),
            (
                self.idempotent_repeat_warning,
                self.idempotent_repeat_recovery,
                "idempotent_repeat",
            ),
            (self.cycles_before_warning, self.cycles_before_recovery, "cycles"),
        ):
            if warning >= recovery:
                raise ValueError(f"progress.{name} 的 warning 必须小于 recovery")
        if self.max_cycle_period * self.cycles_before_recovery > self.cycle_window_size:
            raise ValueError(
                "progress.cycle_window_size 必须容纳 max_cycle_period * cycles_before_recovery"
            )
        return self


class FinalizationConfig(StrictModel):
    enabled: bool = True
    model_timeout_seconds: float | None = Field(default=120, gt=0)
    fallback_summary: bool = True


class AgentConfig(StrictModel):
    # None means that normal task execution has no fixed global budget.  These
    # fields remain available as explicit compatibility/safety policies.
    max_steps: int | None = Field(default=None, ge=1)
    max_wall_time_seconds: float | None = Field(default=None, gt=0)
    max_tool_output_bytes: int = Field(default=1_000_000, gt=0)
    max_total_tool_output_bytes: int | None = Field(default=None, gt=0)
    max_consecutive_failures: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, gt=0)
    process_wait_seconds: float = Field(default=10, ge=0, le=60)
    process_hard_timeout_seconds: float | None = Field(default=None, gt=0)
    max_managed_processes: int = Field(default=16, ge=1, le=256)
    progress: ProgressConfig = Field(default_factory=ProgressConfig)
    finalization: FinalizationConfig = Field(default_factory=FinalizationConfig)


class SubagentsConfig(StrictModel):
    enabled: bool = True
    max_concurrent: int = Field(default=3, ge=1, le=32)
    max_queued: int = Field(default=32, ge=1, le=512)
    max_tasks_per_session: int = Field(default=16, ge=1, le=256)
    max_steps: int | None = Field(default=None, ge=1)
    max_wall_time_seconds: float | None = Field(default=None, gt=0)
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
    compaction_model: str | None = None
    compaction_summary_tokens: int = Field(default=8_000, ge=512)
    compaction_max_output_tokens: int = Field(default=8_192, ge=512)
    compaction_max_message_chars: int = Field(default=12_000, ge=500)
    compaction_rebuild_every: int = Field(default=5, ge=1, le=100)


class MemoryConfig(StrictModel):
    enabled: bool = True
    path: str = "./.bot/memory"
    auto_extract: bool = True
    model: str | None = None
    max_runs_per_cycle: int = Field(default=3, ge=1, le=100)
    max_attempts: int = Field(default=3, ge=1, le=20)
    max_candidates_per_run: int = Field(default=5, ge=1, le=20)
    max_source_tokens: int = Field(default=24_000, ge=1_000)
    max_message_chars: int = Field(default=8_000, ge=500)
    max_output_tokens: int = Field(default=2_048, ge=256)
    min_confidence: float = Field(default=0.75, ge=0, le=1)
    index_tokens: int = Field(default=2_000, ge=256)
    search_limit: int = Field(default=8, ge=1, le=50)


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
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
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
        memory_path = Path(self.memory.path).expanduser()
        if not memory_path.parts or str(memory_path) in {".", ".."}:
            raise ValueError("memory.path 不能指向工作区或空路径")
        if not memory_path.is_absolute() and ".." in memory_path.parts:
            raise ValueError("memory.path 相对路径不能包含 ..")
        if memory_path.is_absolute() and memory_path == Path(memory_path.anchor):
            raise ValueError("memory.path 不能指向文件系统根目录")
        return self

    def skill_path(self, workspace: Path) -> Path:
        path = Path(self.skills.path).expanduser()
        return path if path.is_absolute() else (workspace / path).resolve()

    def state_path(self, workspace: Path) -> Path:
        path = Path(self.storage.state_path).expanduser()
        return path.resolve() if path.is_absolute() else (workspace / path).resolve()

    def memory_path(self, workspace: Path) -> Path:
        path = Path(self.memory.path).expanduser()
        return path.resolve() if path.is_absolute() else (workspace / path).resolve()
