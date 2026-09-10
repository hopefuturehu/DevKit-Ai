from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelConfig(StrictModel):
    provider: Literal["openai_compatible"] = "openai_compatible"
    base_url: str = ""
    api_key: SecretStr | None = Field(default=None, min_length=1, repr=False)
    api_key_ref: str = "auto:BOT_MODEL_API_KEY"
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
    process_inactivity_warning_seconds: float = Field(default=300, gt=0)
    process_inactivity_recovery_seconds: float = Field(default=900, gt=0)
    # None deliberately means that a quiet but live process is never killed or
    # finalized solely because it has not produced output.
    process_inactivity_finalize_seconds: float | None = Field(default=None, gt=0)

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
        if self.process_inactivity_warning_seconds >= self.process_inactivity_recovery_seconds:
            raise ValueError("process inactivity 的 warning 必须小于 recovery")
        if (
            self.process_inactivity_finalize_seconds is not None
            and self.process_inactivity_recovery_seconds >= self.process_inactivity_finalize_seconds
        ):
            raise ValueError("process inactivity 的 recovery 必须小于 finalize")
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
    model_request_retries: int = Field(default=2, ge=0, le=5)
    model_request_retry_backoff_seconds: float = Field(default=1, ge=0, le=30)
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
    # Explicitly opt in to approving every policy ASK decision. Hard DENY
    # decisions (for example sensitive paths and invalid policy bypasses) stay
    # enforced.
    auto_approve: bool = False
    workspace_only: bool = True
    network: Literal["allow", "ask", "deny"] = "ask"


class ContextConfig(StrictModel):
    max_input_tokens: int = Field(default=120_000, gt=0)
    auto_compact_threshold: float = Field(default=0.8, gt=0, lt=1)
    output_reserve_tokens: int = Field(default=4_096, ge=0)
    protocol_reserve_tokens: int = Field(default=2_048, ge=0)
    safety_margin_tokens: int = Field(default=2_048, ge=0)
    snapshot_max_tokens: int = Field(default=12_000, gt=0)
    recent_conversation_tokens: int = Field(default=20_000, gt=0)
    memory_tokens: int = Field(default=8_000, gt=0)
    active_skill_tokens: int = Field(default=16_000, gt=0)
    tool_schema_tokens: int = Field(default=16_000, gt=0)
    tool_result_inline_tokens: int = Field(default=4_000, gt=0)
    tool_result_head_chars: int = Field(default=6_000, gt=0)
    tool_result_tail_chars: int = Field(default=2_000, ge=0)
    compaction_model: str | None = None
    compaction_summary_target_tokens: int | None = Field(default=None, ge=512)
    compaction_summary_tokens: int = Field(default=4_000, ge=512)
    compaction_max_output_tokens: int = Field(default=8_192, ge=512)
    compaction_max_input_tokens: int = Field(default=60_000, ge=2_048)
    compaction_input_target_ratio: float = Field(default=0.8, gt=0, le=1)
    compaction_repair_attempts: int = Field(default=1, ge=0, le=3)
    compaction_condense_attempts: int = Field(default=1, ge=0, le=3)
    compaction_empty_retries: int = Field(default=1, ge=0, le=3)
    compaction_transport_retries: int = Field(default=1, ge=0, le=3)
    compaction_transport_retry_backoff_seconds: float = Field(default=1, ge=0, le=30)
    compaction_range_attempts: int = Field(default=2, ge=1, le=5)
    compaction_failure_backoff_seconds: float = Field(default=300, ge=0, le=86_400)
    compaction_request_timeout_seconds: float = Field(default=90, gt=0, le=3_600)
    compaction_command_max_requests: int = Field(default=8, ge=1, le=100)
    compaction_command_max_seconds: float = Field(default=600, gt=0, le=86_400)
    compaction_command_max_cost_usd: float | None = Field(default=0.25, gt=0)
    compaction_min_recent_user_turns: int = Field(default=3, ge=1, le=100)
    compaction_source_refs: Literal["range", "item"] = "range"
    compaction_thinking: Literal["auto", "provider_default", "enabled", "disabled"] = "auto"
    compaction_max_message_chars: int = Field(default=12_000, ge=500)
    compaction_rebuild_every: int = Field(default=5, ge=1, le=100)


class MemoryConfig(StrictModel):
    enabled: bool = True
    path: str = "./.bot/memory"
    auto_extract: bool = True
    context_mode: Literal["on_demand", "eager"] = "on_demand"
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
    router_enabled: bool = True
    router_min_score: float = Field(default=2, ge=0)
    router_min_term_coverage: float = Field(default=0.25, ge=0, le=1)
    router_max_candidates: int = Field(default=3, ge=1, le=20)
    router_enforce_required: bool = True
    router_max_gate_retries: int = Field(default=1, ge=0, le=3)


class SkillsConfig(StrictModel):
    path: str = "./skills"
    context_mode: Literal["history", "legacy"] = "history"
    auto_activate: bool = True
    max_auto_activated: int = Field(default=3, ge=0)
    max_catalog_chars: int = Field(default=8_000, gt=0)


class AgentsConfig(StrictModel):
    user_path: str = "~/.bot/agents"
    project_path: str = ".bot/agents"
    auto_resume_background: bool = False
    required_wait_timeout_seconds: float = Field(default=900, gt=0, le=86_400)


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
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
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
        compaction_reserved = (
            self.context.compaction_max_output_tokens
            + self.context.protocol_reserve_tokens
            + self.context.safety_margin_tokens
        )
        if compaction_reserved >= self.model.context_window_tokens:
            raise ValueError(
                "context 的 compaction output/protocol/safety reserve 总和必须小于 "
                "model.context_window_tokens"
            )
        if (
            self.context.compaction_summary_target_tokens is not None
            and self.context.compaction_summary_target_tokens
            > self.context.compaction_summary_tokens
        ):
            raise ValueError(
                "context.compaction_summary_target_tokens 不能大于 compaction_summary_tokens"
            )
        if self.subagents.max_concurrent > self.subagents.max_queued:
            raise ValueError("subagents.max_concurrent 不能大于 max_queued")
        worktree_dir = Path(self.subagents.worktree_dir)
        if not worktree_dir.parts or worktree_dir.is_absolute() or ".." in worktree_dir.parts:
            raise ValueError("subagents.worktree_dir 必须是工作区内相对路径")
        project_agents = Path(self.agents.project_path)
        if not project_agents.parts or project_agents.is_absolute() or ".." in project_agents.parts:
            raise ValueError("agents.project_path 必须是工作区内相对路径")
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

    def user_agent_path(self) -> Path:
        return Path(self.agents.user_path).expanduser().resolve()

    def project_agent_path(self, workspace: Path) -> Path:
        workspace = workspace.resolve()
        path = (workspace / self.agents.project_path).resolve()
        try:
            path.relative_to(workspace)
        except ValueError as exc:
            raise ValueError("agents.project_path 通过符号链接逃逸工作区") from exc
        return path

    def state_path(self, workspace: Path) -> Path:
        path = Path(self.storage.state_path).expanduser()
        return path.resolve() if path.is_absolute() else (workspace / path).resolve()

    def memory_path(self, workspace: Path) -> Path:
        path = Path(self.memory.path).expanduser()
        return path.resolve() if path.is_absolute() else (workspace / path).resolve()
