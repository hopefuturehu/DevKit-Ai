"""Best-effort process ownership. Never identify a process by name alone."""

from __future__ import annotations

import asyncio
import errno
import os
import signal
from dataclasses import asdict, dataclass, field
from threading import Thread
from time import monotonic

import psutil


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_time: float
    ppid: int
    sid: int | None


@dataclass
class Inventory:
    processes: dict[int, ProcessIdentity]
    inaccessible: set[int]
    started_at: float = field(default_factory=monotonic)


def collect_processes() -> Inventory:
    started = monotonic()
    result: dict[int, ProcessIdentity] = {}
    inaccessible: set[int] = set()
    for pid in psutil.pids():
        try:
            process = psutil.Process(pid)
            with process.oneshot():
                if process.status() == psutil.STATUS_ZOMBIE:
                    continue
                sid = os.getsid(process.pid) if os.name == "posix" else None
                result[process.pid] = ProcessIdentity(
                    process.pid, process.create_time(), process.ppid(), sid
                )
        except (psutil.NoSuchProcess, ProcessLookupError):
            continue
        except (psutil.AccessDenied, PermissionError):
            inaccessible.add(pid)
    return Inventory(result, inaccessible, started)


class ProcessInventory:
    """One bounded consumer of one in-flight collector per execution target."""

    def __init__(self) -> None:
        self.pending: asyncio.Future[Inventory] | None = None

    async def read(self) -> Inventory:
        if self.pending is None:
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            self.pending = future

            def settle(result, error):
                if not future.done():
                    if error is not None:
                        future.set_exception(error)
                    else:
                        future.set_result(result)

            def collect():
                try:
                    result, error = collect_processes(), None
                except Exception as exc:
                    result, error = None, exc
                try:
                    loop.call_soon_threadsafe(settle, result, error)
                except RuntimeError:
                    pass  # Loop was closed while an OS inventory call was blocked.

            # A stuck OS read must not block asyncio.run's default-executor shutdown.
            Thread(target=collect, daemon=True, name="bot-process-inventory").start()
            future.add_done_callback(
                lambda done: done.exception() if not done.cancelled() else None
            )
        task = self.pending
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self.pending is task:
                self.pending = None


class ProcessScope:
    def __init__(self, root_pid: int) -> None:
        self.root_pid = root_pid
        self.sid = root_pid if os.name == "posix" else None
        self.known: dict[int, ProcessIdentity] = {}
        try:
            root = psutil.Process(root_pid)
            self.known[root_pid] = ProcessIdentity(
                root_pid, root.create_time(), root.ppid(), self.sid
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        self.live: dict[int, ProcessIdentity] = {}
        self.unknown = False

    def update(self, snapshot: Inventory) -> None:
        # Never adopt an unrelated session if its numeric SID was reused after restart.
        old_root, new_root = self.known.get(self.root_pid), snapshot.processes.get(self.root_pid)
        if old_root and new_root and old_root.start_time != new_root.start_time:
            self.sid = None
        # Once seen, a PID belongs only while its creation identity still matches.
        live = {
            pid: identity
            for pid, identity in snapshot.processes.items()
            if (self.sid is not None and identity.sid == self.sid)
            or (pid in self.known and identity.start_time == self.known[pid].start_time)
        }
        changed = True
        while changed:
            changed = False
            for pid, identity in snapshot.processes.items():
                if pid not in live and identity.ppid in live:
                    live[pid] = identity
                    changed = True
        self.unknown = bool(snapshot.inaccessible & (self.known.keys() | {self.root_pid}))
        self.live = live
        self.known.update(live)

    def remaining(self) -> list[dict]:
        return [asdict(identity) for identity in self.live.values()]

    def send(self, sig: signal.Signals, *, deadline: float | None = None) -> None:
        # Signal individual identities, so an unrelated/reused PGID is never targeted.
        for identity in list(self.live.values()):
            if deadline is not None and monotonic() >= deadline:
                raise TimeoutError("process signal deadline reached")
            fd = None
            try:
                if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
                    try:
                        fd = os.pidfd_open(identity.pid)
                    except OSError as exc:
                        if exc.errno not in {errno.ENOSYS, errno.EINVAL, errno.ENODEV}:
                            raise
                process = psutil.Process(identity.pid)
                if process.create_time() != identity.start_time:
                    continue
                if fd is not None:
                    signal.pidfd_send_signal(fd, sig)
                else:
                    process.send_signal(sig)
            except (psutil.NoSuchProcess, ProcessLookupError):
                continue
            except (psutil.AccessDenied, PermissionError):
                self.unknown = True
            finally:
                if fd is not None:
                    os.close(fd)
