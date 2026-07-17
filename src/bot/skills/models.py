from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Skill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    path: Path
    instructions: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value or len(value) > 64:
            raise ValueError("Skill name 长度必须为 1-64")
        if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in value):
            raise ValueError("Skill name 只能包含小写字母、数字、连字符和下划线")
        return value

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Skill description 不能为空")
        return value


class SkillDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path
    level: str
    message: str


class ActiveSkill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    reason: str
    explicit: bool
