import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from bot.sandbox import host_runtime as host


@pytest.fixture
def helpers(tmp_path, monkeypatch):
    root, work, bins, links = (tmp_path / name for name in ("runtime", "work", "bin", "links"))
    for path in (root, work, bins, links):
        path.mkdir(mode=0o700)
    executable = bins / "real"
    executable.write_text("#!/bin/sh\nexit 91\n")
    executable.chmod(0o700)
    (bins / "sh").symlink_to("../links/current")
    (links / "current").symlink_to("../bin/real")
    # Synthetic admin-owned hierarchy. Production SYSTEM_OWNER remains uid 0.
    monkeypatch.setattr(host, "SYSTEM_OWNER", os.getuid())
    monkeypatch.setattr(host, "fixed_executables", lambda: {"sh": bins / "sh"})
    digest = host.seal_host_executables(root, workspace=work)
    raw = (root / host.MANIFEST_NAME).read_bytes()
    return root, work, bins, links, raw, digest


def inspect(helpers, **kwargs):
    _, work, _, _, raw, digest = helpers
    return host.inspect_host_executables(raw, manifest_sha256=digest, workspace=work, **kwargs)


def test_sealing_and_inspection_never_execute_a_candidate(helpers, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("candidate execution is not an integrity check")

    monkeypatch.setattr(subprocess, "run", unexpected)
    monkeypatch.setattr(subprocess, "Popen", unexpected)
    snapshot = inspect(helpers)
    assert snapshot.executables[0][1].canonical == helpers[2] / "real"
    assert helpers[2] / "sh" in snapshot.read_roots
    assert any(
        path.resolve() == helpers[2] / "real" and "links" in path.parts
        for path in snapshot.read_roots
    )
    assert inspect(helpers, previous=snapshot) is snapshot


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize(
    "change",
    ["content", "retarget", "writable_file", "writable_parent", "hardlink", "no_exec", "missing"],
)
def test_changed_helper_is_rejected_before_execution(helpers, change, cached):
    _, _, bins, links, _, _ = helpers
    previous = inspect(helpers) if cached else None
    executable = bins / "real"
    if change == "content":
        executable.write_text("#!/bin/sh\nexit 92\n")
    elif change == "retarget":
        other = bins / "other"
        other.write_bytes(executable.read_bytes())
        other.chmod(0o700)
        (links / "current").unlink()
        (links / "current").symlink_to(other)
    elif change == "writable_file":
        executable.chmod(0o777)
    elif change == "writable_parent":
        links.chmod(0o777)
    elif change == "hardlink":
        (bins / "alias").hardlink_to(executable)
    elif change == "no_exec":
        executable.chmod(0o600)
    elif change == "missing":
        executable.unlink()
    with pytest.raises((ValueError, OSError)):
        inspect(helpers, previous=previous)


def test_identity_replacement_cannot_reuse_cached_attestation(helpers):
    _, _, bins, _, _, _ = helpers
    previous = inspect(helpers)
    program = bins / "real"
    replacement = bins / "replacement"
    replacement.write_bytes(program.read_bytes())
    replacement.chmod(0o700)
    replacement.replace(program)
    with pytest.raises(ValueError, match="身份"):
        inspect(helpers, previous=previous)
    # Fresh inspection verifies the administrator-owned bytes again.
    assert inspect(helpers).executables[0][1] != previous.executables[0][1]


def test_intermediate_link_replacement_is_detected_even_with_the_same_final_file(helpers):
    _, _, _, links, _, _ = helpers
    previous = inspect(helpers)
    (links / "replacement").symlink_to("../bin/real")
    (links / "replacement").replace(links / "current")
    with pytest.raises(ValueError, match="身份"):
        inspect(helpers, previous=previous)


def test_link_loop_is_an_unavailable_dependency(helpers):
    _, _, _, links, _, _ = helpers
    (links / "current").unlink()
    (links / "current").symlink_to("../bin/sh")
    with pytest.raises((ValueError, OSError)):
        inspect(helpers)


@pytest.mark.parametrize("change", ["list", "bool_schema", "extra", "missing", "path", "digest"])
def test_invalid_manifest_never_selects_an_arbitrary_executable(helpers, change):
    _, work, _, _, raw, _ = helpers
    record = json.loads(raw)
    if change == "list":
        record = []
    elif change == "bool_schema":
        record["schema"] = True
    elif change == "extra":
        record["executables"]["attacker"] = record["executables"]["sh"]
    elif change == "missing":
        del record["executables"]["sh"]
    elif change == "path":
        record["executables"]["sh"]["requested"] = str(work / "untrusted")
    else:
        record["executables"]["sh"]["sha256"] = "0" * 64
    raw = json.dumps(record).encode()
    with pytest.raises(ValueError):
        host.inspect_host_executables(
            raw, manifest_sha256=hashlib.sha256(raw).hexdigest(), workspace=work
        )


def test_workspace_binary_and_shadow_path_are_not_authority(helpers, monkeypatch):
    root, work, bins, links, _, _ = helpers
    before = inspect(helpers)
    injected = work / "sh"
    injected.write_text("#!/bin/sh\nexit 0\n")
    injected.chmod(0o700)
    monkeypatch.setenv("PATH", str(work))
    assert inspect(helpers, previous=before) is before
    (links / "current").unlink()
    (links / "current").symlink_to(injected)
    with pytest.raises(ValueError, match="工作区"):
        inspect(helpers)


def test_no_runtime_can_override_the_fixed_program_set(monkeypatch):
    monkeypatch.setattr(host.platform, "system", lambda: "Linux")
    assert host.fixed_executables()["bwrap"] == Path("/usr/bin/bwrap")
    assert all(
        str(path).startswith(("/bin/", "/usr/bin/")) for path in host.fixed_executables().values()
    )
    monkeypatch.setattr(host.platform, "system", lambda: "Darwin")
    assert host.fixed_executables()["sandbox-exec"] == Path("/usr/bin/sandbox-exec")
    assert "bwrap" not in host.fixed_executables()
