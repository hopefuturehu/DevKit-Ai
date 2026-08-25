from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ContextCompactionStatus(StrEnum):
    BUILDING = "building"
    READY = "ready"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class CompactionErrorClass(StrEnum):
    AUTHENTICATION = "authentication"
    PAYMENT = "payment"
    CONFIGURATION = "configuration"
    RATE_LIMIT = "rate_limit"
    TRANSPORT = "transport"
    TIMEOUT = "timeout"
    CONTEXT_OVERFLOW = "context_overflow"
    OUTPUT_LENGTH = "output_length"
    FORMAT = "format"
    EMPTY = "empty"
    PERSISTENCE = "persistence"
    REQUEST_BUDGET = "request_budget"
    UNKNOWN = "unknown"


class ContextCompactionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compacted: bool
    trigger: str
    compaction_id: str | None = None
    parent_id: str | None = None
    covered_start_position: int = 0
    covered_end_position: int = 0
    requested_end_position: int = 0
    previous_end_position: int = 0
    messages_compacted: int = 0
    summary_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    source_chars: int = 0
    summary_chars: int = 0
    source_refs: list[str] = Field(default_factory=list)
    rebuilt_from_raw: bool = False
    attempts: int = 0
    repair_attempts: int = 0
    condense_attempts: int = 0
    transport_retries: int = 0
    request_count: int = 0
    duration_ms: float = 0
    planned_input_tokens: int = 0
    input_limit: int = 0
    reason: str | None = None
    error: str | None = None
    error_class: CompactionErrorClass | None = None
