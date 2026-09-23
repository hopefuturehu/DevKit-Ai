from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "install_sandbox_runtime",
    Path(__file__).resolve().parents[2] / "scripts/install_sandbox_runtime.py",
)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "checkout/runtime/sandbox"
    root.mkdir(parents=True)
    for name in ("package.json", "package-lock.json", "launcher.mjs"):
        (root / name).write_text("{}")
    monkeypatch.setattr(installer, "SOURCE", root)
    monkeypatch.setattr(installer.platform, "system", lambda: "Linux")
    monkeypatch.setattr(installer.platform, "machine", lambda: "aarch64")
    return root


def archive_for(source, *, unsafe=False):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as package:
        name = "../escaped" if unsafe else "node-v24.14.0-linux-arm64/bin/node"
        info = tarfile.TarInfo(name)
        info.size = 4
        package.addfile(info, io.BytesIO(b"test"))
    data = stream.getvalue()
    (source / "runtime-lock.json").write_text(
        json.dumps(
            {
                "node_version": "24.14.0",
                "srt_version": "0.0.77",
                "node_archives": {"linux-arm64": hashlib.sha256(data).hexdigest()},
            }
        )
    )
    return data


def test_rejects_workspace_installation(source):
    with pytest.raises(ValueError, match="outside the task workspace"):
        installer.install(source.parents[1] / "runtime-install")


def test_never_overwrites_existing_installation(source, tmp_path):
    destination = tmp_path / "installed"
    destination.mkdir()
    (destination / "sentinel").write_text("keep")
    with pytest.raises(ValueError, match="already exists"):
        installer.install(destination)
    assert (destination / "sentinel").read_text() == "keep"


def test_rejects_download_before_extraction_if_checksum_differs(source, tmp_path, monkeypatch):
    archive_for(source)
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"bad"))
    with pytest.raises(ValueError, match="checksum mismatch"):
        installer.install(tmp_path / "installed")
    assert not (tmp_path / "installed").exists()
    assert not list(tmp_path.glob(".bot-runtime-install-*"))


def test_tar_cannot_write_outside_staging_area(source, tmp_path, monkeypatch):
    data = archive_for(source, unsafe=True)
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(data))
    with pytest.raises(tarfile.FilterError):
        installer.install(tmp_path / "installed")
    assert not (tmp_path / "escaped").exists()
    assert not (tmp_path / "installed").exists()


def test_npm_failure_does_not_publish_partial_runtime(source, tmp_path, monkeypatch):
    data = archive_for(source)
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(data))

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(installer.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        installer.install(tmp_path / "installed")
    assert not (tmp_path / "installed").exists()
    assert not list(tmp_path.glob(".bot-runtime-install-*"))


def test_install_uses_lock_and_isolated_npm_config(source, tmp_path, monkeypatch):
    data = archive_for(source)
    monkeypatch.setattr(installer.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(data))
    monkeypatch.setenv("NODE_OPTIONS", "--require=/untrusted/injection.js")
    monkeypatch.setenv("MODEL_API_KEY", "synthetic-secret")

    def install_npm(argv, *, cwd, env, **kwargs):
        assert "ci" in argv and "--ignore-scripts" in argv
        assert "NODE_OPTIONS" not in env and "MODEL_API_KEY" not in env
        user = Path(env["npm_config_userconfig"])
        global_ = Path(env["npm_config_globalconfig"])
        assert user != global_ and user.read_text() == global_.read_text() == ""
        package = cwd / "node_modules/@anthropic-ai/sandbox-runtime/package.json"
        package.parent.mkdir(parents=True)
        package.write_text('{"version":"0.0.77"}')

    monkeypatch.setattr(installer.subprocess, "run", install_npm)
    monkeypatch.setattr(installer.subprocess, "check_output", lambda *a, **kw: "v24.14.0\n")
    destination = installer.install(tmp_path / "installed")
    record = json.loads((destination / "installation.json").read_text())
    assert record["platform"] == "linux-arm64"
    assert (
        record["source_hashes"]["launcher.mjs"]
        == hashlib.sha256((source / "launcher.mjs").read_bytes()).hexdigest()
    )
    assert (
        record["source_hashes"]["runtime-lock.json"]
        == hashlib.sha256((source / "runtime-lock.json").read_bytes()).hexdigest()
    )
