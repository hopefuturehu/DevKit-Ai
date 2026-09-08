from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def relative_path(value: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or "\x00" in value:
        raise ValueError("必须是无 .. 的非空相对路径")
    return path.as_posix()


class JsonVerifier(EvalModel):
    kind: Literal["json"] = "json"
    path: str
    schema_: dict[str, Any] = Field(alias="schema")
    fresh: bool = True

    _path = field_validator("path")(relative_path)


class CommandVerifier(EvalModel):
    kind: Literal["command"] = "command"
    # Evaluator-owned directory, relative to the case file, never an Agent output.
    tests: str
    image: str = Field(min_length=1)
    argv: list[str] = Field(min_length=1)
    timeout_seconds: float = Field(default=120, gt=0, le=3600)

    @field_validator("argv")
    @classmethod
    def valid_argv(cls, value: list[str]) -> list[str]:
        if any(not part or "\x00" in part for part in value):
            raise ValueError("验收命令参数不能为空或包含 NUL")
        return value


Verifier = Annotated[JsonVerifier | CommandVerifier, Field(discriminator="kind")]


class ToolExpectation(EvalModel):
    name: str = Field(min_length=1)
    arguments_contain: dict[str, Any] = Field(default_factory=dict)
    success: bool = True
    status: Literal["completed", "failed", "timed_out", "cancelled"] = "completed"
    returncode: int | None = None
    min_count: int = Field(default=1, ge=1)


class CheckResult(EvalModel):
    name: str
    verdict: Literal["pass", "fail", "error"]
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class EvalCase(EvalModel):
    id: str
    prompt: str
    # Backwards-compatible name: this is now the input fixture, never a live workspace.
    workspace: str = "."
    fixture_files: list[str] | None = None
    memory_fixture: str | None = None
    state_fixture: str | None = None
    max_snapshot_bytes: int = Field(default=64 * 1024 * 1024, gt=0, le=1024**3)
    explicit_skills: list[str] = Field(default_factory=list)
    expected_status: Literal["completed", "failed", "cancelled", "limit_reached", "blocked"] = (
        "completed"
    )
    final_contains: list[str] = Field(default_factory=list)
    final_not_contains: list[str] = Field(default_factory=list)
    files_contain: dict[str, list[str]] = Field(default_factory=dict)
    files_contain_require_change: bool = True
    required_changes: list[str] = Field(default_factory=list)
    forbidden_changes: list[str] = Field(default_factory=list)
    verifiers: list[Verifier] = Field(default_factory=list)
    expected_tools: list[str] = Field(default_factory=list)
    tool_results: list[ToolExpectation] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    expected_skills: list[str] = Field(default_factory=list)
    max_tool_calls: int | None = Field(default=None, ge=0)
    max_approval_requests: int | None = Field(default=None, ge=0)

    @field_validator("fixture_files", "required_changes", "forbidden_changes")
    @classmethod
    def relative_paths(cls, value: list[str] | None) -> list[str] | None:
        if value is not None:
            for path in value:
                relative_path(path)
        return value

    @field_validator("files_contain")
    @classmethod
    def relative_files(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        for path in value:
            relative_path(path)
        return value

    def workspace_path(self, base: Path) -> Path:
        path = Path(self.workspace).expanduser()
        return path.resolve() if path.is_absolute() else (base / path).resolve()


class EvalResult(EvalModel):
    schema_version: int = 2
    id: str
    attempt_id: str = ""
    passed: bool
    verdict: Literal["pass", "fail", "error"] = "error"
    run_status: str = "not_started"
    failure_kind: (
        Literal["task", "environment", "provider", "budget", "verifier", "runtime"] | None
    ) = None
    # Retained for existing JSONL consumers; mirrors run_status.
    status: str
    checks: list[CheckResult] = Field(default_factory=list)
    manifest: dict[str, Any] = Field(default_factory=dict)
    artifact_dir: str | None = None
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
