import signal
import sys

import pytest

from bot.execution import LocalExecutionTarget, ProcessSpec
from bot.execution.process_scope import Inventory, ProcessIdentity, ProcessScope


def scope_with_baseline(baseline):
    scope = ProcessScope(987654321, strict=True, baseline=baseline)
    scope.sid = None
    return scope


def test_new_orphan_is_unknown_without_adopting_or_signalling_it(monkeypatch):
    scope = scope_with_baseline(Inventory({}, set()))
    orphan = ProcessIdentity(12345, 10, 1, 12345)
    scope.update(Inventory({orphan.pid: orphan}, set()))
    assert scope.unknown and scope.unattributed == {orphan.pid: orphan}
    assert not scope.known and not scope.live and scope.remaining() == []

    def forbid(*args, **kwargs):
        raise AssertionError("Unattributed processes must never be signalled")

    monkeypatch.setattr("bot.execution.process_scope.psutil.Process", forbid)
    scope.send(signal.SIGTERM)


def test_preexisting_orphan_does_not_block_a_new_scope():
    orphan = ProcessIdentity(12345, 10, 1, 12345)
    inventory = Inventory({orphan.pid: orphan}, set())
    scope = scope_with_baseline(inventory)
    scope.update(inventory)
    assert not scope.unknown and not scope.unattributed


def test_reused_preexisting_pid_cannot_disappear_into_the_baseline():
    old = ProcessIdentity(12345, 10, 1, 12345)
    new = ProcessIdentity(old.pid, 11, 1, 12345)
    scope = scope_with_baseline(Inventory({old.pid: old}, set()))
    scope.update(Inventory({new.pid: new}, set()))
    assert scope.unknown and scope.unattributed[new.pid] == new
    assert new.pid not in scope.known


def test_previously_owned_child_stays_owned_after_becoming_orphan():
    scope = scope_with_baseline(Inventory({}, set()))
    child = ProcessIdentity(12345, 10, 987654321, 987654321)
    scope.known[child.pid] = child
    detached = ProcessIdentity(child.pid, child.start_time, 1, child.pid)
    scope.update(Inventory({detached.pid: detached}, set()))
    assert scope.live[detached.pid] == detached
    assert not scope.unknown and not scope.unattributed


def test_uncertain_orphan_exit_allows_a_fresh_empty_observation():
    scope = scope_with_baseline(Inventory({}, set()))
    orphan = ProcessIdentity(12345, 10, 1, 12345)
    scope.update(Inventory({orphan.pid: orphan}, set()))
    assert scope.unknown
    scope.update(Inventory({}, set()))
    assert not scope.unknown and not scope.unattributed


def test_new_unreadable_process_prevents_claiming_empty_scope():
    scope = scope_with_baseline(Inventory({}, {10}))
    scope.update(Inventory({}, {10, 11}))
    assert scope.unknown


def test_strict_recovery_without_baseline_cannot_report_success():
    scope = ProcessScope(987654321, strict=True)
    scope.restore_guard(None)
    scope.update(Inventory({}, set()))
    assert scope.unknown


def test_guard_survives_json_style_pid_keys_on_recovery():
    orphan = ProcessIdentity(12345, 10, 1, 12345)
    scope = scope_with_baseline(Inventory({orphan.pid: orphan}, {99}))
    record = scope.guard_record()
    record["baseline_births"] = {str(pid): born for pid, born in record["baseline_births"].items()}
    restored = ProcessScope(987654321, strict=True)
    restored.restore_guard(record)
    restored.update(Inventory({orphan.pid: orphan}, {99}))
    assert not restored.unknown


@pytest.mark.asyncio
async def test_missing_prelaunch_inventory_never_starts_target(tmp_path, monkeypatch):
    target = LocalExecutionTarget(strict_process_scope=True)

    async def unavailable():
        raise PermissionError("process inventory denied")

    monkeypatch.setattr(target._inventory, "read", unavailable)
    marker = tmp_path / "executed"
    with pytest.raises(PermissionError, match="inventory denied"):
        await target.start_process(
            ProcessSpec(
                argv=[sys.executable, "-c", "import pathlib; pathlib.Path('executed').touch()"],
                cwd=tmp_path,
            )
        )
    assert not marker.exists() and not target.has_live_processes
    await target.aclose()
