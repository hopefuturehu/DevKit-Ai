import hashlib
import json
import os
import subprocess
from dataclasses import replace

import pytest

from bot.sandbox import host_runtime
from bot.sandbox import loader_controls as loader


@pytest.fixture
def controls(tmp_path, monkeypatch):
    root, work, system = (tmp_path / name for name in ("runtime", "work", "system"))
    for path in (root, work, system):
        path.mkdir(mode=0o700)
    config = system / "loader.conf"
    config.write_text("trusted config")
    (system / "config.d").mkdir()
    (system / "config.d/one.conf").write_text("trusted entry")
    (system / "cache").mkdir()
    (system / "cache/payload").write_bytes(b"synthetic cache")
    mapping = {
        "config": (config, "file"),
        "preload": (system / "preload", "absent"),
        "optional": (system / "missing/cache", "identity_directory"),
        "config_d": (system / "config.d", "config_directory"),
        "cache": (system / "cache", "identity_directory"),
    }
    monkeypatch.setattr(host_runtime, "SYSTEM_OWNER", os.getuid())
    monkeypatch.setattr(loader, "fixed_controls", lambda: mapping)
    return root, work, system, mapping


def seal(controls):
    root, work, _, _ = controls
    digest = loader.seal_loader_controls(root, workspace=work)
    return (root / loader.MANIFEST_NAME).read_bytes(), digest


def inspect(controls, raw, digest, **kwargs):
    return loader.inspect_loader_controls(
        raw, manifest_sha256=digest, workspace=controls[1], **kwargs
    )


def test_seal_and_inspect_do_not_execute_programs_or_read_cache_payloads(controls, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("loader control inspection must not execute a candidate")

    monkeypatch.setattr(subprocess, "run", unexpected)
    monkeypatch.setattr(subprocess, "Popen", unexpected)
    original = loader._content

    def read(path):
        assert path != controls[2] / "cache/payload"
        return original(path)

    monkeypatch.setattr(loader, "_content", read)
    raw, digest = seal(controls)
    initial = inspect(controls, raw, digest)
    assert inspect(controls, raw, digest, previous=initial) is initial
    assert controls[2] / "preload" in initial.protected_write_paths
    assert controls[2] / "missing/cache" in initial.protected_write_paths


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize(
    "change",
    [
        "preload",
        "config",
        "replace",
        "directory",
        "new_entry",
        "cache_payload",
        "missing_parent",
        "permissions",
        "dangling",
        "hardlink",
    ],
)
def test_changed_loader_state_is_never_silently_rebaselined(controls, cached, change):
    _, _, system, _ = controls
    raw, digest = seal(controls)
    previous = inspect(controls, raw, digest) if cached else None
    if change == "preload":
        (system / "preload").touch()
    elif change == "config":
        (system / "loader.conf").write_text("changed config")
    elif change == "replace":
        (system / "replacement").write_bytes((system / "loader.conf").read_bytes())
        (system / "replacement").replace(system / "loader.conf")
    elif change == "directory":
        (system / "config.d").rename(system / "previous")
        (system / "config.d").mkdir()
        (system / "config.d/one.conf").write_text("trusted entry")
    elif change == "new_entry":
        (system / "config.d/two.conf").touch()
    elif change == "cache_payload":
        (system / "cache/payload").write_bytes(b"changed cache")
    elif change == "missing_parent":
        (system / "missing").mkdir()
    elif change == "permissions":
        (system / "config.d").chmod(0o777)
    elif change == "dangling":
        (system / "loader.conf").unlink()
        (system / "loader.conf").symlink_to(system / "missing-target")
    else:
        (system / "alias").hardlink_to(system / "loader.conf")
    with pytest.raises((OSError, ValueError)):
        inspect(controls, raw, digest, previous=previous)


def test_loader_manifest_cannot_select_arbitrary_paths_or_bool_schema(controls):
    raw, _ = seal(controls)
    for mutate in (
        lambda value: value.update(schema=True),
        lambda value: value["controls"]["config"].update(path="/arbitrary"),
    ):
        value = json.loads(raw)
        mutate(value)
        changed = json.dumps(value).encode()
        with pytest.raises(ValueError):
            inspect(controls, changed, hashlib.sha256(changed).hexdigest())


def test_external_config_symlink_target_is_also_protected(controls):
    _, _, system, _ = controls
    external = system / "external.conf"
    external.write_text("external config")
    (system / "config.d/link.conf").symlink_to(external)
    raw, digest = seal(controls)
    snapshot = inspect(controls, raw, digest)
    assert external in snapshot.protected_write_paths
    assert system / "config.d/link.conf" in snapshot.protected_write_paths


@pytest.mark.parametrize("kind", ["config_directory", "identity_directory"])
def test_directory_entry_bound_and_special_files_fail_closed(controls, monkeypatch, kind):
    root, work, system, mapping = controls
    mapping.clear()
    mapping["only"] = (system / "config.d", kind)
    monkeypatch.setattr(loader, "ENTRY_LIMIT", 1)
    (system / "config.d/two.conf").touch()
    with pytest.raises(ValueError, match="超过限制"):
        loader.seal_loader_controls(root, workspace=work)
    (system / "config.d/two.conf").unlink()
    (system / "config.d/one.conf").unlink()
    os.mkfifo(system / "config.d/pipe")
    with pytest.raises(ValueError):
        loader.seal_loader_controls(root, workspace=work)


def test_fixed_map_change_cannot_reuse_cached_state(controls):
    raw, digest = seal(controls)
    snapshot = inspect(controls, raw, digest)
    controls[3].pop("preload")
    with pytest.raises(ValueError):
        inspect(controls, raw, digest, previous=snapshot)
    with pytest.raises(ValueError):
        inspect(controls, raw, digest, previous=replace(snapshot, manifest_sha256="changed"))
