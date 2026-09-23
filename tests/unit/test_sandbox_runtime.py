import hashlib
import json
import shutil
import subprocess

import pytest
from typer.testing import CliRunner

from bot.cli.app import app
from bot.sandbox.runtime import MANIFEST_NAMES, inspect_runtime, manifest_directory


@pytest.fixture
def installation(tmp_path, monkeypatch):
    monkeypatch.setattr("bot.sandbox.runtime.platform.system", lambda: "Darwin")
    monkeypatch.setattr("bot.sandbox.runtime.platform.machine", lambda: "arm64")
    root = tmp_path / "runtime"
    workspace = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    workspace.mkdir()
    source = manifest_directory()
    for name in MANIFEST_NAMES:
        shutil.copyfile(source / name, root / name)
    lock = json.loads((root / "runtime-lock.json").read_text())
    record = {
        "platform": "darwin-arm64",
        "node": "v" + lock["node_version"],
        "source_hashes": {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in MANIFEST_NAMES
        },
    }
    (root / "installation.json").write_text(json.dumps(record))
    package = root / "node_modules/@anthropic-ai/sandbox-runtime"
    (package / "dist").mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"version": lock["srt_version"]}))
    (package / "dist/cli.js").touch()
    node = root / "node/bin/node"
    node.parent.mkdir(parents=True)
    node.touch(mode=0o700)
    return workspace, root, lock


def test_matching_metadata_never_claims_enforcement_or_runs_a_binary(installation, monkeypatch):
    workspace, root, lock = installation

    def unexpected(*a, **kw):
        pytest.fail("metadata inspection must not execute binaries")

    monkeypatch.setattr(subprocess, "run", unexpected)
    inspection = inspect_runtime(workspace, root=root)
    # A Linux test runner lacks sandbox-exec; the metadata still records the
    # version but correctly reports the OS dependency as unavailable.
    assert inspection.capabilities.backend_version == lock["srt_version"]
    assert inspection.capabilities.node_version == lock["node_version"]
    assert inspection.capabilities.file_read in {"unverified", "unavailable"}
    assert inspection.capabilities.network_filter != "enforced"
    assert inspection.capabilities.process_isolation != "enforced"
    assert inspection.capabilities.resource_limits == "unavailable"
    assert dict(inspection.executables)["node"] == root / "node/bin/node"


@pytest.mark.parametrize("change", ["version", "lock", "platform", "missing", "symlink", "mode"])
def test_changed_or_untrusted_installation_is_unavailable(installation, tmp_path, change):
    workspace, root, _ = installation
    if change == "version":
        (root / "node_modules/@anthropic-ai/sandbox-runtime/package.json").write_text(
            '{"version":"0"}'
        )
    elif change == "lock":
        with (root / "package-lock.json").open("a") as stream:
            stream.write(" ")
    elif change == "platform":
        record = json.loads((root / "installation.json").read_text())
        record["platform"] = "linux-arm64"
        (root / "installation.json").write_text(json.dumps(record))
    elif change == "missing":
        (root / "node/bin/node").unlink()
    elif change == "symlink":
        external = tmp_path / "external"
        external.write_text("{}")
        (root / "installation.json").unlink()
        (root / "installation.json").symlink_to(external)
    else:
        root.chmod(0o777)
    inspection = inspect_runtime(workspace, root=root)
    assert inspection.errors
    assert inspection.capabilities.file_read == "unavailable"


def test_project_runtime_and_path_override_are_not_trusted(installation, monkeypatch):
    workspace, root, _ = installation
    monkeypatch.setenv("PATH", str(root / "node/bin"))
    inspection = inspect_runtime(workspace, root=workspace)
    assert "工作区之外" in inspection.errors[0]
    assert not inspection.executables


def test_missing_runtime_is_reported_without_installation(tmp_path):
    root = tmp_path / "not-installed"
    inspection = inspect_runtime(tmp_path / "work", root=root)
    assert inspection.capabilities.file_read == "unavailable"
    assert not root.exists()


def test_cli_required_reports_closed_gate_without_provider_or_target_execution(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "work"
    project.mkdir()
    config = tmp_path / "trusted.toml"
    config.write_text('[sandbox]\nmode = "required"\n')

    def unexpected(**kwargs):
        pytest.fail("required preflight must run before constructing the model provider")

    monkeypatch.setattr("bot.cli.runtime.OpenAICompatibleProvider", unexpected)
    result = CliRunner().invoke(app, ["-C", str(project), "--config", str(config), "run", "hello"])
    assert result.exit_code == 2, result.output
    assert "SANDBOX_UNAVAILABLE" in result.output
    assert "Traceback" not in result.output
    assert not (project / ".bot").exists()


def test_doctor_distinguishes_unavailable_runtime_from_os_enforcement(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "work"
    project.mkdir()
    config = tmp_path / "trusted.toml"
    config.write_text("""[sandbox]
mode = "required"
[model]
name = "synthetic"
base_url = "https://example.invalid/v1"
api_key = "synthetic-not-a-real-key"
[storage]
state_path = "./.bot/state.db"
""")
    result = CliRunner().invoke(app, ["-C", str(project), "--config", str(config), "doctor"])
    assert result.exit_code != 0
    output = "".join(result.output.split())
    assert "执行已关闭" in output
    assert "file_read=unavailable" in output
    assert "resource_limits=unavailable" in output
    assert "synthetic-not-a-real-key" not in output
