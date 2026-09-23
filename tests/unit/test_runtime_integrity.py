import hashlib
import json
import os
import shutil
import subprocess

import pytest

from bot.sandbox.runtime_integrity import inspect_tree, seal_runtime


@pytest.fixture
def installation(tmp_path):
    root = tmp_path / "runtime"
    (root / "node/bin").mkdir(parents=True)
    (root / "node_modules/vendor").mkdir(parents=True)
    (root / "node/bin/node").write_text("synthetic executable, never run")
    (root / "node/bin/node").chmod(0o755)
    (root / "node_modules/vendor/library.js").write_text("original module")
    (root / "node/bin/npm").symlink_to("../../node_modules/vendor/library.js")
    work = tmp_path / "workspace"
    digest = seal_runtime(root, workspace=work)
    return root, work, digest


def test_discovery_hashes_once_then_checks_identities_without_executing(installation, monkeypatch):
    root, work, digest = installation

    def forbidden(*args, **kwargs):
        pytest.fail("discovery cannot execute candidates or rehash an unchanged tree")

    monkeypatch.setattr(subprocess, "run", forbidden)
    snapshot = inspect_tree(root, work, manifest_sha256=digest)
    assert len(snapshot.state.files) == 2 and len(snapshot.state.links) == 1
    monkeypatch.setattr("bot.sandbox.runtime_integrity._digest", forbidden)
    assert inspect_tree(root, work, manifest_sha256=digest, previous=snapshot) is snapshot


@pytest.mark.parametrize(
    "change",
    [
        "node",
        "vendor",
        "restored_mtime",
        "added",
        "removed",
        "link",
        "escape",
        "loop",
        "hardlink",
        "fifo",
        "permissions",
        "mode",
        "parent",
        "manifest",
        "manifest_fifo",
    ],
)
def test_changes_fail_before_candidate_execution(installation, change):
    root, work, digest = installation
    snapshot = inspect_tree(root, work, manifest_sha256=digest)
    vendor = root / "node_modules/vendor/library.js"
    link = root / "node/bin/npm"
    if change == "node":
        (root / "node/bin/node").write_text("replaced executable")
    elif change in {"vendor", "restored_mtime"}:
        info = vendor.stat()
        vendor.write_text("modified module")
        if change == "restored_mtime":
            os.utime(vendor, ns=(info.st_atime_ns, info.st_mtime_ns))
    elif change == "added":
        (root / "node_modules/vendor/new.js").write_text("new executable dependency")
    elif change == "removed":
        vendor.unlink()
    elif change in {"link", "escape", "loop"}:
        link.unlink()
        if change == "escape":
            outside = root.parent / "outside"
            outside.write_text("external")
            link.symlink_to(outside)
        else:
            link.symlink_to("node" if change == "link" else "npm")
    elif change == "hardlink":
        os.link(vendor, root.parent / "external-alias")
    elif change == "fifo":
        (root / "node_modules/pipe").touch()
        (root / "node_modules/pipe").unlink()
        os.mkfifo(root / "node_modules/pipe")
    elif change == "permissions":
        vendor.chmod(0o666)
    elif change == "mode":
        vendor.chmod(0o600)
    elif change == "parent":
        root.parent.chmod(0o777)
    else:
        manifest = root / "runtime-files.json"
        manifest.unlink()
        if change == "manifest_fifo":
            os.mkfifo(manifest)
        else:
            manifest.write_text("{}")
    for previous in (None, snapshot):
        with pytest.raises((OSError, ValueError)):
            inspect_tree(root, work, manifest_sha256=digest, previous=previous)


def test_file_replacement_with_same_bytes_invalidates_live_snapshot(installation):
    root, work, digest = installation
    snapshot = inspect_tree(root, work, manifest_sha256=digest)
    node = root / "node/bin/node"
    replacement = root / "node/bin/replacement"
    shutil.copy2(node, replacement)
    replacement.replace(node)
    with pytest.raises(ValueError, match="身份"):
        inspect_tree(root, work, manifest_sha256=digest, previous=snapshot)
    # A new trusted runtime instance can establish a new identity after checking
    # the same sealed contents; the old live instance cannot silently accept it.
    assert inspect_tree(root, work, manifest_sha256=digest).state != snapshot.state


def test_manifest_must_describe_the_entire_tree_and_cannot_be_resealed(installation):
    root, work, digest = installation
    manifest = root / "runtime-files.json"
    record = json.loads(manifest.read_text())
    record["files"].pop("node/bin/node")
    raw = json.dumps(record).encode()
    manifest.write_bytes(raw)
    with pytest.raises(ValueError, match="清单"):
        inspect_tree(root, work, manifest_sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(FileExistsError):
        seal_runtime(root, workspace=work)


def test_workspace_and_sibling_unsealed_link_are_rejected(installation):
    root, work, digest = installation
    with pytest.raises(ValueError, match="工作区"):
        inspect_tree(root, root / "task", manifest_sha256=digest)
    (root / "unsealed").write_text("outside the sealed subtrees")
    (root / "node/bin/npm").unlink()
    (root / "node/bin/npm").symlink_to("../../unsealed")
    with pytest.raises(ValueError, match="符号链接"):
        inspect_tree(root, work, manifest_sha256=digest)
