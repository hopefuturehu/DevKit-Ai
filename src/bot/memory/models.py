from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MemoryKind(StrEnum):
    USER_PREFERENCE = "user_preference"
    WORKSPACE_FACT = "workspace_fact"
    DECISION = "decision"
    PROCEDURE = "procedure"
    PITFALL = "pitfall"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    CONFLICT = "conflict"
    STALE = "stale"
    SUPERSEDED = "superseded"
    FORGOTTEN = "forgotten"


class MemoryEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    run_id: str
    positions: list[int] = Field(min_length=1, max_length=20)


class MemoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    key: str
    kind: MemoryKind
    scope: Literal["global", "workspace"]
    origin: Literal["user", "auto", "legacy", "manual"]
    status: MemoryStatus
    content: str
    confidence: float = Field(ge=0, le=1)
    created_at: str
    updated_at: str
    evidence: list[MemoryEvidence] = Field(default_factory=list)
    path: str


class ExtractedMemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: MemoryKind
    scope: Literal["workspace"] = "workspace"
    memory_key: str = Field(min_length=3, max_length=120)
    content: str = Field(min_length=1, max_length=2_000)
    confidence: float = Field(ge=0, le=1)
    evidence_positions: list[int] = Field(min_length=1, max_length=20)


class MemoryExtractionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[ExtractedMemoryCandidate] = Field(default_factory=list)


class MemoryConsolidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    added: int = 0
    merged: int = 0
    conflicts: int = 0
    suppressed: int = 0
