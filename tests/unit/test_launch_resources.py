import asyncio
import subprocess
import sys

import pytest

from bot.config.models import AppConfig, ProcessCleanupConfig
from bot.execution.base import ProcessSpec, ProcessStatus
from bot.execution.local import LocalExecutionTarget, PreparedLaunch
from bot.execution.native import NativeSandboxExecutionTarget
from bot.sandbox.models import SandboxError
from bot.sandbox.resources import OwnedDirectory
from bot.sandbox.runtime import manifest_directory


def test_native_allowlist_gate_is_closed_before_runtime_or_resource_access(tmp_path):
    config = AppConfig.model_validate(
        {
            "sandbox": {
                "mode": "required",
                "network": {"mode": "allowlist", "allowed_domains": ["example.com"]},
            },
            "permissions": {"network": "allow"},
        }
    )
    with pytest.raises(SandboxError, match="allowlist"):
        NativeSandboxExecutionTarget(
            config,
            workspace=tmp_path,
            runtime_root=tmp_path / "absent",
            state_root=tmp_path / "state",
        )
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("options", [["--bad-option"], ["--args", "bad-fd"]])
def test_bwrap_adapter_rejects_unknown_options_before_start(tmp_path, options):
    script = manifest_directory() / "bwrap-launcher.sh"
    result = subprocess.run(
        ["/bin/bash", str(script), *options, "--", "/bin/bash", "-c", "touch executed"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 125 and not (tmp_path / "executed").exists()


def test_owned_cleanup_rejects_replacement_and_does_not_follow_task_symlinks(tmp_path):
    resource = OwnedDirectory.create(tmp_path, prefix="owned-")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("preserve")
    (resource.path / "link").symlink_to(outside, target_is_directory=True)
    original = resource.path.with_name("moved")
    resource.path.rename(original)
    resource.path.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="身份已改变"):
        resource.remove()
    resource.path.rmdir()
    original.rename(resource.path)
    resource.remove()
    resource.remove()
    assert sentinel.read_text() == "preserve" and not resource.path.exists()


class WrappedTarget(LocalExecutionTarget):
    def __init__(self, argv, release):
        super().__init__(
            max_managed_processes=1,
            cleanup=ProcessCleanupConfig(
                total_timeout_seconds=1,
                term_grace_seconds=0.2,
                kill_grace_seconds=0.4,
                drain_grace_seconds=0.4,
            ),
        )
        self.argv, self.release = argv, release

    def _prepare_launch(self, spec):
        return PreparedLaunch(tuple(self.argv), {"TEST_ENV": "isolated"}, release=self.release)


@pytest.mark.asyncio
async def test_launch_spec_preserves_logical_command_and_releases_after_exit(tmp_path):
    released = []
    target = WrappedTarget(
        [sys.executable, "-S", "-c", "import os; print(os.environ.get('HOME','no-home'))"],
        lambda: released.append(True),
    )
    spec = ProcessSpec(argv=["logical-tool", "original-argument"], cwd=tmp_path)
    try:
        pid = await target.start_process(spec)
        spec.argv[:] = ["mutated"]
        result = await target.poll_process(pid, wait_seconds=5)
        assert result.status == ProcessStatus.COMPLETED
        assert result.argv == ["logical-tool", "original-argument"]
        assert result.stdout.strip() == "no-home"
        assert released == [True]
    finally:
        await target.aclose()


@pytest.mark.asyncio
async def test_spawn_failure_releases_unstarted_launch_resources(tmp_path):
    released = []
    target = WrappedTarget([str(tmp_path / "absent-executable")], lambda: released.append(True))
    try:
        with pytest.raises(FileNotFoundError):
            await target.start_process(ProcessSpec(argv=["logical"], cwd=tmp_path))
        assert released == [True] and not target.has_live_processes
    finally:
        await target.aclose()


@pytest.mark.asyncio
async def test_resource_cleanup_failure_retains_quota_and_is_recoverable(tmp_path):
    blocked = True

    def release():
        if blocked:
            raise OSError("synthetic resource failure")

    target = WrappedTarget([sys.executable, "-S", "-c", "pass"], release)
    records = []

    async def observer(spec, pid, record):
        records.append(record)

    target.cleanup_observer = observer
    try:
        pid = await target.start_process(ProcessSpec(argv=["logical"], cwd=tmp_path))
        result = await target.poll_process(pid, wait_seconds=5)
        assert result.status == ProcessStatus.CLEANUP_FAILED
        assert target.has_live_processes
        with pytest.raises(RuntimeError, match="上限"):
            await target.start_process(ProcessSpec(argv=["logical"], cwd=tmp_path))
        async with asyncio.timeout(2):
            while not records:  # noqa: ASYNC110 - cleanup observer handshake
                await asyncio.sleep(0.01)
        assert records[0]["spec"]["argv"] == ["logical"]
        blocked = False
        retried = await target.terminate_process(pid)
        assert retried.cleanup_status == "observed_empty" and not target.has_live_processes
    finally:
        blocked = False
        await target.aclose()
