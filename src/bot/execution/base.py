from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ProcessSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    argv: list[str] = Field(min_length=1)
    cwd: Path
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=300, gt=0)
    output_limit_bytes: int = Field(default=1_000_000, gt=0)


class ProcessEventKind(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"
    COMPLETED = "completed"


class ProcessEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ProcessEventKind
    data: str = ""
    returncode: int | None = None
    truncated: bool = False


class EnvironmentCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operating_system: str
    architecture: str
    executables: dict[str, str | None] = Field(default_factory=dict)


class ExecutionTarget(ABC):
    @abstractmethod
    async def probe(self, executables: list[str] | None = None) -> EnvironmentCapabilities:
        raise NotImplementedError

    @abstractmethod
    def execute(self, spec: ProcessSpec) -> AsyncIterator[ProcessEvent]:
        raise NotImplementedError
