from __future__ import annotations

import asyncio
import os
import platform
import shutil
import signal
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from uuid import uuid4

from bot.execution.base import (
    EnvironmentCapabilities,
    ExecutionTarget,
    ProcessEvent,
    ProcessEventKind,
    ProcessSnapshot,
    ProcessSpec,
    ProcessStatus,
)


@dataclass
class _ManagedProcess:
    process_id: str
    spec: ProcessSpec
    process: asyncio.subprocess.Process
    started_at: float
    status: ProcessStatus = ProcessStatus.RUNNING
    returncode: int | None = None
    stdout_parts: list[str] = field(default_factory=list)
    stderr_parts: list[str] = field(default_factory=list)
    stdout_cursor: int = 0
    stderr_cursor: int = 0
    output_bytes: int = 0
    truncated: bool = False
    last_output_at: float | None = None
    termination_reason: str | None = None
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    readers: list[asyncio.Task[None]] = field(default_factory=list)
    monitor: asyncio.Task[None] | None = None
    timeout_task: asyncio.Task[None] | None = None


class LocalExecutionTarget(ExecutionTarget):
    def __init__(self, *, max_managed_processes: int = 16) -> None:
        if max_managed_processes < 1:
            raise ValueError("max_managed_processes 必须大于 0")
        self.max_managed_processes = max_managed_processes
        self._managed_processes: dict[str, _ManagedProcess] = {}

    async def probe(self, executables: list[str] | None = None) -> EnvironmentCapabilities:
        found = {name: shutil.which(name) for name in executables or []}
        return EnvironmentCapabilities(
            operating_system=platform.system().lower(),
            architecture=platform.machine().lower(),
            executables=found,
        )

    def execute(self, spec: ProcessSpec) -> AsyncIterator[ProcessEvent]:
        return self._execute(spec)

    @property
    def supports_managed_processes(self) -> bool:
        return True

    @property
    def has_live_processes(self) -> bool:
        return any(
            managed.process.returncode is None for managed in self._managed_processes.values()
        )

    async def start_process(self, spec: ProcessSpec) -> str:
        live_count = sum(
            managed.process.returncode is None for managed in self._managed_processes.values()
        )
        if live_count >= self.max_managed_processes:
            raise RuntimeError(
                f"受管进程达到上限 {self.max_managed_processes}；请先等待或终止现有进程"
            )
        cwd = Path(spec.cwd).resolve()
        if not cwd.is_dir():
            raise ValueError(f"工作目录不存在: {cwd}")

        environment = self._safe_environment()
        environment.update(spec.env)
        kwargs: dict[str, object] = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True

        process = await asyncio.create_subprocess_exec(
            *spec.argv,
            cwd=str(cwd),
            env=environment,
            stdin=(asyncio.subprocess.PIPE if spec.interactive else asyncio.subprocess.DEVNULL),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        process_id = f"proc_{uuid4().hex}"
        managed = _ManagedProcess(
            process_id=process_id,
            spec=spec.model_copy(update={"cwd": cwd}),
            process=process,
            started_at=monotonic(),
        )
        self._managed_processes[process_id] = managed
        managed.readers = [
            asyncio.create_task(self._capture_managed_stream(managed, process.stdout, "stdout")),
            asyncio.create_task(self._capture_managed_stream(managed, process.stderr, "stderr")),
        ]
        managed.monitor = asyncio.create_task(self._monitor_managed_process(managed))
        managed.timeout_task = asyncio.create_task(self._enforce_managed_timeout(managed))
        return process_id

    async def poll_process(
        self,
        process_id: str,
        *,
        wait_seconds: float = 0,
        consume_output: bool = True,
    ) -> ProcessSnapshot:
        managed = self._managed_process(process_id)
        if wait_seconds < 0:
            raise ValueError("wait_seconds 不能小于 0")
        if managed.status == ProcessStatus.RUNNING and wait_seconds > 0:
            try:
                await asyncio.wait_for(managed.finished.wait(), timeout=wait_seconds)
            except TimeoutError:
                pass
        return self._managed_snapshot(managed, consume_output=consume_output)

    async def send_process_input(
        self,
        process_id: str,
        data: str,
        *,
        eof: bool = False,
    ) -> ProcessSnapshot:
        managed = self._managed_process(process_id)
        if not managed.spec.interactive:
            raise ValueError(f"进程 {process_id} 未以 interactive 模式启动")
        if managed.status != ProcessStatus.RUNNING:
            raise ValueError(f"进程 {process_id} 当前状态为 {managed.status.value}")
        stdin = managed.process.stdin
        if stdin is None:
            raise ValueError(f"进程 {process_id} 没有可写 stdin")
        if data:
            stdin.write(data.encode())
            await stdin.drain()
        if eof:
            stdin.close()
        return self._managed_snapshot(managed, consume_output=False)

    async def terminate_process(
        self,
        process_id: str,
        *,
        reason: str | None = None,
    ) -> ProcessSnapshot:
        managed = self._managed_process(process_id)
        if managed.status == ProcessStatus.RUNNING:
            managed.status = ProcessStatus.CANCELLED
            managed.termination_reason = reason or "Agent 请求终止"
            await self._terminate(managed.process)
            await managed.finished.wait()
        return self._managed_snapshot(managed, consume_output=True)

    async def list_processes(self) -> list[ProcessSnapshot]:
        return [
            self._managed_snapshot(managed, consume_output=False, include_output=False)
            for managed in self._managed_processes.values()
        ]

    async def aclose(self) -> None:
        live = [
            managed
            for managed in self._managed_processes.values()
            if managed.process.returncode is None
        ]
        for managed in live:
            if managed.status == ProcessStatus.RUNNING:
                managed.status = ProcessStatus.CANCELLED
                managed.termination_reason = "Runtime 关闭时清理"
            await self._terminate(managed.process)
        if live:
            await asyncio.gather(*(managed.finished.wait() for managed in live))
        tasks = [
            task
            for managed in self._managed_processes.values()
            for task in (managed.monitor, managed.timeout_task, *managed.readers)
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._managed_processes.clear()

    def _managed_process(self, process_id: str) -> _ManagedProcess:
        try:
            return self._managed_processes[process_id]
        except KeyError as exc:
            raise ValueError(f"未知受管进程: {process_id}") from exc

    async def _capture_managed_stream(
        self,
        managed: _ManagedProcess,
        stream: asyncio.StreamReader | None,
        stream_name: str,
    ) -> None:
        if stream is None:
            return
        while chunk := await stream.read(8192):
            managed.last_output_at = monotonic()
            remaining = managed.spec.output_limit_bytes - managed.output_bytes
            if remaining <= 0:
                managed.truncated = True
                continue
            selected = chunk[:remaining]
            managed.output_bytes += len(selected)
            if len(selected) < len(chunk):
                managed.truncated = True
            text = selected.decode(errors="replace")
            if stream_name == "stdout":
                managed.stdout_parts.append(text)
            else:
                managed.stderr_parts.append(text)

    async def _monitor_managed_process(self, managed: _ManagedProcess) -> None:
        returncode = await managed.process.wait()
        await asyncio.gather(*managed.readers, return_exceptions=True)
        managed.returncode = returncode
        if managed.status == ProcessStatus.RUNNING:
            managed.status = ProcessStatus.COMPLETED if returncode == 0 else ProcessStatus.FAILED
        managed.finished.set()
        timeout_task = managed.timeout_task
        if timeout_task is not None and timeout_task is not asyncio.current_task():
            timeout_task.cancel()

    async def _enforce_managed_timeout(self, managed: _ManagedProcess) -> None:
        if managed.status != ProcessStatus.RUNNING:
            return
        try:
            await asyncio.sleep(managed.spec.timeout_seconds)
        except asyncio.CancelledError:
            return
        if managed.status != ProcessStatus.RUNNING:
            return
        managed.status = ProcessStatus.TIMED_OUT
        managed.termination_reason = f"达到 hard timeout {managed.spec.timeout_seconds:g} 秒"
        await self._terminate(managed.process)

    @staticmethod
    def _managed_snapshot(
        managed: _ManagedProcess,
        *,
        consume_output: bool,
        include_output: bool = True,
    ) -> ProcessSnapshot:
        stdout_all = "".join(managed.stdout_parts)
        stderr_all = "".join(managed.stderr_parts)
        if include_output:
            stdout = stdout_all[managed.stdout_cursor :]
            stderr = stderr_all[managed.stderr_cursor :]
        else:
            stdout = ""
            stderr = ""
        if consume_output:
            managed.stdout_cursor = len(stdout_all)
            managed.stderr_cursor = len(stderr_all)
        now = monotonic()
        elapsed = max(0.0, now - managed.started_at)
        last_output_seconds_ago = (
            max(0.0, now - managed.last_output_at)
            if managed.last_output_at is not None
            else elapsed
        )
        return ProcessSnapshot(
            process_id=managed.process_id,
            status=managed.status,
            argv=managed.spec.argv,
            cwd=managed.spec.cwd,
            elapsed_seconds=elapsed,
            hard_timeout_seconds=managed.spec.timeout_seconds,
            interactive=managed.spec.interactive,
            returncode=managed.returncode,
            stdout=stdout,
            stderr=stderr,
            truncated=managed.truncated,
            last_output_seconds_ago=last_output_seconds_ago,
            termination_reason=managed.termination_reason,
        )

    async def _execute(self, spec: ProcessSpec) -> AsyncIterator[ProcessEvent]:
        cwd = Path(spec.cwd).resolve()
        if not cwd.is_dir():
            raise ValueError(f"工作目录不存在: {cwd}")

        environment = self._safe_environment()
        environment.update(spec.env)
        kwargs: dict[str, object] = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True

        process = await asyncio.create_subprocess_exec(
            *spec.argv,
            cwd=str(cwd),
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        queue: asyncio.Queue[tuple[ProcessEventKind, bytes | None]] = asyncio.Queue()

        async def read_stream(stream: asyncio.StreamReader | None, kind: ProcessEventKind) -> None:
            if stream is None:
                await queue.put((kind, None))
                return
            while chunk := await stream.read(8192):
                await queue.put((kind, chunk))
            await queue.put((kind, None))

        readers = [
            asyncio.create_task(read_stream(process.stdout, ProcessEventKind.STDOUT)),
            asyncio.create_task(read_stream(process.stderr, ProcessEventKind.STDERR)),
        ]
        waiter = asyncio.create_task(process.wait())
        deadline = asyncio.get_running_loop().time() + spec.timeout_seconds
        completed_streams = 0
        emitted_bytes = 0
        truncated = False
        try:
            while completed_streams < 2:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError(f"命令执行超过 {spec.timeout_seconds:g} 秒: {spec.argv[0]}")
                try:
                    kind, chunk = await asyncio.wait_for(queue.get(), timeout=remaining)
                except TimeoutError:
                    raise TimeoutError(
                        f"命令执行超过 {spec.timeout_seconds:g} 秒: {spec.argv[0]}"
                    ) from None
                if chunk is None:
                    completed_streams += 1
                    continue
                if emitted_bytes >= spec.output_limit_bytes:
                    truncated = True
                    continue
                remaining_bytes = spec.output_limit_bytes - emitted_bytes
                selected = chunk[:remaining_bytes]
                emitted_bytes += len(selected)
                if len(selected) < len(chunk):
                    truncated = True
                yield ProcessEvent(
                    kind=kind,
                    data=selected.decode(errors="replace"),
                    truncated=truncated,
                )
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"命令执行超过 {spec.timeout_seconds:g} 秒: {spec.argv[0]}")
            try:
                returncode = await asyncio.wait_for(waiter, timeout=remaining)
            except TimeoutError:
                raise TimeoutError(
                    f"命令执行超过 {spec.timeout_seconds:g} 秒: {spec.argv[0]}"
                ) from None
            yield ProcessEvent(
                kind=ProcessEventKind.COMPLETED,
                returncode=returncode,
                truncated=truncated,
            )
        except (asyncio.CancelledError, TimeoutError):
            await self._terminate(process)
            raise
        finally:
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            if not waiter.done():
                waiter.cancel()

    @staticmethod
    def _safe_environment() -> dict[str, str]:
        allowed = {
            "PATH",
            "HOME",
            "USER",
            "LOGNAME",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TERM",
            "TMPDIR",
            "TEMP",
            "TMP",
            "SYSTEMROOT",
            "WINDIR",
        }
        return {key: value for key, value in os.environ.items() if key in allowed}

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        else:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    return
            else:
                process.kill()
            await process.wait()
