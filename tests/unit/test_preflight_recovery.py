from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.sandbox.coordination import AdmissionCoordinator
from bot.sandbox.models import SandboxError, SandboxPolicy
from bot.sandbox.preflight_recovery import ProbeResources
from bot.sandbox.resources import DirectoryIdentity, OwnedDirectory


@pytest.fixture
def resources(tmp_path):
    state = tmp_path.resolve() / "state"
    state.mkdir(mode=0o700)
    parent = SimpleNamespace(
        state_root=state,
        cleanup_scope="parent-runtime-and-boot",
        _state_identity=DirectoryIdentity.capture(state),
        coordinator=AdmissionCoordinator(tmp_path.resolve() / "registry"),
    )
    owned = OwnedDirectory.create(state, prefix="preflight-")
    sockets = OwnedDirectory.create(Path("/tmp").resolve(), prefix="bot-check-")
    work, runs = owned.path / "work", owned.path / "runs"
    work.mkdir(mode=0o700)
    runs.mkdir(mode=0o700)
    manager = SimpleNamespace(
        workspace=work,
        _state_identity=DirectoryIdentity.capture(runs),
        cleanup_scope="child-runtime-and-boot",
    )
    value = ProbeResources.create(parent, owned, sockets, manager)
    try:
        yield value
    finally:
        # No process is ever started by this unit fixture. These exact original
        # directory handles, not a modified manifest, own fixture teardown.
        if sockets.path.exists():
            sockets.remove()
        if owned.path.exists():
            owned.remove()


def claim_as_restarted(resources, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr("bot.sandbox.preflight_recovery.owner_alive", lambda owner: False)
        return ProbeResources.recover(resources.parent, resources.owned.path)


def test_live_host_or_recoverer_cannot_be_overtaken(resources, monkeypatch):
    with pytest.raises(ValueError, match="仍存活"):
        ProbeResources.recover(resources.parent, resources.owned.path)
    restored = claim_as_restarted(resources, monkeypatch)
    assert restored.record["owner"] != resources.record["owner"]
    with pytest.raises(ValueError, match="仍存活"):
        ProbeResources.recover(resources.parent, resources.owned.path)
    with pytest.raises(ValueError, match="所有权"):
        resources.remove()
    assert resources.owned.path.exists() and resources.socket_directory.path.exists()
    restored.remove()
    assert not resources.owned.path.exists() and not resources.socket_directory.path.exists()


@pytest.mark.parametrize("change", ["runtime", "state", "workspace", "socket", "legacy", "owner"])
def test_mismatched_resources_never_grant_deletion_authority(resources, monkeypatch, change):
    value = deepcopy(resources.record)
    if change == "runtime":
        value["parent_scope"] = "different-runtime"
    elif change == "state":
        value["state_directory"]["inode"] += 1
    elif change == "workspace":
        value["workspace_directory"] = value["state_directory"]
    elif change == "socket":
        value["socket_directory"] = value["state_directory"]
    elif change == "legacy":
        value["schema"] = 1
    elif change == "owner":
        value["owner"]["started"] = float("nan")
    ProbeResources._publish(resources.owned, value)
    with pytest.raises((ValueError, SandboxError)):
        claim_as_restarted(resources, monkeypatch)
    assert ProbeResources._read(resources.owned)["owner"]["claim"] == value["owner"]["claim"]
    assert resources.owned.path.exists() and resources.socket_directory.path.exists()


@pytest.mark.parametrize("unsafe_file", ["symlink", "hardlink", "permissions", "missing"])
def test_manifest_file_must_be_private_and_independent(resources, tmp_path, unsafe_file):
    path = resources.owned.path / "resources.json"
    outside = tmp_path / "untouched"
    if unsafe_file == "symlink":
        path.rename(outside)
        path.symlink_to(outside)
    elif unsafe_file == "hardlink":
        outside.hardlink_to(path)
    elif unsafe_file == "missing":
        path.unlink()
    else:
        path.chmod(0o644)
    with pytest.raises((OSError, SandboxError)):
        ProbeResources.recover(resources.parent, resources.owned.path)
    assert resources.owned.path.exists() and resources.socket_directory.path.exists()


def test_missing_socket_directory_supports_partial_cleanup(resources, monkeypatch):
    resources.socket_directory.remove()
    restored = claim_as_restarted(resources, monkeypatch)
    assert restored.socket_directory.removed
    restored.remove()
    assert not restored.owned.path.exists()


def test_replaced_socket_directory_is_never_removed(resources, monkeypatch):
    path = resources.socket_directory.path
    saved = path.with_name(path.name + "-original")
    path.rename(saved)
    path.mkdir(mode=0o700)
    replacement = OwnedDirectory(DirectoryIdentity.capture(path))
    (path / "sentinel").write_text("unrelated")
    try:
        with pytest.raises(ValueError, match="身份已改变"):
            claim_as_restarted(resources, monkeypatch)
        assert (path / "sentinel").read_text() == "unrelated"
    finally:
        replacement.remove()
        saved.rename(path)


@pytest.mark.parametrize("key", ["workspace_directory", "state_directory"])
def test_replaced_child_directory_after_claim_blocks_removal(resources, monkeypatch, key):
    restored = claim_as_restarted(resources, monkeypatch)
    path = Path(restored.record[key]["path"])
    saved = path.with_name(path.name + "-original")
    path.rename(saved)
    path.mkdir(mode=0o700)
    replacement = OwnedDirectory(DirectoryIdentity.capture(path))
    (path / "sentinel").write_text("unrelated")
    try:
        with pytest.raises(ValueError, match="身份已改变"):
            restored.remove()
        assert (path / "sentinel").read_text() == "unrelated"
        assert resources.socket_directory.path.exists()
    finally:
        replacement.remove()
        saved.rename(path)


def test_remaining_child_lease_blocks_resource_deletion_even_without_a_live_manager(resources):
    work = Path(resources.record["workspace_directory"]["path"])
    lease = resources.parent.coordinator.acquire(
        SandboxPolicy(
            workspace=work,
            read_roots=(work,),
            write_roots=(work,),
            deny_read=(),
            deny_write=(),
            protected_paths=(),
            home_dir=work,
            temp_dir=work,
            cache_dir=work,
        )
    )
    try:
        with pytest.raises(ValueError, match="进程占用"):
            resources.remove()
        assert resources.owned.path.exists() and resources.socket_directory.path.exists()
    finally:
        lease.release()
    resources.remove()
