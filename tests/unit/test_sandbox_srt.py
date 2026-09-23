import os
import tempfile
from pathlib import Path

import pytest

from bot.config.models import AppConfig
from bot.sandbox.models import SandboxError
from bot.sandbox.policy import RunDirectories, resolve_policy
from bot.sandbox.srt import compile_srt_config, launcher_environment, system_read_paths


@pytest.fixture
def policy(tmp_path, monkeypatch, request):
    work, runtime = tmp_path / "work", tmp_path / "runtime"
    directories = RunDirectories(tmp_path / "runs/first")
    temporary = tempfile.TemporaryDirectory(prefix="bot-srt-", dir="/tmp")
    request.addfinalizer(temporary.cleanup)
    control = Path(temporary.name).resolve()
    for path in (work, runtime, directories.home, directories.temp, directories.cache, control):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "host-home"))
    resolved = resolve_policy(
        AppConfig(), workspace=work, directories=directories, runtime_read_roots=(runtime,)
    )
    return resolved.policy, runtime, control


def test_srt_compilation_has_explicit_roots_and_cancels_implicit_shared_writes(policy):
    value, _, _ = policy
    settings = compile_srt_config(value)
    assert "/" in settings["filesystem"]["denyRead"]
    assert settings["filesystem"]["allowRead"] == list(map(str, value.read_roots))
    assert settings["filesystem"]["allowWrite"] == list(map(str, value.write_roots))
    for name in ("/tmp/claude", "/private/tmp/claude"):
        assert name in settings["filesystem"]["denyRead"]
        assert name in settings["filesystem"]["denyWrite"]
    assert settings["network"]["allowedDomains"] == []
    assert settings["network"]["allowAllUnixSockets"] is False
    assert settings["network"]["allowLocalBinding"] is False
    assert settings["enableWeakerNestedSandbox"] is False
    assert settings["enableWeakerNetworkIsolation"] is False


def test_linux_protects_symlink_directory_entry_without_blocking_project_sources(
    policy, monkeypatch
):
    value, _, control = policy
    monkeypatch.setattr("bot.sandbox.srt.platform.system", lambda: "Linux")
    alias = value.workspace / ".bot/config.toml"
    alias.parent.mkdir()
    target = control / "trusted.toml"
    target.touch()
    alias.symlink_to(target)
    settings = compile_srt_config(value)
    assert str(alias.parent) in settings["filesystem"]["denyWrite"]
    assert str(alias.parent) in settings["filesystem"]["denyRead"]
    assert str(alias) not in settings["filesystem"]["denyRead"]
    assert str(value.workspace) not in settings["filesystem"]["denyWrite"]


def test_linux_rejects_alias_guard_that_would_remove_all_workspace_writes(policy, monkeypatch):
    value, _, control = policy
    monkeypatch.setattr("bot.sandbox.srt.platform.system", lambda: "Linux")
    target = control / "secret"
    target.touch()
    alias = value.workspace / ".env"
    alias.symlink_to(target)
    with pytest.raises(SandboxError, match="整个写入根"):
        compile_srt_config(value)


def test_environment_does_not_inherit_keys_paths_loaders_or_proxy(policy, monkeypatch):
    value, runtime, control = policy
    for name in ("NODE_OPTIONS", "PYTHONPATH", "SSH_AUTH_SOCK", "FAKE_MODEL_KEY", "HTTPS_PROXY"):
        monkeypatch.setenv(name, "synthetic-secret")
    monkeypatch.setenv("PATH", str(value.workspace))
    env = launcher_environment(value, runtime_root=runtime, control_temp=control, extra={"CI": "1"})
    assert "synthetic-secret" not in str(env)
    assert env["HOME"] == str(value.home_dir)
    assert env["CLAUDE_CODE_TMPDIR"] == str(value.temp_dir)
    assert env["TMPDIR"] == str(control)
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["CI"] == "1"
    assert str(value.workspace) not in env["PATH"]


@pytest.mark.parametrize(
    "key", ["NODE_OPTIONS", "PYTHONPATH", "HOME", "PATH", "HTTPS_PROXY", "BASH_ENV"]
)
def test_extra_environment_cannot_supply_startup_authority(policy, key):
    value, runtime, control = policy
    with pytest.raises(SandboxError, match="环境变量名称"):
        launcher_environment(
            value, runtime_root=runtime, control_temp=control, extra={key: "unsafe"}
        )


def test_launcher_temp_cannot_overlap_task_access(policy):
    value, runtime, _ = policy
    with pytest.raises(SandboxError, match="不能被任务"):
        launcher_environment(value, runtime_root=runtime, control_temp=value.temp_dir)


def test_launcher_temp_socket_path_limit_is_checked_before_launch(policy, tmp_path):
    value, runtime, _ = policy
    control = tmp_path / ("long-control-" * 8)
    control.mkdir()
    with pytest.raises(SandboxError, match="临时目录过长"):
        launcher_environment(value, runtime_root=runtime, control_temp=control)


def test_system_paths_do_not_include_entire_usr_or_macos_system_volume():
    paths = system_read_paths()
    assert Path("/") not in paths
    assert Path("/usr") not in paths
    assert Path("/System") not in paths
    assert Path("/System/Volumes") not in paths
