import asyncio
import hashlib
import json
import os

import pytest
from typer.testing import CliRunner

from bot.cli.app import _with_runtime_shutdown, app
from bot.config.models import AppConfig
from bot.sandbox import diagnostics
from bot.sandbox.coordination import AdmissionCoordinator
from bot.sandbox.models import SandboxCapabilities, SandboxError, SandboxErrorCode, SandboxPolicy
from bot.sandbox.runtime import RuntimeInspection


@pytest.fixture
def layout(tmp_path, monkeypatch):
    work, home = tmp_path / "work", tmp_path / "home"
    work.mkdir()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for key in tuple(os.environ):
        if key.startswith("BOT_"):
            monkeypatch.delenv(key)
    registry = tmp_path / "registry"
    monkeypatch.setattr("bot.sandbox.coordination.default_root", lambda: registry)
    capabilities = SandboxCapabilities(os_version="test", architecture="test")
    inspection = RuntimeInspection(tmp_path / "runtime", capabilities)
    monkeypatch.setattr(diagnostics, "inspect_runtime", lambda *a, **k: inspection)
    return work, home, registry, inspection


def test_readonly_cli_needs_no_model_and_creates_no_files_or_native_target(layout, monkeypatch):
    work, home, registry, _ = layout

    def forbidden(*args, **kwargs):
        pytest.fail("read-only diagnostic cannot start a target or resolve model credentials")

    monkeypatch.setattr(diagnostics, "NativeSandboxExecutionTarget", forbidden)
    monkeypatch.setattr("bot.cli.app.resolve_model_api_key", forbidden)
    result = CliRunner().invoke(app, ["-C", str(work), "doctor", "--sandbox", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["ok"] and report["probe"]["status"] == "not_run"
    assert report["capabilities"]["file_read"] == "unverified"
    assert report["business_execution_enabled"] is False
    assert report["business_execution_attempted"] is False
    assert report["recovery_after"]["cleanup_authority"] is False
    assert list(work.iterdir()) == [] and list(home.iterdir()) == [] and not registry.exists()


def test_readonly_inventory_preserves_owner_and_does_not_display_journal_payload(layout):
    work, _, registry, _ = layout
    state = work.parent / "private-state"
    state.mkdir(mode=0o700)
    run = state / "run-old"
    run.mkdir(mode=0o700)
    coordinator = AdmissionCoordinator(registry)
    lease = coordinator.acquire(
        SandboxPolicy(
            workspace=work,
            read_roots=(work, run),
            write_roots=(run,),
            deny_read=(),
            deny_write=(),
            protected_paths=(),
            home_dir=run,
            temp_dir=run,
            cache_dir=run,
        )
    )
    lease.save_recovery(
        {
            "phase": "registered",
            "argv": ["must-not-display"],
            "env": {"KEY": "must-not-display"},
            "stdout": "must-not-display",
        }
    )
    before = {
        p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
        for p in registry.iterdir()
    }
    try:
        result = CliRunner().invoke(
            app, ["-C", str(work), "doctor", "--sandbox", "--json", "--sandbox-state", str(state)]
        )
        assert result.exit_code == 1, result.output
        report = json.loads(result.output)
        assert report["recovery_before"]["status"] == "pending"
        assert report["recovery_before"]["leases"] == [
            {"id": lease.binding["id"], "phase": "registered", "owner": "alive"}
        ]
        assert "must-not-display" not in result.output
        assert before == {
            p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in registry.iterdir()
        }
        lease.verify()
    finally:
        lease.release()


def test_missing_registry_mutex_is_reported_without_creating_one(layout):
    _, _, registry, _ = layout
    registry.mkdir(mode=0o700)
    with pytest.raises(FileNotFoundError):
        AdmissionCoordinator.read_snapshots()
    assert list(registry.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["passed", "probe_failure", "cleanup_failure", "cancel"])
async def test_probe_lifecycle_reports_failures_and_always_closes_target(
    layout, monkeypatch, outcome
):
    work, _, _, inspection = layout
    started, release = asyncio.Event(), asyncio.Event()
    actions = []

    class Target:
        def __init__(self, config, **kwargs):
            assert config.sandbox.mode == "required" and config.sandbox.network.mode == "deny"
            assert config.permissions.network == "deny" and not config.model.api_key
            assert config.sandbox.read_roots == () and config.sandbox.write_roots == ()
            self.sandbox_capabilities = inspection.capabilities

        async def prepare_run(self):
            actions.append("prepare")
            started.set()
            await release.wait()
            if outcome == "probe_failure":
                raise SandboxError(
                    SandboxErrorCode.CLEANUP_FAILED, "unknown", stage="startup_recovery"
                )
            self.sandbox_capabilities = inspection.capabilities.model_copy(
                update={"file_read": "enforced"}
            )

        async def aclose(self):
            actions.append("close")
            if outcome == "cleanup_failure":
                raise RuntimeError("cleanup incomplete")

    monkeypatch.setattr(diagnostics, "NativeSandboxExecutionTarget", Target)
    config = AppConfig.model_validate({"model": {"api_key": "must-not-leak"}})
    session = diagnostics.SandboxDiagnostic(config, work, probe=True)
    task = asyncio.create_task(_with_runtime_shutdown(session, session.run()))
    await asyncio.wait_for(started.wait(), 2)
    if outcome == "cancel":
        task.cancel()
    release.set()
    await task
    assert actions == ["prepare", "close"]
    assert session.report["ok"] is (outcome == "passed")
    assert session.exit_code() == (0 if outcome == "passed" else 130 if outcome == "cancel" else 1)
    assert "must-not-leak" not in json.dumps(session.report)
    if outcome == "probe_failure":
        assert session.report["errors"][0]["stage"] == "startup_recovery"
    if outcome == "cleanup_failure":
        assert session.report["probe"]["status"] == "passed"
        assert session.report["cleanup"]["status"] == "incomplete"


def test_invalid_installation_never_constructs_a_probe(layout, monkeypatch):
    work, _, _, inspection = layout
    monkeypatch.setattr(
        diagnostics,
        "inspect_runtime",
        lambda *a, **k: RuntimeInspection(
            inspection.root, inspection.capabilities, errors=("missing locked runtime",)
        ),
    )
    monkeypatch.setattr(
        diagnostics,
        "NativeSandboxExecutionTarget",
        lambda *a, **k: pytest.fail("invalid metadata cannot start a probe"),
    )
    result = CliRunner().invoke(app, ["-C", str(work), "doctor", "--sandbox-probe", "--json"])
    assert result.exit_code == 1
    report = json.loads(result.output)
    assert report["probe"]["status"] == "not_run" and not report["runtime"]["metadata_valid"]


def test_project_cannot_use_diagnostic_to_relax_security_configuration(layout):
    work, home, _, _ = layout
    (home / ".bot").mkdir()
    (home / ".bot/config.toml").write_text('[sandbox]\nmode="required"\n')
    (work / ".bot").mkdir()
    (work / ".bot/config.toml").write_text('[sandbox]\nmode="off"\n')
    result = CliRunner().invoke(app, ["-C", str(work), "doctor", "--sandbox-probe", "--json"])
    assert result.exit_code == 2
    assert json.loads(result.output)["errors"][0]["code"] == "SANDBOX_CONFIG_INVALID"


def test_sandbox_options_require_explicit_sandbox_diagnostic(layout):
    work, _, _, _ = layout
    result = CliRunner().invoke(app, ["-C", str(work), "doctor", "--json"])
    assert result.exit_code == 2 and "--sandbox" in result.output
