"""Public asyncio subprocess protocol: root exit is independent of pipe EOF."""

from __future__ import annotations

import asyncio
import codecs
from time import monotonic


class ProcessTransport(asyncio.SubprocessProtocol):
    def __init__(self, output_limit: int) -> None:
        self.transport: asyncio.SubprocessTransport | None = None
        self.root_exited = asyncio.Event()
        self.root_exited_at: float | None = None
        self.pipes_closed = asyncio.Event()
        self.changed = asyncio.Event()
        self.writable = asyncio.Event()
        self.writable.set()
        self.stdout: list[str] = []
        self.stderr: list[str] = []
        self.output_limit = output_limit
        self.output_bytes = 0
        self.truncated = False
        self.cut_off = False
        self.last_output_at: float | None = None
        self._closed: set[int] = set()
        self._decoders = {fd: codecs.getincrementaldecoder("utf-8")("replace") for fd in (1, 2)}

    @property
    def pid(self) -> int:
        assert self.transport is not None
        return self.transport.get_pid()

    @property
    def returncode(self) -> int | None:
        return self.transport.get_returncode() if self.transport else None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def pipe_data_received(self, fd: int, data: bytes) -> None:
        if fd not in self._decoders or self.cut_off:
            return
        self.last_output_at = monotonic()
        selected = data[: max(0, self.output_limit - self.output_bytes)]
        self.output_bytes += len(selected)
        self.truncated |= len(selected) < len(data)
        (self.stdout if fd == 1 else self.stderr).append(self._decoders[fd].decode(selected))
        self.changed.set()

    def pipe_connection_lost(self, fd: int, exc: Exception | None) -> None:
        if exc is not None and fd in (1, 2):
            self.cut_off = True
        if fd in self._decoders and fd not in self._closed:
            (self.stdout if fd == 1 else self.stderr).append(
                self._decoders[fd].decode(b"", final=True)
            )
        self._closed.add(fd)
        if {1, 2} <= self._closed:
            self.pipes_closed.set()
        if fd == 0:
            self.writable.set()
        self.changed.set()

    def process_exited(self) -> None:
        self.root_exited_at = monotonic()
        self.root_exited.set()
        self.changed.set()

    def pause_writing(self) -> None:
        self.writable.clear()

    def resume_writing(self) -> None:
        self.writable.set()

    async def write(self, data: bytes, *, eof: bool = False) -> None:
        assert self.transport is not None
        pipe = self.transport.get_pipe_transport(0)
        if pipe is None or pipe.is_closing():
            raise ValueError("进程 stdin 已关闭")
        for start in range(0, len(data), 8192):
            await self.writable.wait()
            if pipe.is_closing():
                raise ValueError("进程 stdin 已关闭")
            pipe.write(data[start : start + 8192])
        await self.writable.wait()
        if eof:
            pipe.close()

    def close(self) -> None:
        self.cut_off |= not self.pipes_closed.is_set()
        if self.transport is not None:
            for fd in (0, 1, 2):
                pipe = self.transport.get_pipe_transport(fd)
                if pipe is not None:
                    pipe.close()
            self.transport.close()

    async def wait(self) -> int:
        await self.root_exited.wait()
        assert self.returncode is not None
        return self.returncode
