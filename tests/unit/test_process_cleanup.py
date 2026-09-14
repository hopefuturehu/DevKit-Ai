import asyncio
import os
import signal
import sys
from time import monotonic

import psutil
import pytest

from bot.config.models import ProcessCleanupConfig
from bot.execution import LocalExecutionTarget, ProcessSpec, ProcessStatus
from bot.execution.process_scope import Inventory, ProcessIdentity, ProcessScope
from bot.execution.process_transport import ProcessTransport


def cleanup_config():
    return ProcessCleanupConfig(
        total_timeout_seconds=1,
        term_grace_seconds=0.2,
        kill_grace_seconds=0.4,
        drain_grace_seconds=0.4,
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX sessions")
@pytest.mark.asyncio
@pytest.mark.parametrize("root_exits", [False, True])
async def test_cleanup_finds_child_that_changes_group(tmp_path, root_exits):
    target = LocalExecutionTarget(cleanup=cleanup_config())
    script = """import os, signal, time
pid = os.fork()
if pid == 0:
    os.setpgrp()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    open('child.pid', 'w').write(str(os.getpid()))
    time.sleep(20)
else:
    ROOT
""".replace("ROOT", "pass" if root_exits else "time.sleep(20)")
    try:
        proc = await target.start_process(
            ProcessSpec(argv=[sys.executable, "-c", script], cwd=tmp_path)
        )
        async with asyncio.timeout(3):
            while not (tmp_path / "child.pid").exists():  # noqa: ASYNC110 - external process handshake
                await asyncio.sleep(0.01)
        child = int((tmp_path / "child.pid").read_text())
        await asyncio.sleep(0.05)
        started = monotonic()
        first, second = await asyncio.gather(
            target.terminate_process(proc), target.terminate_process(proc)
        )
        assert monotonic() - started < 1.5
        assert first.status == second.status == ProcessStatus.CANCELLED
        assert first.output_complete and first.cleanup_status == "observed_empty"
        assert first.cleanup_changed != second.cleanup_changed
        assert (
            not psutil.pid_exists(child) or psutil.Process(child).status() == psutil.STATUS_ZOMBIE
        )
        assert not (await target.terminate_process(proc)).cleanup_changed
    finally:
        await target.aclose()


@pytest.mark.asyncio
async def test_failed_cleanup_retains_quota_and_can_be_retried(tmp_path, monkeypatch):
    target = LocalExecutionTarget(max_managed_processes=1, cleanup=cleanup_config())
    records = []

    async def record(spec, pid, payload):
        records.append(payload)

    target.cleanup_observer = record
    proc = await target.start_process(
        ProcessSpec(argv=[sys.executable, "-c", "import time; time.sleep(20)"], cwd=tmp_path)
    )
    scope = target._managed_processes[proc].scope
    original = scope.send
    monkeypatch.setattr(scope, "send", lambda sig, **kwargs: None)
    try:
        started = monotonic()
        result = await target.terminate_process(proc)
        assert monotonic() - started < 1.5
        assert result.status == ProcessStatus.CLEANUP_FAILED
        assert not result.output_complete and target.has_live_processes
        assert result.remaining_processes
        with pytest.raises(RuntimeError, match="上限"):
            await target.start_process(ProcessSpec(argv=["true"], cwd=tmp_path))
        restored = LocalExecutionTarget(cleanup=cleanup_config())
        restored.restore_cleanups(records)
        assert restored.has_live_processes
        await restored.aclose()
    finally:
        monkeypatch.setattr(scope, "send", original)
        await target.aclose()


def test_process_scope_rejects_reused_identity(monkeypatch):
    scope = ProcessScope(os.getpid())
    identity = ProcessIdentity(987654, 10, 1, None)
    scope.known = {identity.pid: identity}
    scope.update(Inventory({identity.pid: ProcessIdentity(identity.pid, 11, 1, None)}, set()))
    assert not scope.live


def test_transport_decodes_split_utf8_and_caps_retention():
    protocol = ProcessTransport(6)
    raw = "中文尾".encode()
    for byte in raw:
        protocol.pipe_data_received(1, bytes([byte]))
    protocol.pipe_connection_lost(1, None)
    protocol.pipe_connection_lost(2, None)
    assert "".join(protocol.stdout) == "中文"
    assert protocol.truncated and protocol.pipes_closed.is_set()


@pytest.mark.asyncio
async def test_unavailable_inventory_returns_unknown_without_spawning_collectors(
    tmp_path, monkeypatch
):
    import threading

    import bot.execution.process_scope as scopes

    started, release = threading.Event(), threading.Event()
    calls = []
    original = scopes.collect_processes

    def blocked():
        calls.append(1)
        started.set()
        release.wait(4)
        return original()

    monkeypatch.setattr(scopes, "collect_processes", blocked)
    target = LocalExecutionTarget(cleanup=cleanup_config())
    proc = await target.start_process(
        ProcessSpec(argv=[sys.executable, "-c", "import time; time.sleep(20)"], cwd=tmp_path)
    )
    try:
        begin = monotonic()
        result = await target.terminate_process(proc)
        assert monotonic() - begin < 1.5
        assert result.status == ProcessStatus.CLEANUP_FAILED
        assert result.cleanup_status == "unknown" and not result.output_complete
        assert len(calls) == 1 and started.is_set()
    finally:
        release.set()
        await asyncio.sleep(0.05)
        await target.aclose()


@pytest.mark.asyncio
async def test_cleanup_never_targets_another_run(tmp_path):
    target = LocalExecutionTarget(cleanup=cleanup_config())
    spec = ProcessSpec(argv=[sys.executable, "-c", "import time; time.sleep(20)"], cwd=tmp_path)
    first = await target.start_process(spec.model_copy(update={"run_id": "a"}))
    second = await target.start_process(spec.model_copy(update={"run_id": "b"}))
    try:
        await target.terminate_process(first)
        snapshot = await target.poll_process(second)
        assert snapshot.status == ProcessStatus.RUNNING and snapshot.returncode is None
    finally:
        await target.aclose()


def test_reused_session_leader_is_not_adopted():
    scope = ProcessScope(os.getpid())
    old = scope.known[os.getpid()]
    scope.sid = old.pid
    scope.update(
        Inventory({old.pid: ProcessIdentity(old.pid, old.start_time + 1, old.ppid, old.pid)}, set())
    )
    assert not scope.live and scope.sid is None


def test_signal_rechecks_creation_time_before_killing(monkeypatch):
    sent = []
    monkeypatch.setattr(psutil.Process, "send_signal", lambda *args: sent.append(args))
    if hasattr(signal, "pidfd_send_signal"):
        monkeypatch.setattr(signal, "pidfd_send_signal", lambda *args: sent.append(args))
    scope = ProcessScope(os.getpid())
    old = scope.known[os.getpid()]
    scope.live = {old.pid: ProcessIdentity(old.pid, old.start_time - 100, old.ppid, old.sid)}
    scope.send(signal.SIGTERM)
    assert sent == []


def test_transport_pipe_errors_do_not_count_as_complete_output():
    protocol = ProcessTransport(100)
    protocol.pipe_connection_lost(1, OSError("reader failed"))
    protocol.pipe_connection_lost(2, None)
    assert protocol.pipes_closed.is_set() and protocol.cut_off


def test_signal_deadline_is_checked_before_each_identity():
    scope = ProcessScope(os.getpid())
    scope.live = {-1: ProcessIdentity(-1, 1, 0, None)}
    with pytest.raises(TimeoutError, match="signal deadline"):
        scope.send(signal.SIGTERM, deadline=monotonic() - 1)
