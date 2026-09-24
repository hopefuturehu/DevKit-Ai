"""Bounded parent/launcher pipe, never a parser for command stdout or stderr.

Only the verified outer launcher receives the write descriptor. Its children
inherit stdio only. A dispatch acknowledgement is not proof of target exec:
after dispatch, executed remains unknown unless a separate proof exists.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from uuid import uuid4

from bot.sandbox.models import SandboxError, SandboxErrorCode
from bot.sandbox.worker import decode_json_line

MAX_STATUS_BYTES = 4096
STARTUP_TIMEOUT = 10.0
_PHASES = frozenset({"initialize", "policy", "wrap", "spawn"})


@dataclass(frozen=True)
class LaunchAcknowledgement:
    state: str
    stage: str

    @property
    def executed(self) -> bool | None:
        return False if self.state == "failed" else None


def decode_status(raw: bytes, invocation: str) -> LaunchAcknowledgement:
    if len(raw) > MAX_STATUS_BYTES:
        raise ValueError("启动状态超过大小限制")
    lines = raw.splitlines(keepends=True)
    if len(lines) != 2:
        raise ValueError("启动状态必须包含就绪前标记和一个终态")
    messages = [decode_json_line(line, limit=MAX_STATUS_BYTES) for line in lines]
    for message in messages:
        if (
            message.keys() != {"v", "id", "state", "stage"}
            or type(message["v"]) is not int
            or message["v"] != 1
            or message["id"] != invocation
            or not isinstance(message["state"], str)
            or not isinstance(message["stage"], str)
        ):
            raise ValueError("启动状态身份或字段无效")
    if messages[0] != {"v": 1, "id": invocation, "state": "initializing", "stage": "initialize"}:
        raise ValueError("缺少可信启动器初始化标记")
    final = messages[1]
    if final["state"] == "dispatched":
        valid = final["stage"] == "spawn"
    else:
        valid = final["state"] in {"failed", "unknown"} and final["stage"] in _PHASES
    if not valid:
        raise ValueError("启动状态转换无效")
    return LaunchAcknowledgement(final["state"], final["stage"])


class LaunchStatusPipe:
    def __init__(self):
        self.invocation = uuid4().hex
        self.reader, self.writer = os.pipe()
        try:
            os.set_blocking(self.reader, False)
        except BaseException:
            os.close(self.reader)
            os.close(self.writer)
            raise
        self.buffer = bytearray()
        self.loop = None
        self.ready = None
        self.result: LaunchAcknowledgement | None = None
        self.error: str | None = None
        self.eof = False

    @property
    def arguments(self) -> tuple[str, ...]:
        if self.writer is None:
            raise ValueError("启动状态写描述符已关闭")
        return ("--status-fd", str(self.writer), "--status-id", self.invocation)

    def after_spawn(self):
        if self.writer is not None:
            writer, self.writer = self.writer, None
            os.close(writer)

    def _finish(self):
        if self.reader is not None:
            reader, self.reader = self.reader, None
            if self.loop is not None and not self.loop.is_closed():
                self.loop.remove_reader(reader)
            os.close(reader)
        if self.ready is not None and not self.ready.done():
            self.ready.set_result(None)

    def _read(self):
        if self.reader is None:
            return
        try:
            while True:
                chunk = os.read(self.reader, MAX_STATUS_BYTES + 1 - len(self.buffer))
                if not chunk:
                    self.eof = True
                    self.result = decode_status(bytes(self.buffer), self.invocation)
                    self._finish()
                    return
                self.buffer.extend(chunk)
                if len(self.buffer) > MAX_STATUS_BYTES:
                    raise ValueError("启动状态超过大小限制")
        except BlockingIOError:
            return
        except (OSError, ValueError) as exc:
            self.error = str(exc)[:500]
            self._finish()

    async def wait(self) -> LaunchAcknowledgement:
        if self.loop is None:
            self.loop = asyncio.get_running_loop()
            self.ready = self.loop.create_future()
            if self.reader is not None:
                self.loop.add_reader(self.reader, self._read)
                self._read()
            else:
                self.ready.set_result(None)
        try:
            async with asyncio.timeout(STARTUP_TIMEOUT):
                await asyncio.shield(self.ready)
        except TimeoutError as exc:
            raise SandboxError(
                SandboxErrorCode.LAUNCH_FAILED,
                "等待可信启动状态超时",
                stage="launcher_status",
                executed=None,
            ) from exc
        if self.error or self.result is None:
            raise SandboxError(
                SandboxErrorCode.LAUNCH_FAILED,
                self.error or "可信启动状态未完成",
                stage="launcher_status",
                executed=None,
            )
        return self.result

    def close(self):
        self.after_spawn()
        # A very short command can finish before the event-loop callback runs.
        # Preserve any complete status instead of discarding it during release.
        self._read()
        if self.reader is not None:
            self.error = "可信启动状态在 EOF 前关闭"
            self._finish()
