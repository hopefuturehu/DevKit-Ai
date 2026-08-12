from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    prompt: str
    workspace: str = "."
    explicit_skills: list[str] = Field(default_factory=list)
    expected_status: Literal[
        "completed", "failed", "cancelled", "limit_reached", "blocked"
    ] = "completed"
    final_contains: list[str] = Field(default_factory=list)
    final_not_contains: list[str] = Field(default_factory=list)
    files_contain: dict[str, list[str]] = Field(default_factory=dict)
    expected_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    expected_skills: list[str] = Field(default_factory=list)
    max_tool_calls: int | None = Field(default=None, ge=0)
    max_approval_requests: int | None = Field(default=None, ge=0)

    def workspace_path(self, base: Path) -> Path:
        path = Path(self.workspace).expanduser()
        return path.resolve() if path.is_absolute() else (base / path).resolve()


class EvalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    passed: bool
    status: str
    failures: list[str] = Field(default_factory=list)
    duration_seconds: float
    steps: int
    tool_calls: int
    tool_names: list[str] = Field(default_factory=list)
    activated_skills: list[str] = Field(default_factory=list)
    approval_requests: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None = None
    final_text: str = ""
