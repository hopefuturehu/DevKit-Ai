import json
import os
import subprocess
import sys
from dataclasses import replace
from uuid import uuid4

import pytest

from bot.sandbox.coordination import AdmissionCoordinator, AdmissionLease
from bot.sandbox.models import SandboxError, SandboxPolicy


@pytest.fixture
def layout(tmp_path):
    root = tmp_path.resolve()
    registry = root / "registry"
    work = root / "work"
    work.mkdir()
    coordinator = AdmissionCoordinator(registry)

    def policy(*, workspace=work, write=True, reads=(), excludes=()):
        private = root / "private" / uuid4().hex
        home, temp, cache = (private / name for name in ("home", "tmp", "cache"))
        for path in (home, temp, cache):
            path.mkdir(parents=True)
        return SandboxPolicy(
            workspace=workspace,
            read_roots=(workspace, *reads, home, temp, cache),
            write_roots=(*((workspace,) if write else ()), home, temp, cache),
            deny_read=excludes,
            deny_write=excludes,
            protected_paths=(),
            home_dir=home,
            temp_dir=temp,
            cache_dir=cache,
        )

    return coordinator, work, policy


@pytest.mark.parametrize("first_write,second_write", [(True, True), (True, False), (False, True)])
def test_separate_instances_refuse_read_write_conflicts_until_explicit_release(
    layout, first_write, second_write
):
    coordinator, _, policy = layout
    first = coordinator.acquire(policy(write=first_write))
    other = AdmissionCoordinator(coordinator.root)
    try:
        with pytest.raises(SandboxError, match="占用") as failure:
            other.acquire(policy(write=second_write))
        assert failure.value.stage == "admission_conflict"
        assert failure.value.executed is False
        first.verify()
    finally:
        first.release()
    second = other.acquire(policy(write=second_write))
    second.release()
    second.release()
    with pytest.raises(SandboxError):
        second.verify()


def test_readers_and_disjoint_worktrees_can_coexist(layout):
    coordinator, work, policy = layout
    reader = coordinator.acquire(policy(write=False))
    second = coordinator.acquire(policy(write=False))
    reader.release()
    second.release()
    trees = work / "worktrees"
    first_work, second_work = trees / "one", trees / "two"
    first_work.mkdir(parents=True)
    second_work.mkdir()
    parent = coordinator.acquire(policy(excludes=(trees,)))
    first = coordinator.acquire(policy(workspace=first_work))
    second = coordinator.acquire(policy(workspace=second_work))
    for lease in (parent, first, second):
        lease.verify()
        lease.release()


def test_ancestor_and_extra_read_grants_participate_in_conflict(layout):
    coordinator, work, policy = layout
    child = work / "child"
    child.mkdir()
    first = coordinator.acquire(policy(workspace=child))
    try:
        with pytest.raises(SandboxError, match="占用"):
            coordinator.acquire(policy(write=False))
        other = work.parent / "other"
        other.mkdir()
        with pytest.raises(SandboxError, match="占用"):
            coordinator.acquire(policy(workspace=other, write=False, reads=(child,)))
    finally:
        first.release()


def test_private_root_carveout_does_not_disappear_under_parent_read_deny(layout):
    coordinator, _, policy = layout
    first_policy = policy(write=False)
    first_policy = first_policy.model_copy(update={"deny_read": (first_policy.home_dir.parent,)})
    first = coordinator.acquire(first_policy)
    try:
        other = policy(write=False, reads=(first_policy.home_dir,))
        with pytest.raises(SandboxError, match="占用"):
            coordinator.acquire(other)
    finally:
        first.release()


@pytest.mark.parametrize("change", ["json", "permissions", "hardlink", "symlink", "owner"])
def test_invalid_lease_blocks_new_admission_and_cannot_be_released(layout, change):
    coordinator, work, policy = layout
    lease = coordinator.acquire(policy())
    path = coordinator.root / f"lease-{lease.binding['id']}.json"
    original = path.read_bytes()
    alias = work.parent / "alias"
    if change == "json":
        path.write_text("{invalid")
    elif change == "permissions":
        path.chmod(0o666)
    elif change == "hardlink":
        alias.hardlink_to(path)
    elif change == "symlink":
        path.rename(alias)
        path.symlink_to(alias)
    else:
        record = json.loads(original)
        record["owner"]["claim"] = "0" * 32
        path.write_text(json.dumps(record))
    try:
        with pytest.raises((SandboxError, OSError)):
            lease.release()
        assert not lease.released
        if change != "owner":
            with pytest.raises((SandboxError, OSError)):
                coordinator.acquire(policy(write=False))
    finally:
        if change == "symlink":
            path.unlink()
            alias.rename(path)
        elif change == "hardlink":
            alias.unlink()
        path.chmod(0o600)
        path.write_bytes(original)
        lease.release()


def test_registry_itself_cannot_be_granted_to_a_task(layout):
    coordinator, _, policy = layout
    with pytest.raises(SandboxError, match="注册表区域"):
        coordinator.acquire(policy(workspace=coordinator.root.parent))


def test_mutex_is_nonblocking_and_not_replaced_or_deleted(layout):
    coordinator, _, policy = layout
    with coordinator._locked():
        with pytest.raises(SandboxError, match="更新"):
            AdmissionCoordinator(coordinator.root).acquire(policy())
    identity = (coordinator.root / "mutex").stat().st_ino
    lease = coordinator.acquire(policy())
    lease.release()
    assert (coordinator.root / "mutex").stat().st_ino == identity


def test_registry_identity_replacement_is_refused(layout):
    coordinator, _, policy = layout
    old = coordinator.root.with_name("old-registry")
    coordinator.root.rename(old)
    coordinator.root.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="身份"):
        coordinator.acquire(policy())


def test_crashed_owner_keeps_occupancy_until_verified_recovery_and_release(layout):
    coordinator, _, policy = layout
    value = policy()
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            """
import json,sys
from pathlib import Path
from bot.sandbox.coordination import AdmissionCoordinator
from bot.sandbox.models import SandboxPolicy
c=AdmissionCoordinator(Path(sys.argv[1]))
lease=c.acquire(SandboxPolicy.model_validate_json(sys.argv[2]))
print(json.dumps(lease.record()),flush=True)
sys.stdin.read()
# Intentional host exit without lease release; no business process was spawned.
""",
            str(coordinator.root),
            value.model_dump_json(),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        metadata = json.loads(child.stdout.readline())
        with pytest.raises(SandboxError, match="仍存活"):
            coordinator.recover(metadata)
        with pytest.raises(SandboxError, match="占用"):
            coordinator.acquire(value)
        child.communicate("", timeout=10)
        assert child.returncode == 0
        with pytest.raises(SandboxError, match="占用"):
            AdmissionCoordinator(coordinator.root).acquire(value)
        with pytest.raises(SandboxError, match="不一致"):
            coordinator.recover({**metadata, "binding_sha256": "0" * 64})
        recovered = coordinator.recover(metadata)
        recovered.verify()
        with pytest.raises(SandboxError, match="仍存活"):
            coordinator.recover(metadata)
        recovered.release()
        current = coordinator.acquire(value)
        current.release()
    finally:
        if child.poll() is None:
            child.communicate("", timeout=10)


def test_forked_or_forged_handle_does_not_release_another_owner(layout):
    coordinator, _, policy = layout
    lease = coordinator.acquire(policy())
    fake = replace(lease, owner={**lease.owner, "pid": os.getpid() + 1})
    with pytest.raises(SandboxError, match="不属于"):
        fake.release()
    lease.verify()
    lease.release()


def test_recovery_journal_is_durable_and_detached_from_caller_objects(layout):
    coordinator, _, policy = layout
    lease = coordinator.acquire(policy())
    reference = lease.record()
    value = {"phase": "prepared", "nested": {"pid": None}}
    lease.save_recovery(value)
    value["nested"]["pid"] = 123
    snapshot = AdmissionCoordinator(coordinator.root).snapshots()[0]
    assert snapshot["recovery"] == {"phase": "prepared", "nested": {"pid": None}}
    snapshot["recovery"]["phase"] = "forged"
    lease.verify()
    lease.save_recovery({"phase": "registered", "nested": {"pid": 456}})
    assert lease.record() == reference
    assert coordinator.snapshots()[0]["recovery"]["nested"]["pid"] == 456
    lease.release()


def test_legacy_lease_remains_occupied_and_cannot_gain_a_recovery_journal(layout):
    coordinator, _, policy = layout
    current = coordinator.acquire(policy())
    path = coordinator.root / f"lease-{current.binding['id']}.json"
    record = json.loads(path.read_bytes())
    record["binding"]["v"] = 1
    del record["recovery"]
    path.write_text(json.dumps(record))
    legacy = AdmissionLease(coordinator, record["binding"], record["owner"])
    try:
        with pytest.raises(SandboxError, match="占用"):
            coordinator.acquire(policy())
        with pytest.raises(SandboxError, match="旧准入"):
            legacy.save_recovery({"phase": "registered"})
        assert coordinator.snapshots() == [record]
    finally:
        legacy.release()


def test_failed_durable_write_preserves_exact_published_identity_for_cleanup(layout, monkeypatch):
    coordinator, _, policy = layout
    lease = coordinator.acquire(policy())
    publish = coordinator._publish

    def published_then_failed(*args):
        publish(*args)
        raise OSError("synthetic directory fsync failure after rename")

    monkeypatch.setattr(coordinator, "_publish", published_then_failed)
    with pytest.raises(OSError, match="fsync"):
        lease.save_recovery({"phase": "prepared"})
    lease.verify()
    lease.release()
