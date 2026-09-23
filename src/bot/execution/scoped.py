"""A host-created invocation view over a shared native process registry."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bot.execution.base import (
    EnvironmentCapabilities,
    ExecutionTarget,
    ProcessEvent,
    ProcessSnapshot,
    ProcessSpec,
)
from bot.sandbox.models import ExecutionScope, SandboxError, SandboxErrorCode

if TYPE_CHECKING:
    from bot.execution.native import NativeSandboxExecutionTarget


@dataclass(frozen=True)
class InvocationIdentity:
    workspace: Path
    session_id: str
    run_id: str
    tool_call_id: str
    read_only: bool
    denied_paths: tuple[Path, ...]


class NativeInvocationTarget(ExecutionTarget):
    def __init__(self, target: NativeSandboxExecutionTarget, identity: InvocationIdentity):
        self._target = target
        self._identity = identity
        self._scope: ExecutionScope | None = None
        self._closed = False

    @property
    def requires_confinement(self) -> bool:
        return True

    @property
    def progress_scope(self) -> str:
        return self._target.progress_scope

    @property
    def supports_managed_processes(self) -> bool:
        return True

    def _check_open(self) -> None:
        if self._closed:
            raise SandboxError(SandboxErrorCode.CONFIG_INVALID, "工具调用作用域已释放")

    def _prepare_spec(self, spec: ProcessSpec) -> ProcessSpec:
        self._check_open()
        identity = self._identity
        if (
            spec.session_id not in (None, identity.session_id)
            or spec.run_id not in (None, identity.run_id)
            or spec.scope is not None
        ):
            raise SandboxError(SandboxErrorCode.CONFIG_INVALID, "工具不能替换调用身份或策略引用")
        self._bind()
        return spec.model_copy(
            deep=True,
            update={
                "session_id": identity.session_id,
                "run_id": identity.run_id,
                "scope": self._scope,
            },
        )

    def _bind(self) -> None:
        self._check_open()
        identity = self._identity
        if self._scope is None:
            self._scope = self._target.bind_scope(
                session_id=identity.session_id,
                run_id=identity.run_id,
                tool_call_id=identity.tool_call_id,
                read_only=identity.read_only,
                denied_paths=identity.denied_paths,
            )

    async def worker_operation(
        self,
        operation: str,
        arguments: dict,
        *,
        workspace_only: bool = True,
        output_limit: int = 1_000_000,
        timeout_seconds: float = 300,
    ):
        from bot.sandbox.client import execute_worker

        self._bind()
        assert self._scope is not None
        argv, request = self._target.prepare_worker(
            self._scope,
            operation,
            arguments,
            workspace_only=workspace_only,
            output_limit=output_limit,
        )
        return await execute_worker(
            self._target,
            self._prepare_spec(
                ProcessSpec(
                    argv=argv,
                    cwd=self._identity.workspace,
                    timeout_seconds=timeout_seconds,
                    interactive=True,
                    output_limit_bytes=8_010_000,
                )
            ),
            request,
        )

    def _authorize(self, process_id: str) -> None:
        self._check_open()
        self._target.authorize_process(
            process_id, session_id=self._identity.session_id, run_id=self._identity.run_id
        )

    async def probe(self, executables: list[str] | None = None) -> EnvironmentCapabilities:
        self._check_open()
        return await self._target.probe(executables)

    def execute(self, spec: ProcessSpec) -> AsyncIterator[ProcessEvent]:
        return self._target.execute(self._prepare_spec(spec))

    async def start_process(self, spec: ProcessSpec) -> str:
        return await self._target.start_process(self._prepare_spec(spec))

    async def poll_process(
        self, process_id: str, *, wait_seconds: float = 0, consume_output: bool = True
    ) -> ProcessSnapshot:
        self._authorize(process_id)
        return await self._target.poll_process(
            process_id, wait_seconds=wait_seconds, consume_output=consume_output
        )

    async def send_process_input(
        self, process_id: str, data: str, *, eof: bool = False
    ) -> ProcessSnapshot:
        self._authorize(process_id)
        return await self._target.send_process_input(process_id, data, eof=eof)

    async def terminate_process(
        self, process_id: str, *, reason: str | None = None
    ) -> ProcessSnapshot:
        self._authorize(process_id)
        return await self._target.terminate_process(process_id, reason=reason)

    async def list_processes(self) -> list[ProcessSnapshot]:
        self._check_open()
        return [
            snapshot
            for snapshot in await self._target.list_processes()
            if (snapshot.session_id, snapshot.run_id)
            == (self._identity.session_id, self._identity.run_id)
            and snapshot.execution_metadata.get("purpose") != "worker"
        ]

    async def read_process_output(
        self, process_id: str, *, stdout_offset: int = 0, stderr_offset: int = 0, limit: int = 16000
    ) -> dict:
        self._authorize(process_id)
        return await self._target.read_process_output(
            process_id, stdout_offset=stdout_offset, stderr_offset=stderr_offset, limit=limit
        )

    def release_invocation(self) -> None:
        self._closed = True
        if self._scope is not None:
            self._target.discard_scope(self._scope)

    async def aclose(self) -> None:
        # A tool view cannot shut down another tool's processes or the runtime.
        self.release_invocation()
