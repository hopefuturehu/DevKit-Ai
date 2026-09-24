import asyncio

import pytest

from bot.sandbox.models import SandboxError, SandboxErrorCode
from bot.sandbox.preflight import NativePreflight, StartupGate
from bot.sandbox.resources import OwnedDirectory


class Probe:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.has_live_processes = False
        self.runs = 0
        self.closes = 0
        self.result = object()

    async def run(self):
        self.runs += 1
        self.has_live_processes = True
        self.started.set()
        try:
            await self.release.wait()
            return self.result
        finally:
            self.cleanup_started.set()
            await self.cleanup_release.wait()
            self.has_live_processes = False

    async def aclose(self):
        self.closes += 1
        assert not self.has_live_processes


@pytest.mark.asyncio
async def test_concurrent_callers_share_one_probe_and_cached_result():
    probe = Probe()
    gate = StartupGate(lambda: probe)
    calls = [asyncio.create_task(gate.ensure()) for _ in range(4)]
    await probe.started.wait()
    assert probe.runs == 1 and gate.live
    probe.release.set()
    probe.cleanup_release.set()
    assert all(result is probe.result for result in await asyncio.gather(*calls))
    assert await gate.ensure() is probe.result and probe.runs == 1
    assert not gate.live
    await gate.aclose()
    with pytest.raises(SandboxError):
        await gate.ensure()


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_cleanup_and_does_not_replay():
    probe = Probe()
    gate = StartupGate(lambda: probe)
    job = asyncio.create_task(gate.ensure())
    await probe.started.wait()
    job.cancel()
    await probe.cleanup_started.wait()
    job.cancel()
    assert gate.live and not job.done()
    probe.cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert not gate.live
    with pytest.raises(asyncio.CancelledError):
        await gate.ensure()
    assert probe.runs == 1
    await gate.aclose()


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_probe_before_returning():
    probe = Probe()
    gate = StartupGate(lambda: probe)
    job = asyncio.create_task(gate.ensure())
    await probe.started.wait()
    closing = asyncio.create_task(gate.aclose())
    await probe.cleanup_started.wait()
    assert not closing.done()
    probe.cleanup_release.set()
    await closing
    with pytest.raises(asyncio.CancelledError):
        await job
    assert probe.closes == 1 and not gate.live


@pytest.mark.asyncio
async def test_concurrent_cancellations_and_close_do_not_interrupt_shared_cleanup():
    probe = Probe()
    gate = StartupGate(lambda: probe)
    jobs = [asyncio.create_task(gate.ensure()) for _ in range(3)]
    await probe.started.wait()
    jobs[0].cancel()
    await probe.cleanup_started.wait()
    jobs[1].cancel()
    closing = asyncio.create_task(gate.aclose())
    await asyncio.sleep(0)
    assert gate.live and not closing.done()
    probe.cleanup_release.set()
    results = await asyncio.gather(*jobs, return_exceptions=True)
    await closing
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert not gate.live and probe.runs == 1 and probe.closes == 1


@pytest.mark.asyncio
async def test_cleanup_failure_takes_precedence_over_caller_cancellation():
    class Unresolved(Probe):
        async def run(self):
            try:
                await super().run()
            finally:
                self.has_live_processes = True
                raise SandboxError(SandboxErrorCode.CLEANUP_FAILED, "unknown ownership")

    probe = Unresolved()
    gate = StartupGate(lambda: probe)
    job = asyncio.create_task(gate.ensure())
    await probe.started.wait()
    job.cancel()
    await probe.cleanup_started.wait()
    probe.cleanup_release.set()
    with pytest.raises(SandboxError) as error:
        await job
    assert error.value.code == SandboxErrorCode.CLEANUP_FAILED
    assert error.value.executed is False
    assert error.value.stage == "isolation_preflight" and gate.live
    with pytest.raises(SandboxError):
        await gate.ensure()
    assert probe.runs == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["endpoints", "processes", "directory"])
async def test_failed_cleanup_retains_resources_and_still_attempts_process_cleanup(
    tmp_path, monkeypatch, failure
):
    probe = NativePreflight(None, tmp_path, None)
    probe.owned = OwnedDirectory.create(tmp_path, prefix="preflight-")
    probe.socket_directory = OwnedDirectory.create(tmp_path, prefix="socket-")
    closed = []

    class Manager:
        _managed_processes = {}

        async def aclose(self):
            closed.append(True)
            if failure == "processes":
                raise RuntimeError("unresolved process")

    probe.manager = Manager()

    async def close_endpoints():
        if failure == "endpoints":
            raise OSError("endpoint cleanup failed")

    monkeypatch.setattr(probe, "_close_endpoints", close_endpoints)
    if failure == "directory":

        def remove():
            raise OSError("resource cleanup failed")

        monkeypatch.setattr(probe.socket_directory, "remove", remove)
    with pytest.raises(SandboxError) as error:
        await probe.aclose()
    assert error.value.code == SandboxErrorCode.CLEANUP_FAILED
    assert error.value.executed is False
    assert closed == [True]
    assert probe.owned.path.is_dir() and probe.socket_directory.path.is_dir()


@pytest.mark.asyncio
@pytest.mark.parametrize("unresolved", [False, True])
async def test_failed_probe_is_not_retried_and_preserves_cleanup_failure(unresolved):
    class Failed:
        runs = 0
        has_live_processes = unresolved

        async def run(self):
            self.runs += 1
            if unresolved:
                raise SandboxError(SandboxErrorCode.CLEANUP_FAILED, "unresolved")
            raise ValueError("missing OS boundary")

        async def aclose(self):
            if unresolved:
                raise SandboxError(SandboxErrorCode.CLEANUP_FAILED, "unresolved")

    probe = Failed()
    gate = StartupGate(lambda: probe)
    for _ in range(2):
        with pytest.raises(SandboxError) as error:
            await gate.ensure()
        assert error.value.executed is False
        assert error.value.stage == "isolation_preflight"
        assert error.value.code == (
            SandboxErrorCode.CLEANUP_FAILED if unresolved else SandboxErrorCode.UNAVAILABLE
        )
    assert probe.runs == 1 and gate.live is unresolved
    if unresolved:
        with pytest.raises(SandboxError):
            await gate.aclose()
    else:
        await gate.aclose()
