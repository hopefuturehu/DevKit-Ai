from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProgressKind(StrEnum):
    NONE = "none"
    WEAK = "weak"
    STRONG = "strong"
    WAITING = "waiting"


class ProgressSignal(BaseModel):
    """Machine-readable progress evidence returned by a Tool.

    ``evidence_key`` must identify the semantic result and omit volatile fields
    such as elapsed time.  It is persisted only as a hash by the controller.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ProgressKind
    summary: str = ""
    evidence_key: str | None = None
    inactivity_seconds: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_waiting_fields(self) -> ProgressSignal:
        if self.kind != ProgressKind.WAITING and self.inactivity_seconds is not None:
            raise ValueError("只有 waiting 进展信号可以携带 inactivity_seconds")
        return self
