from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from bot.sandbox.models import ExecutionScope

if TYPE_CHECKING:
    from bot.tools.base import ToolResult


class ProcessSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    argv: list[str] = Field(min_length=1)
    session_id: str | None = None
    run_id: str | None = None
    scope: ExecutionScope | None = None
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
    TERMINATING = "terminating"
    CLEANUP_FAILED = "cleanup_failed"
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
    scope: ExecutionScope | None = None
    execution_metadata: dict = Field(default_factory=dict)
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
    cleanup_status: Literal[
        "not_requested", "pending", "observed_empty", "incomplete", "unknown"
    ] = "not_requested"
    containment: Literal["session_scan", "root_only"] = "root_only"
    output_complete: bool = True
    remaining_processes: list[dict] = Field(default_factory=list)
    cleanup_elapsed_seconds: float = Field(default=0, ge=0)
    cleanup_changed: bool = False


class EnvironmentCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operating_system: str
    architecture: str
    executables: dict[str, str | None] = Field(default_factory=dict)


class ExecutionTarget(ABC):
    async def worker_operation(
        self,
        operation: str,
        arguments: dict,
        *,
        workspace_only: bool = True,
        output_limit: int = 1_000_000,
        timeout_seconds: float = 300,
    ) -> ToolResult:
        from bot.sandbox.models import SandboxError, SandboxErrorCode

        raise SandboxError(SandboxErrorCode.UNAVAILABLE, "当前目标没有受限文件 worker")

    @property
    def requires_confinement(self) -> bool:
        return False

    def for_invocation(
        self,
        *,
        workspace: Path,
        session_id: str,
        run_id: str,
        tool_call_id: str,
        read_only: bool = False,
        denied_paths: tuple[Path, ...] = (),
    ) -> ExecutionTarget:
        """Bind trusted caller identity without changing shared target state."""
        return self

    def release_invocation(self) -> None:
        """Release an invocation view, not its background processes or shared runtime."""
        return None

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


CleanupObserver = Callable[[ProcessSpec, str, dict], Awaitable[None]]


class ManagedExecutionTarget(ExecutionTarget):
    cleanup_observer: CleanupObserver | None = None

    @property
    @abstractmethod
    def cleanup_scope(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def has_live_processes(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def restore_cleanups(self, records: list[dict]) -> None:
        raise NotImplementedError
