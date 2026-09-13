from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class ProcessSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    argv: list[str] = Field(min_length=1)
    session_id: str | None = None
    run_id: str | None = None
    cwd: Path
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float | None = Field(default=None, gt=0)
    output_limit_bytes: int = Field(default=1_000_000, gt=0)
    interactive: bool = False


class ProcessEventKind(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"
    COMPLETED = "completed"


class ProcessStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class ProcessEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ProcessEventKind
    data: str = ""
    returncode: int | None = None
    truncated: bool = False


class ProcessSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    process_id: str
    session_id: str | None = None
    run_id: str | None = None
    status: ProcessStatus
    argv: list[str]
    cwd: Path
    elapsed_seconds: float = Field(ge=0)
    hard_timeout_seconds: float | None = Field(default=None, gt=0)
    interactive: bool = False
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    # Digests cover retained output since launch, not just the latest poll.
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None
    truncated: bool = False
    last_output_seconds_ago: float | None = Field(default=None, ge=0)
    termination_reason: str | None = None


class EnvironmentCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operating_system: str
    architecture: str
    executables: dict[str, str | None] = Field(default_factory=dict)


class ExecutionTarget(ABC):
    @property
    def progress_scope(self) -> str:
        """Override with a stable target/environment identity for cross-run restore."""
        return self.__dict__.setdefault("_progress_scope", uuid4().hex)

    @abstractmethod
    async def probe(self, executables: list[str] | None = None) -> EnvironmentCapabilities:
        raise NotImplementedError

    @abstractmethod
    def execute(self, spec: ProcessSpec) -> AsyncIterator[ProcessEvent]:
        raise NotImplementedError

    @property
    def supports_managed_processes(self) -> bool:
        return False

    async def start_process(self, spec: ProcessSpec) -> str:
        raise NotImplementedError("当前执行目标不支持受管进程")

    async def poll_process(
        self,
        process_id: str,
        *,
        wait_seconds: float = 0,
        consume_output: bool = True,
    ) -> ProcessSnapshot:
        raise NotImplementedError("当前执行目标不支持受管进程")

    async def send_process_input(
        self,
        process_id: str,
        data: str,
        *,
        eof: bool = False,
    ) -> ProcessSnapshot:
        raise NotImplementedError("当前执行目标不支持受管进程")

    async def terminate_process(
        self,
        process_id: str,
        *,
        reason: str | None = None,
    ) -> ProcessSnapshot:
        raise NotImplementedError("当前执行目标不支持受管进程")

    async def list_processes(self) -> list[ProcessSnapshot]:
        return []

    async def read_process_output(
        self,
        process_id: str,
        *,
        stdout_offset: int = 0,
        stderr_offset: int = 0,
        limit: int = 16000,
    ) -> dict:
        raise NotImplementedError("当前执行目标不支持独立日志读取")

    async def aclose(self) -> None:
        return None
