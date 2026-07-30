from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ContextCompactionStatus(StrEnum):
    BUILDING = "building"
    READY = "ready"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class ContextCompactionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compacted: bool
    trigger: str
    compaction_id: str | None = None
    parent_id: str | None = None
    covered_start_position: int = 0
    covered_end_position: int = 0
    previous_end_position: int = 0
    messages_compacted: int = 0
    summary_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    source_chars: int = 0
    summary_chars: int = 0
    source_refs: list[str] = Field(default_factory=list)
    rebuilt_from_raw: bool = False
    reason: str | None = None
    error: str | None = None
