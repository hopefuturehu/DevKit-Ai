import asyncio
import sys

import psutil
import pytest

from bot.execution.base import ProcessSpec
from bot.execution.local import LocalExecutionTarget, PreparedLaunch


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_start", [False, True])
async def test_close_waits_for_admitted_spawn_despite_repeated_cancellation(
    tmp_path, monkeypatch, cancel_start
):
    target = LocalExecutionTarget()
    loop = asyncio.get_running_loop()
    original_spawn = loop.subprocess_exec
    spawned, release = asyncio.Event(), asyncio.Event()
    child = None

    async def delayed_spawn(*args, **kwargs):
        nonlocal child
        transport, protocol = await original_spawn(*args, **kwargs)
        child = psutil.Process(transport.get_pid())
        child.create_time()  # Cache identity before PID reuse is possible.
        spawned.set()
        await release.wait()
        return transport, protocol

    monkeypatch.setattr(loop, "subprocess_exec", delayed_spawn)
    starting = asyncio.create_task(
        target.start_process(
            ProcessSpec(
                argv=[sys.executable, "-S", "-c", "import time; time.sleep(30)"], cwd=tmp_path
            )
        )
    )
    closing = another_close = None
    try:
        await asyncio.wait_for(spawned.wait(), 5)
        assert child.is_running() and not target._managed_processes
        closing = asyncio.create_task(target.aclose())
        await asyncio.sleep(0)
        another_close = asyncio.create_task(target.aclose())
        for _ in range(2):
            closing.cancel()
            if cancel_start:
                starting.cancel()
            await asyncio.sleep(0)
        assert not closing.done() and not another_close.done()
        with pytest.raises(RuntimeError, match="已经关闭"):
            # This admission check will acquire the lock after the admitted
            # spawn, so release it first; _closed is already authoritative.
            release.set()
            await target.start_process(ProcessSpec(argv=[sys.executable], cwd=tmp_path))
        if cancel_start:
            with pytest.raises(asyncio.CancelledError):
                await starting
        else:
            await starting
        with pytest.raises(asyncio.CancelledError):
            await closing
        await another_close
        assert not target.has_live_processes and not target._managed_processes
        assert not child.is_running()
    finally:
        release.set()
        await asyncio.gather(
            *(t for t in (starting, closing, another_close) if t is not None),
            return_exceptions=True,
        )
        await target.aclose()


@pytest.mark.asyncio
async def test_backend_close_failure_still_reclaims_process_and_can_retry(tmp_path):
    class FailingBackend(LocalExecutionTarget):
        fail = True
        released = False

        async def _before_close(self):
            if self.fail:
                raise RuntimeError("synthetic backend cleanup failure")

        async def _after_close(self):
            self.released = True

    target = FailingBackend()
    try:
        process_id = await target.start_process(
            ProcessSpec(
                argv=[sys.executable, "-S", "-c", "import time; time.sleep(30)"], cwd=tmp_path
            )
        )
        child = psutil.Process(target._managed_process(process_id).process.pid)
        with pytest.raises(RuntimeError, match="synthetic backend"):
            await target.aclose()
        assert not child.is_running() and not target.has_live_processes
        assert not target.released
        target.fail = False
        await target.aclose()
        assert target.released
    finally:
        target.fail = False
        await target.aclose()


@pytest.mark.asyncio
async def test_cancelled_close_reports_cleanup_failure_and_retains_quota(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockedResource(LocalExecutionTarget):
        fail = True

        def _prepare_launch(self, spec):
            def remove():
                if self.fail:
                    raise OSError("synthetic retained resource")

            return PreparedLaunch(tuple(spec.argv), self._safe_environment(), release=remove)

        async def _before_close(self):
            entered.set()
            await release.wait()

    target = BlockedResource()
    closing = None
    try:
        await target.start_process(
            ProcessSpec(
                argv=[sys.executable, "-S", "-c", "import time; time.sleep(30)"], cwd=tmp_path
            )
        )
        closing = asyncio.create_task(target.aclose())
        await asyncio.wait_for(entered.wait(), 5)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        with pytest.raises(RuntimeError, match="process_cleanup_incomplete"):
            await closing
        assert target.has_live_processes
        target.fail = False
        await target.aclose()
        assert not target.has_live_processes
    finally:
        release.set()
        target.fail = False
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        await target.aclose()
