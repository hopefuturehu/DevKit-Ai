from __future__ import annotations

import asyncio
import hashlib
import os
import platform
import shutil
import signal
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import monotonic
from uuid import uuid4

from bot.config.models import ProcessCleanupConfig
from bot.execution.base import (
    EnvironmentCapabilities,
    ExecutionTarget,
    ProcessEvent,
    ProcessEventKind,
    ProcessSnapshot,
    ProcessSpec,
    ProcessStatus,
)
from bot.execution.process_scope import ProcessIdentity, ProcessInventory, ProcessScope
from bot.execution.process_transport import ProcessTransport


@dataclass
class _ManagedProcess:
    process_id: str
    spec: ProcessSpec
    process: ProcessTransport
    scope: ProcessScope
    started_at: float
    finished_at: float | None = None
    status: ProcessStatus = ProcessStatus.RUNNING
    stdout_cursor: int = 0
    stderr_cursor: int = 0
    termination_reason: str | None = None
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    cleanup_task: asyncio.Task[None] | None = None
    cleanup_status: str = "not_requested"
    cleanup_elapsed: float = 0
    cleanup_generation: int = 0
    reported_cleanup_generation: int = 0
    timeout_task: asyncio.Task[None] | None = None


CleanupObserver = Callable[[ProcessSpec, str, dict], Awaitable[None]]


class LocalExecutionTarget(ExecutionTarget):
    @property
    def cleanup_scope(self) -> str:
        import psutil

        from bot.core.termination.identity import fingerprint

        return fingerprint(self.progress_scope, psutil.boot_time())

    def restore_cleanups(self, records: list[dict]) -> None:
        for record in records:
            if record.get("cleanup_scope") != self.cleanup_scope:
                continue
            scope = ProcessScope(record["scope_root_pid"])
            scope.known = {
                item["pid"]: ProcessIdentity(**item) for item in record["known_processes"]
            }
            scope.live = dict(scope.known)
            scope.unknown = record["cleanup_status"] == "unknown"
            protocol = ProcessTransport(0)
            protocol.root_exited.set()
            protocol.pipes_closed.set()
            protocol.cut_off = True
            managed = _ManagedProcess(
                record["process_id"],
                ProcessSpec.model_validate(record["spec"]),
                protocol,
                scope,
                monotonic(),
                status=ProcessStatus.CLEANUP_FAILED,
                cleanup_status=record["cleanup_status"],
                cleanup_generation=1,
            )
            managed.finished.set()
            self._managed_processes[managed.process_id] = managed

    @property
    def progress_scope(self) -> str:
        from bot.core.termination.identity import fingerprint

        return fingerprint("local", socket.gethostname(), self._safe_environment())

    def __init__(
        self,
        *,
        max_managed_processes: int = 16,
        cleanup: ProcessCleanupConfig | None = None,
    ) -> None:
        if max_managed_processes < 1:
            raise ValueError("max_managed_processes 必须大于 0")
        self.max_managed_processes = max_managed_processes
        self.cleanup = cleanup or ProcessCleanupConfig()
        self.cleanup_observer: CleanupObserver | None = None
        self._managed_processes: dict[str, _ManagedProcess] = {}
        self._inventory = ProcessInventory()
        self._monitor: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()
        self._closed = False

    async def probe(self, executables: list[str] | None = None) -> EnvironmentCapabilities:
        return EnvironmentCapabilities(
            operating_system=platform.system().lower(),
            architecture=platform.machine().lower(),
            executables={name: shutil.which(name) for name in executables or []},
        )

    def execute(self, spec: ProcessSpec) -> AsyncIterator[ProcessEvent]:
        return self._execute(spec)

    @property
    def supports_managed_processes(self) -> bool:
        return True

    @property
    def has_live_processes(self) -> bool:
        return any(self._managed_process_alive(p) for p in self._managed_processes.values())

    @staticmethod
    def _managed_process_alive(managed: _ManagedProcess) -> bool:
        # An unresolved cleanup remains charged against the process quota.
        return not managed.finished.is_set() or managed.status == ProcessStatus.CLEANUP_FAILED

    async def start_process(self, spec: ProcessSpec) -> str:
        async with self._start_lock:
            if self._closed:
                raise RuntimeError("执行环境已经关闭")
            if (
                sum(self._managed_process_alive(p) for p in self._managed_processes.values())
                >= self.max_managed_processes
            ):
                raise RuntimeError(
                    f"受管进程达到上限 {self.max_managed_processes}；请先等待或终止现有进程"
                )
            if any(
                p.status == ProcessStatus.CLEANUP_FAILED and p.spec.session_id == spec.session_id
                for p in self._managed_processes.values()
            ):
                raise RuntimeError("process_cleanup_incomplete: 当前会话仍有未解决的进程清理")
            cwd = Path(spec.cwd).resolve()
            if not cwd.is_dir():
                raise ValueError(f"工作目录不存在: {cwd}")
            spec = spec.model_copy(update={"cwd": cwd})
            environment = self._safe_environment()
            environment.update(spec.env)
            protocol = ProcessTransport(spec.output_limit_bytes)
            spawning = asyncio.create_task(
                asyncio.get_running_loop().subprocess_exec(
                    lambda: protocol,
                    *spec.argv,
                    cwd=str(cwd),
                    env=environment,
                    stdin=asyncio.subprocess.PIPE
                    if spec.interactive
                    else asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **({"start_new_session": True} if os.name == "posix" else {}),
                )
            )
            cancelled = False
            try:
                await asyncio.shield(spawning)
            except asyncio.CancelledError:
                # Spawn may complete at the cancellation boundary: register and clean it too.
                cancelled = True
                while not spawning.done():
                    try:
                        await asyncio.shield(spawning)
                    except asyncio.CancelledError:
                        continue
                spawning.result()
            process_id = f"proc_{uuid4().hex}"
            managed = _ManagedProcess(
                process_id, spec, protocol, ProcessScope(protocol.pid), monotonic()
            )
            self._managed_processes[process_id] = managed
            if self._monitor is None or self._monitor.done():
                self._monitor = asyncio.create_task(self._monitor_processes())
            if spec.timeout_seconds is not None:
                managed.timeout_task = asyncio.create_task(self._enforce_managed_timeout(managed))
            if cancelled:
                await self.terminate_process(process_id, reason="启动期间取消")
                raise asyncio.CancelledError
            return process_id

    async def _monitor_processes(self) -> None:
        while not self._closed:
            active = [p for p in self._managed_processes.values() if not p.finished.is_set()]
            if not active:
                return
            exited = {
                p.process_id
                for p in active
                if p.process.root_exited.is_set() and p.process.pipes_closed.is_set()
            }
            try:
                async with asyncio.timeout(0.5):
                    inventory = await self._inventory.read()
                for managed in active:
                    managed.scope.update(inventory)
                    process = managed.process
                    if (
                        managed.status == ProcessStatus.RUNNING
                        and managed.process_id in exited
                        and inventory.started_at >= (process.root_exited_at or 0)
                        and process.root_exited.is_set()
                        and process.pipes_closed.is_set()
                        and not managed.scope.live
                        and not managed.scope.unknown
                    ):
                        managed.status = (
                            ProcessStatus.COMPLETED
                            if process.returncode == 0 and not process.cut_off
                            else ProcessStatus.FAILED
                        )
                        self._finish(managed)
                        process.close()
            except (TimeoutError, OSError):
                # An unavailable inventory must not turn a live/unknown scope into success.
                pass
            await asyncio.sleep(0.25)

    @staticmethod
    def _finish(managed: _ManagedProcess) -> None:
        managed.finished_at = monotonic()
        managed.finished.set()
        managed.process.changed.set()
        if managed.timeout_task is not None and managed.timeout_task is not asyncio.current_task():
            managed.timeout_task.cancel()

    async def _enforce_managed_timeout(self, managed: _ManagedProcess) -> None:
        assert managed.spec.timeout_seconds is not None
        await asyncio.sleep(managed.spec.timeout_seconds)
        if managed.status == ProcessStatus.RUNNING:
            self._begin_cleanup(
                managed,
                f"达到 hard timeout {managed.spec.timeout_seconds:g} 秒",
                ProcessStatus.TIMED_OUT,
            )
            assert managed.cleanup_task is not None
            await asyncio.shield(managed.cleanup_task)

    def _begin_cleanup(
        self,
        managed: _ManagedProcess,
        reason: str,
        final_status: ProcessStatus,
    ) -> bool:
        if managed.cleanup_task is not None and not managed.cleanup_task.done():
            return False
        if managed.finished.is_set() and managed.status != ProcessStatus.CLEANUP_FAILED:
            return False
        managed.finished.clear()
        managed.finished_at = None
        managed.status = ProcessStatus.TERMINATING
        managed.cleanup_status = "pending"
        managed.termination_reason = reason
        managed.cleanup_generation += 1
        managed.cleanup_task = asyncio.create_task(self._cleanup_process(managed, final_status))
        return True

    async def _cleanup_process(self, managed: _ManagedProcess, final_status: ProcessStatus) -> None:
        started = monotonic()
        deadline = started + self.cleanup.total_timeout_seconds
        succeeded = False
        try:
            async with asyncio.timeout_at(deadline):
                for sig, grace in (
                    (signal.SIGTERM, self.cleanup.term_grace_seconds),
                    (getattr(signal, "SIGKILL", signal.SIGTERM), self.cleanup.kill_grace_seconds),
                ):
                    stage_end = min(deadline, monotonic() + grace)
                    while True:
                        managed.scope.update(await self._inventory.read())
                        managed.scope.send(sig, deadline=deadline)
                        if not managed.scope.live and not managed.scope.unknown:
                            break
                        if monotonic() >= stage_end:
                            break
                        await asyncio.sleep(min(0.05, max(0, stage_end - monotonic())))
                    if not managed.scope.live and not managed.scope.unknown:
                        break
                # Reap callback and pipes are independently awaited under the SAME deadline.
                drain_end = min(deadline, monotonic() + self.cleanup.drain_grace_seconds)
                async with asyncio.timeout_at(drain_end):
                    await managed.process.root_exited.wait()
                    await managed.process.pipes_closed.wait()
                    inventory = await self._inventory.read()
                    if inventory.started_at < (managed.process.root_exited_at or 0):
                        inventory = await self._inventory.read()
                    managed.scope.update(inventory)
                succeeded = (
                    not managed.scope.live
                    and not managed.scope.unknown
                    and (not managed.process.cut_off or managed.cleanup_generation > 1)
                )
        except Exception as exc:
            if not managed.scope.live:
                managed.scope.unknown = True
            managed.termination_reason = (
                f"{managed.termination_reason}; 清理未完成: {type(exc).__name__}"
            )
        finally:
            managed.cleanup_elapsed = monotonic() - started
            managed.cleanup_status = (
                "observed_empty"
                if succeeded
                else "unknown"
                if managed.scope.unknown
                else "incomplete"
            )
            managed.status = final_status if succeeded else ProcessStatus.CLEANUP_FAILED
            managed.process.close()
            self._finish(managed)
        if self.cleanup_observer is not None:
            # Observer failure must not undo cleanup or keep a tool waiting indefinitely.
            try:
                async with asyncio.timeout(max(0.001, deadline - monotonic())):
                    await self.cleanup_observer(
                        managed.spec,
                        managed.process_id,
                        {
                            **self._managed_snapshot(managed, consume_output=False).model_dump(
                                mode="json"
                            ),
                            "cleanup_scope": self.cleanup_scope,
                            "scope_root_pid": managed.scope.root_pid,
                            "known_processes": [asdict(p) for p in managed.scope.known.values()],
                            "spec": managed.spec.model_dump(mode="json"),
                        },
                    )
            except Exception as exc:
                managed.termination_reason = (
                    f"{managed.termination_reason}; 清理记录写入失败: {type(exc).__name__}"
                )

    async def terminate_process(
        self, process_id: str, *, reason: str | None = None
    ) -> ProcessSnapshot:
        managed = self._managed_process(process_id)
        self._begin_cleanup(managed, reason or "Agent 请求终止", ProcessStatus.CANCELLED)
        if managed.cleanup_task is not None:
            await asyncio.shield(managed.cleanup_task)
        changed = managed.reported_cleanup_generation < managed.cleanup_generation
        managed.reported_cleanup_generation = managed.cleanup_generation
        return self._managed_snapshot(managed, consume_output=True).model_copy(
            update={"cleanup_changed": changed}
        )

    async def poll_process(
        self,
        process_id: str,
        *,
        wait_seconds: float = 0,
        consume_output: bool = True,
    ) -> ProcessSnapshot:
        if wait_seconds < 0:
            raise ValueError("wait_seconds 不能小于 0")
        managed = self._managed_process(process_id)
        if not managed.finished.is_set() and wait_seconds > 0:
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
        await managed.process.write(data.encode(), eof=eof)
        return self._managed_snapshot(managed, consume_output=False)

    async def list_processes(self) -> list[ProcessSnapshot]:
        return [
            self._managed_snapshot(p, consume_output=False, include_output=False)
            for p in self._managed_processes.values()
        ]

    async def read_process_output(
        self,
        process_id: str,
        *,
        stdout_offset: int = 0,
        stderr_offset: int = 0,
        limit: int = 16000,
    ) -> dict:
        managed = self._managed_process(process_id)
        stdout, stderr = "".join(managed.process.stdout), "".join(managed.process.stderr)
        return {
            "snapshot": self._managed_snapshot(
                managed, consume_output=False, include_output=False
            ).model_dump(mode="json"),
            "stdout": stdout[stdout_offset : stdout_offset + limit],
            "stderr": stderr[stderr_offset : stderr_offset + limit],
            "stdout_offset": stdout_offset,
            "stderr_offset": stderr_offset,
            "stdout_length": len(stdout),
            "stderr_length": len(stderr),
        }

    async def aclose(self) -> None:
        self._closed = True
        active = [p for p in self._managed_processes.values() if self._managed_process_alive(p)]
        for managed in active:
            self._begin_cleanup(managed, "Runtime 关闭时清理", ProcessStatus.CANCELLED)
        try:
            await asyncio.gather(
                *(asyncio.shield(p.cleanup_task) for p in active if p.cleanup_task is not None)
            )
        finally:
            for managed in self._managed_processes.values():
                managed.process.close()
                if managed.timeout_task is not None:
                    managed.timeout_task.cancel()
            if self._monitor is not None:
                self._monitor.cancel()
                await asyncio.gather(self._monitor, return_exceptions=True)
        remaining = [p.process_id for p in active if p.status == ProcessStatus.CLEANUP_FAILED]
        self._managed_processes = {
            key: value for key, value in self._managed_processes.items() if key in remaining
        }
        if remaining:
            raise RuntimeError(f"process_cleanup_incomplete: {', '.join(remaining)}")

    def _managed_process(self, process_id: str) -> _ManagedProcess:
        try:
            return self._managed_processes[process_id]
        except KeyError as exc:
            raise ValueError(f"未知受管进程: {process_id}") from exc

    @staticmethod
    def _managed_snapshot(
        managed: _ManagedProcess,
        *,
        consume_output: bool,
        include_output: bool = True,
    ) -> ProcessSnapshot:
        process = managed.process
        stdout, stderr = "".join(process.stdout), "".join(process.stderr)
        out = stdout[managed.stdout_cursor :] if include_output else ""
        err = stderr[managed.stderr_cursor :] if include_output else ""
        if consume_output:
            managed.stdout_cursor, managed.stderr_cursor = len(stdout), len(stderr)
        now = monotonic()
        return ProcessSnapshot(
            session_id=managed.spec.session_id,
            run_id=managed.spec.run_id,
            process_id=managed.process_id,
            status=managed.status,
            argv=managed.spec.argv,
            cwd=managed.spec.cwd,
            elapsed_seconds=max(0, (managed.finished_at or now) - managed.started_at),
            hard_timeout_seconds=managed.spec.timeout_seconds,
            interactive=managed.spec.interactive,
            returncode=process.returncode,
            stdout=out,
            stderr=err,
            stdout_sha256=hashlib.sha256(stdout.encode()).hexdigest(),
            stderr_sha256=hashlib.sha256(stderr.encode()).hexdigest(),
            truncated=process.truncated,
            last_output_seconds_ago=max(0, now - (process.last_output_at or managed.started_at)),
            termination_reason=managed.termination_reason,
            cleanup_status=managed.cleanup_status,
            containment="session_scan" if os.name == "posix" else "root_only",
            output_complete=process.pipes_closed.is_set() and not process.cut_off,
            remaining_processes=managed.scope.remaining(),
            cleanup_elapsed_seconds=managed.cleanup_elapsed,
        )

    async def _execute(self, spec: ProcessSpec) -> AsyncIterator[ProcessEvent]:
        process_id = await self.start_process(spec)
        managed = self._managed_process(process_id)
        completed = False
        try:
            while True:
                managed.process.changed.clear()
                snapshot = self._managed_snapshot(managed, consume_output=True)
                for kind, data in (
                    (ProcessEventKind.STDOUT, snapshot.stdout),
                    (ProcessEventKind.STDERR, snapshot.stderr),
                ):
                    if data:
                        yield ProcessEvent(kind=kind, data=data, truncated=snapshot.truncated)
                if managed.finished.is_set():
                    if snapshot.status == ProcessStatus.TIMED_OUT:
                        raise TimeoutError(
                            f"命令执行超过 {spec.timeout_seconds:g} 秒: {spec.argv[0]}"
                        )
                    if snapshot.status == ProcessStatus.CLEANUP_FAILED:
                        raise RuntimeError("process_cleanup_incomplete: 命令清理未完成")
                    completed = True
                    yield ProcessEvent(
                        kind=ProcessEventKind.COMPLETED,
                        returncode=snapshot.returncode,
                        truncated=snapshot.truncated,
                    )
                    return
                await managed.process.changed.wait()
        finally:
            if not completed and not managed.finished.is_set():
                await self.terminate_process(process_id, reason="命令流关闭")

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
