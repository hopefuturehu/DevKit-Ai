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
    process_group_id: int | None
    started_at: float
    finished_at: float | None = None
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
    termination_complete: asyncio.Event = field(default_factory=asyncio.Event)
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
            self._managed_process_alive(managed) for managed in self._managed_processes.values()
        )

    async def start_process(self, spec: ProcessSpec) -> str:
        live_count = sum(
            self._managed_process_alive(managed) for managed in self._managed_processes.values()
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

        process = await self._spawn_process(
            spec.argv,
            cwd=str(cwd),
            environment=environment,
            stdin=(asyncio.subprocess.PIPE if spec.interactive else asyncio.subprocess.DEVNULL),
            **kwargs,
        )
        process_id = f"proc_{uuid4().hex}"
        managed = _ManagedProcess(
            process_id=process_id,
            spec=spec.model_copy(update={"cwd": cwd}),
            process=process,
            process_group_id=process.pid if os.name == "posix" else None,
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
        if self._managed_process_alive(managed):
            if managed.status == ProcessStatus.RUNNING:
                managed.status = ProcessStatus.CANCELLED
                managed.termination_reason = reason or "Agent 请求终止"
            try:
                await self._terminate(
                    managed.process,
                    process_group_id=managed.process_group_id,
                )
            finally:
                managed.termination_complete.set()
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
            if self._managed_process_alive(managed)
        ]
        errors: list[BaseException] = []
        for managed in live:
            if managed.status == ProcessStatus.RUNNING:
                managed.status = ProcessStatus.CANCELLED
                managed.termination_reason = "Runtime 关闭时清理"
            try:
                await self._terminate(
                    managed.process,
                    process_group_id=managed.process_group_id,
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                managed.termination_complete.set()
        if live:
            try:
                async with asyncio.timeout(5):
                    await asyncio.gather(*(managed.finished.wait() for managed in live))
            except BaseException as exc:
                errors.append(exc)
        if errors:
            primary, *additional = errors
            for error in additional:
                primary.add_note(f"额外的进程清理错误: {type(error).__name__}: {error}")
            raise primary
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

    @classmethod
    def _managed_process_alive(cls, managed: _ManagedProcess) -> bool:
        if managed.finished.is_set():
            return False
        if managed.process.returncode is None:
            return True
        if managed.termination_complete.is_set():
            return False
        return cls._process_group_alive(managed.process_group_id)

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
        managed.returncode = returncode
        await asyncio.gather(*managed.readers, return_exceptions=True)
        await self._wait_for_process_group_exit(
            managed.process_group_id,
            stop_event=managed.termination_complete,
        )
        managed.finished_at = monotonic()
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
        try:
            await self._terminate(
                managed.process,
                process_group_id=managed.process_group_id,
            )
        finally:
            managed.termination_complete.set()

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
        elapsed = max(0.0, (managed.finished_at or now) - managed.started_at)
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

        process = await self._spawn_process(
            spec.argv,
            cwd=str(cwd),
            environment=environment,
            stdin=asyncio.subprocess.DEVNULL,
            **kwargs,
        )
        process_group_id = process.pid if os.name == "posix" else None
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
        completed = False
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
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0 or not await self._wait_for_process_group_exit(
                process_group_id,
                timeout_seconds=max(0.0, remaining),
            ):
                raise TimeoutError(f"命令执行超过 {spec.timeout_seconds:g} 秒: {spec.argv[0]}")
            completed = True
            yield ProcessEvent(
                kind=ProcessEventKind.COMPLETED,
                returncode=returncode,
                truncated=truncated,
            )
        finally:
            if not completed:
                await self._terminate(process, process_group_id=process_group_id)
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

    @classmethod
    async def _spawn_process(
        cls,
        argv: list[str],
        *,
        cwd: str,
        environment: dict[str, str],
        stdin: int,
        **kwargs: object,
    ) -> asyncio.subprocess.Process:
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=environment,
                stdin=stdin,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
        )
        try:
            return await asyncio.shield(spawn)
        except asyncio.CancelledError:
            process = await spawn
            await cls._terminate(
                process,
                process_group_id=process.pid if os.name == "posix" else None,
            )
            raise

    @staticmethod
    def _process_group_alive(process_group_id: int | None) -> bool:
        if os.name != "posix" or process_group_id is None:
            return False
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @classmethod
    async def _wait_for_process_group_exit(
        cls,
        process_group_id: int | None,
        *,
        timeout_seconds: float | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> bool:
        if os.name != "posix" or process_group_id is None:
            return True
        deadline = (
            asyncio.get_running_loop().time() + timeout_seconds
            if timeout_seconds is not None
            else None
        )
        while cls._process_group_alive(process_group_id):
            if stop_event is not None and stop_event.is_set():
                return False
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    @classmethod
    async def _terminate(
        cls,
        process: asyncio.subprocess.Process,
        *,
        process_group_id: int | None = None,
    ) -> None:
        if os.name == "posix":
            group_id = process_group_id or process.pid
            try:
                os.killpg(group_id, signal.SIGTERM)
            except ProcessLookupError:
                pass
            else:
                exited = await cls._wait_for_process_group_exit(group_id, timeout_seconds=2)
                if not exited:
                    try:
                        os.killpg(group_id, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await cls._wait_for_process_group_exit(group_id, timeout_seconds=2)
        else:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except TimeoutError:
                    process.kill()
                    await process.wait()
                return

        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
