import hashlib
import json
import platform

import pytest

from bot.sandbox.worker_runtime import inspect_worker


@pytest.fixture
def installed(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    (root / "python-cache").mkdir()
    (root / "worker.py").write_text("# worker")
    stdlib = root / "stdlib"
    stdlib.mkdir()
    source = stdlib / "module.py"
    source.write_text("# trusted module")
    executable = root / "python"
    executable.write_text("synthetic, never execute")
    executable.chmod(0o700)
    metadata = {
        "executable": str(executable),
        "stdlib": str(stdlib),
        "version": "3.12.11",
        "system": platform.system(),
        "machine": platform.machine(),
        "libraries": [],
        "cache_mode": "empty-prefix",
        "worker_sha256": hashlib.sha256((root / "worker.py").read_bytes()).hexdigest(),
        "files": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (executable, source)},
    }
    raw = json.dumps(metadata).encode()
    (root / "worker-python.json").write_bytes(raw)
    return root, tmp_path / "work", hashlib.sha256(raw).hexdigest()


def test_inspection_never_executes_candidate_and_reuses_verified_identity(installed, monkeypatch):
    root, work, digest = installed

    def unexpected(*args, **kwargs):
        pytest.fail("runtime inspection must not execute candidate binaries")

    monkeypatch.setattr("subprocess.run", unexpected)
    snapshot = inspect_worker(root, work, manifest_sha256=digest)
    assert inspect_worker(root, work, manifest_sha256=digest, previous=snapshot) is snapshot
    assert snapshot.executable == root / "python"
    assert root / "stdlib" in snapshot.read_roots
    assert root / "stdlib/site-packages" in snapshot.denied_roots


@pytest.mark.parametrize(
    "change",
    [
        "binary",
        "module",
        "new_module",
        "symlink",
        "cache",
        "manifest",
        "worker",
        "permissions",
        "parent",
        "workspace",
    ],
)
def test_changed_or_project_worker_runtime_fails_closed(installed, change):
    root, work, digest = installed
    snapshot = inspect_worker(root, work, manifest_sha256=digest)
    if change == "binary":
        (root / "python").write_text("replaced")
    elif change == "module":
        (root / "stdlib/module.py").write_text("# malicious module")
    elif change == "new_module":
        (root / "stdlib/new.py").write_text("# new module")
    elif change == "symlink":
        (root / "stdlib/module.py").unlink()
        (root / "stdlib/module.py").symlink_to(root / "python")
    elif change == "cache":
        (root / "python-cache/injected.pyc").write_bytes(b"bad")
    elif change == "manifest":
        (root / "worker-python.json").write_text("{}")
    elif change == "worker":
        (root / "worker.py").write_text("changed")
    elif change == "permissions":
        (root / "python").chmod(0o777)
    elif change == "parent":
        root.chmod(0o777)
    else:
        work = root
    with pytest.raises((ValueError, OSError)):
        inspect_worker(root, work, manifest_sha256=digest)
    # The generic runtime inspector separately pins worker.py; fast Python
    # verification is still responsible for every interpreter and stdlib asset.
    if change != "worker":
        with pytest.raises((ValueError, OSError)):
            inspect_worker(root, work, manifest_sha256=digest, previous=snapshot)


def test_ambient_stdlib_cache_is_unused_and_not_part_of_admission(installed):
    root, work, digest = installed
    snapshot = inspect_worker(root, work, manifest_sha256=digest)
    cache = root / "stdlib/__pycache__"
    cache.mkdir()
    (cache / "module.cpython-312.pyc").write_bytes(b"not loaded with the fixed empty prefix")
    assert inspect_worker(root, work, manifest_sha256=digest, previous=snapshot) is snapshot
    assert not list((root / "python-cache").iterdir())
