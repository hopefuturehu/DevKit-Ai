from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictMemoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EpisodeStatus(StrEnum):
    PENDING = "pending"
    CONSOLIDATED = "consolidated"
    ARCHIVED = "archived"


class ConsolidationStatus(StrEnum):
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"


class MemoryKind(StrEnum):
    PREFERENCE = "preference"
    PROJECT = "project"
    LESSON = "lesson"
    DECISION = "decision"
    ERROR = "error"
    CONSTRAINT = "constraint"
    ARTIFACT = "artifact"
    VERIFICATION = "verification"
    TASK = "task"


class MemoryScope(StrEnum):
    SESSION = "session"
    WORKSPACE = "workspace"


class MemoryOperation(StrEnum):
    UPSERT = "upsert"
    RESOLVE = "resolve"
    RETRACT = "retract"


class MemoryCardStatus(StrEnum):
    ACTIVE = "active"
    RESOLVED = "resolved"
    RETRACTED = "retracted"
    STALE = "stale"


class EpisodeSummary(StrictMemoryModel):
    episode_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=2_000)
    keywords: list[str] = Field(default_factory=list, max_length=20)


class MemoryCandidate(StrictMemoryModel):
    operation: MemoryOperation
    kind: MemoryKind
    scope: MemoryScope
    content: str = Field(min_length=1, max_length=2_000)
    target_memory_id: str | None = Field(default=None, max_length=128)
    source_positions: list[int] = Field(min_length=1, max_length=50)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_target(self) -> MemoryCandidate:
        if self.operation in {MemoryOperation.RESOLVE, MemoryOperation.RETRACT}:
            if not self.target_memory_id:
                raise ValueError(f"{self.operation.value} 操作必须提供 target_memory_id")
        return self


class ConsolidationPayload(StrictMemoryModel):
    episodes: list[EpisodeSummary] = Field(min_length=1)
    candidates: list[MemoryCandidate] = Field(default_factory=list, max_length=200)


class VerifiedMemoryCandidate(StrictMemoryModel):
    candidate: MemoryCandidate
    accepted: bool
    rejection_reason: str | None = None


class ConsolidationResult(StrictMemoryModel):
    consolidated: bool
    trigger: str
    consolidation_id: str | None = None
    episodes_consolidated: int = 0
    candidates_accepted: int = 0
    candidates_rejected: int = 0
    cards_created: int = 0
    cards_updated: int = 0
    cards_staled: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reason: str | None = None
    error: str | None = None
