from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class PlanStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class PlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=500)
    status: PlanStatus

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("TODO 内容不能为空")
        return normalized


class PlanUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    explanation: str | None = Field(default=None, max_length=1_000)
    items: list[PlanItem] = Field(max_length=64)

    @field_validator("explanation")
    @classmethod
    def normalize_explanation(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_plan(self) -> PlanUpdate:
        contents = [item.content for item in self.items]
        if len(contents) != len(set(contents)):
            raise ValueError("TODO 内容不能重复")
        active = sum(item.status == PlanStatus.IN_PROGRESS for item in self.items)
        if active > 1:
            raise ValueError("最多只能有一个 in_progress TODO")
        return self


def validate_plan_payload(payload: Any) -> dict[str, Any] | None:
    """Validate a persisted projection before it re-enters trusted runtime context."""

    try:
        return PlanUpdate.model_validate(payload).model_dump(mode="json")
    except ValidationError:
        return None
