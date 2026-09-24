import asyncio

import pytest

from bot.sandbox.models import SandboxError, SandboxErrorCode
from bot.sandbox.recovery import RecoveryGate


class Recovery:
    def __init__(self, *, fail=False):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.cancelled = False
        self.fail = fail

    async def run(self):
        self.calls += 1
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.fail:
            raise SandboxError(SandboxErrorCode.CLEANUP_FAILED, "unknown", stage="startup_recovery")


@pytest.mark.asyncio
async def test_cancelled_waiter_waits_for_shared_recovery_without_cancelling_other_runs():
    recovery = Recovery()
    gate = RecoveryGate(recovery.run)
    first = asyncio.create_task(gate.ensure())
    second = asyncio.create_task(gate.ensure())
    await recovery.started.wait()
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.sleep(0)
    assert not first.done() and not second.done() and gate.live
    recovery.release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    await gate.ensure()
    assert recovery.calls == 1 and not recovery.cancelled and not gate.live
    await gate.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_close_joins_recovery_and_cannot_admit_waiting_calls(fail):
    recovery = Recovery(fail=fail)
    gate = RecoveryGate(recovery.run)
    call = asyncio.create_task(gate.ensure())
    await recovery.started.wait()
    closing = asyncio.create_task(gate.aclose())
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert gate.closed and not call.done() and not closing.done()
    recovery.release.set()
    with pytest.raises(SandboxError):
        await call
    with pytest.raises(asyncio.CancelledError):
        await closing
    await gate.aclose()
    assert recovery.calls == 1 and not recovery.cancelled and not gate.live
    with pytest.raises(SandboxError):
        await gate.ensure()


@pytest.mark.asyncio
async def test_cleanup_failure_precedes_cancellation_and_is_not_automatically_retried():
    recovery = Recovery(fail=True)
    gate = RecoveryGate(recovery.run)
    call = asyncio.create_task(gate.ensure())
    await recovery.started.wait()
    call.cancel()
    recovery.release.set()
    for pending in (call, gate.ensure()):
        with pytest.raises(SandboxError) as error:
            await pending
        assert error.value.code == SandboxErrorCode.CLEANUP_FAILED
    assert recovery.calls == 1
    # Backend shutdown remains responsible for verifying its final resources.
    await gate.aclose()


@pytest.mark.asyncio
async def test_unused_target_close_still_discovers_crash_leftovers():
    recovery = Recovery()
    gate = RecoveryGate(recovery.run)
    closing = asyncio.create_task(gate.aclose())
    await recovery.started.wait()
    assert not closing.done()
    recovery.release.set()
    await closing
    await gate.aclose()
    assert recovery.calls == 1
