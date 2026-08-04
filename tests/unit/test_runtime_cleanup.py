import asyncio
import signal
from pathlib import Path

import pytest
import typer

from bot.cli.app import _with_runtime_shutdown
from bot.cli.runtime import Runtime


class _FailingSubagents:
    has_live_tasks = False

    async def shutdown(self) -> None:
        raise RuntimeError("subagent shutdown failed")


class _TrackingTarget:
    has_live_processes = False

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _TrackingStore:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _SuccessfulRuntime:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_runtime_cleans_processes_when_subagent_shutdown_fails(tmp_path: Path) -> None:
    target = _TrackingTarget()
    store = _TrackingStore()
    runtime = Runtime(
        config=None,
        workspace=tmp_path,
        catalog=None,
        skills=None,
        tools=None,
        target=target,
        context=None,
        store=store,
        compactor=None,
        runner=None,
        subagents=_FailingSubagents(),
    )

    with pytest.raises(RuntimeError, match="subagent shutdown failed"):
        await runtime.aclose()

    assert target.closed
    assert not store.closed


@pytest.mark.asyncio
async def test_sigterm_cancels_work_and_waits_for_runtime_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    handlers: dict[int, tuple[object, tuple[object, ...]]] = {}
    removed: list[int] = []

    def add_handler(signum: int, callback, *args: object) -> None:
        handlers[signum] = (callback, args)

    monkeypatch.setattr(loop, "add_signal_handler", add_handler)
    monkeypatch.setattr(loop, "remove_signal_handler", lambda signum: removed.append(signum))
    monkeypatch.setattr(signal, "signal", lambda signum, previous: previous)
    runtime = _SuccessfulRuntime()

    async def workload() -> None:
        callback, args = handlers[signal.SIGTERM]
        callback(*args)
        await asyncio.sleep(1)

    with pytest.raises(typer.Exit) as raised:
        await _with_runtime_shutdown(runtime, workload())

    assert raised.value.exit_code == 128 + signal.SIGTERM
    assert runtime.closed
    assert signal.SIGTERM in removed
