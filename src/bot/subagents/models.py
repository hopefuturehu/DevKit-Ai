from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class WorkerStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    LIMIT_REACHED = "limit_reached"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        return self in TERMINAL_WORKER_STATUSES


TERMINAL_WORKER_STATUSES = frozenset(
    {
        WorkerStatus.COMPLETED,
        WorkerStatus.BLOCKED,
        WorkerStatus.FAILED,
        WorkerStatus.LIMIT_REACHED,
        WorkerStatus.CANCELLED,
        WorkerStatus.INTERRUPTED,
    }
)


class WorkerIsolation(StrEnum):
    READ_ONLY = "read_only"
    WORKTREE = "worktree"


class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    instructions: str
    allowed_tools: list[str] = Field(default_factory=list)
    isolation: WorkerIsolation = WorkerIsolation.READ_ONLY
    max_steps: int | None = Field(default=None, ge=1)
    max_wall_time_seconds: float | None = Field(default=None, gt=0)
    max_cost_usd: float | None = Field(default=None, gt=0)
    explicit_skills: list[str] = Field(default_factory=list)


class AgentTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    parent_session_id: str
    parent_run_id: str
    child_session_id: str
    agent_name: str
    spec: AgentSpec
    objective: str
    constraints: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    context_refs: list[str] = Field(default_factory=list)
    required: bool = True
    status: WorkerStatus = WorkerStatus.QUEUED
    isolation: WorkerIsolation = WorkerIsolation.READ_ONLY
    workspace: str
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    result: dict | None = None
    reported_at: str | None = None


class AgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    child_session_id: str
    status: WorkerStatus
    summary: str = ""
    findings: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    worktree_path: str | None = None
    verification: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None


def agent_task_from_record(record: dict) -> AgentTask:
    return AgentTask.model_validate(record)
