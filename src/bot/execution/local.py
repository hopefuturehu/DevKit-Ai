from __future__ import annotations

import asyncio
import os
import platform
import shutil
import signal
from collections.abc import AsyncIterator
from pathlib import Path

from bot.execution.base import (
    EnvironmentCapabilities,
    ExecutionTarget,
    ProcessEvent,
    ProcessEventKind,
    ProcessSpec,
)


class LocalExecutionTarget(ExecutionTarget):
    async def probe(self, executables: list[str] | None = None) -> EnvironmentCapabilities:
        found = {name: shutil.which(name) for name in executables or []}
        return EnvironmentCapabilities(
            operating_system=platform.system().lower(),
            architecture=platform.machine().lower(),
            executables=found,
        )

    def execute(self, spec: ProcessSpec) -> AsyncIterator[ProcessEvent]:
        return self._execute(spec)

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
