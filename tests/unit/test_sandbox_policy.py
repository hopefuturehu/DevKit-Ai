import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from bot.config.models import AppConfig
from bot.sandbox.models import ExecutionScope, SandboxError
from bot.sandbox.policy import RunDirectories, resolve_policy


@pytest.fixture
def layout(tmp_path, monkeypatch):
    base = tmp_path.resolve()
    home, work, runtime = base / "user", base / "work", base / "runtime"
    for path in (home, work, runtime):
        path.mkdir()
    monkeypatch.setenv("HOME", str(home))
    config = AppConfig.model_validate(
        {
            "sandbox": {"mode": "required"},
            "storage": {"state_path": ".bot/state.db"},
        }
    )

    def directories(name):
        dirs = RunDirectories(base / "private/runs" / name)
        for path in (dirs.home, dirs.temp, dirs.cache):
            path.mkdir(parents=True, mode=0o700)
        return dirs

    first = directories("first")

    def resolve(**kwargs):
        return resolve_policy(
            **{
                "config": config,
                "workspace": work,
                "directories": first,
                "runtime_read_roots": (runtime,),
                **kwargs,
            }
        )

    return config, work, runtime, directories, resolve


def test_policy_protects_control_paths_secrets_runtime_git_and_sibling_runs(layout):
    _, work, runtime, _, resolve = layout
    (work / ".git").mkdir()
    result = resolve()
    policy = result.policy
    for path in (
        work / ".bot/config.toml",
        work / ".bot/state.db",
        work / ".bot/state.db-wal",
        work / ".bot/state.db-shm",
        work / ".bot/state.db-journal",
        work / ".bot/memory",
        work / ".env",
        Path.home() / ".ssh",
        Path.home() / ".aws",
    ):
        assert path in policy.deny_read and path in policy.deny_write
    assert work / ".git" in policy.deny_write
    assert work / ".git" not in policy.deny_read
    assert runtime in policy.read_roots and runtime in policy.deny_write
    assert work in policy.read_roots and work in policy.write_roots
    assert policy.home_dir.parent.parent in policy.deny_read
    assert set(policy.environment_names) == {"CI", "TERM", "LANG", "LC_ALL"}


def test_control_symlink_name_and_target_are_both_protected(layout, tmp_path):
    config, work, _, _, resolve = layout
    (work / ".bot").mkdir()
    target = tmp_path / "state.db"
    target.touch()
    alias = work / ".bot/state.db"
    alias.symlink_to(target)
    policy = resolve().policy
    assert alias in policy.deny_read and alias in policy.deny_write
    assert target in policy.deny_read and target in policy.deny_write
    assert Path(str(target) + "-wal") in policy.deny_read
    assert config.state_path(work) == target


def test_existing_hardlink_alias_is_in_both_os_deny_sets(layout, tmp_path):
    _, work, _, _, resolve = layout
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("synthetic secret")
    alias = work / "innocent.txt"
    os.link(sentinel, alias)
    result = resolve()
    assert alias in result.policy.deny_read
    assert alias in result.policy.deny_write
    assert result.admission.hardlink_denies == (alias,)


@pytest.mark.parametrize("permission_mode,read_only", [("read-only", False), ("safe", True)])
def test_readonly_has_only_own_private_write_roots(layout, permission_mode, read_only):
    config, _, _, _, resolve = layout
    config.permissions.mode = permission_mode
    policy = resolve(read_only=read_only).policy
    assert set(policy.write_roots) == {policy.home_dir, policy.temp_dir, policy.cache_dir}


def test_child_policy_retains_parent_read_denies_and_cannot_gain_writes(layout):
    _, work, _, directories, resolve = layout
    child_work = work / "child"
    child_work.mkdir()
    secret = child_work / "keep-private"
    parent = resolve(read_only=True, protected_paths=(secret,)).policy
    child = resolve(workspace=child_work, directories=directories("child"), parent=parent).policy
    assert child_work not in child.write_roots
    assert secret in child.deny_read and secret in child.deny_write
    assert set(parent.protected_paths) <= set(child.protected_paths)
    assert parent.home_dir not in child.read_roots


def test_child_cannot_enter_sibling_or_managed_worktree(layout):
    config, work, _, directories, resolve = layout
    managed = work / config.subagents.worktree_dir
    managed.mkdir(parents=True)
    target = managed / "task"
    target.mkdir()
    parent = resolve().policy
    with pytest.raises(SandboxError, match="超出父策略"):
        resolve(workspace=target, directories=directories("child"), parent=parent)


def test_child_cannot_add_runtime_read_root(layout, tmp_path):
    _, _, _, directories, resolve = layout
    parent = resolve().policy
    extra = tmp_path / "another-runtime"
    extra.mkdir()
    with pytest.raises(SandboxError, match="不能增加运行时"):
        resolve(runtime_read_roots=(extra,), directories=directories("child"), parent=parent)


def test_parent_network_ceiling_and_permissions_intersect(layout):
    config, _, _, directories, resolve = layout
    config = config.model_copy(
        update={
            "sandbox": config.sandbox.model_validate(
                {
                    "mode": "required",
                    "network": {"mode": "allowlist", "allowed_domains": ["example.com:443"]},
                }
            )
        }
    )
    parent = resolve(config=config).policy
    assert parent.allowed_domains == ("example.com:443",)
    wider = config.model_copy(
        update={
            "sandbox": config.sandbox.model_validate(
                {
                    "mode": "required",
                    "network": {
                        "mode": "allowlist",
                        "allowed_domains": ["example.com:443", "pypi.org"],
                    },
                }
            )
        }
    )
    child = resolve(config=wider, parent=parent, directories=directories("child")).policy
    assert child.allowed_domains == ("example.com:443",)
    config.permissions.network = "deny"
    assert resolve(config=config).policy.network_mode == "deny"


def test_policies_and_scopes_are_immutable_and_concurrent_runs_do_not_share_state(layout):
    _, _, _, directories, resolve = layout
    dirs = [directories(f"run-{index}") for index in range(4)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        policies = list(executor.map(lambda value: resolve(directories=value).policy, dirs))
    assert len({p.policy_id for p in policies}) == 4
    for policy, private in zip(policies, dirs, strict=True):
        assert policy.home_dir == private.home
        with pytest.raises(ValidationError):
            policy.write_roots = (Path("/"),)
        scope = ExecutionScope(
            session_id="session",
            run_id=private.root.name,
            tool_call_id="call",
            workspace=policy.workspace,
            policy_id=policy.policy_id,
        )
        with pytest.raises(ValidationError):
            scope.run_id = "other"
        assert policy.model_copy().policy_id == policy.policy_id


@pytest.mark.parametrize("overlap", ["workspace", "private", "runtime"])
def test_workspace_runtime_and_private_areas_must_be_disjoint(layout, overlap):
    _, work, runtime, directories, resolve = layout
    if overlap == "workspace":
        with pytest.raises(SandboxError, match="工作区不能与可信运行时"):
            resolve(runtime_read_roots=(work,))
    elif overlap == "private":
        dirs = directories("child")
        with pytest.raises(SandboxError, match="私有目录区域"):
            resolve(workspace=dirs.root.parent)
    else:
        with pytest.raises(SandboxError, match="任务私有目录区域"):
            resolve(runtime_read_roots=(directories("child").root.parent, runtime))


def test_private_home_cannot_be_a_symlink_to_workspace(layout):
    _, work, _, directories, resolve = layout
    dirs = directories("linked")
    dirs.home.rmdir()
    dirs.home.symlink_to(work, target_is_directory=True)
    with pytest.raises(SandboxError, match="真实子目录"):
        resolve(directories=dirs)


def test_private_home_cannot_reopen_another_private_control_directory(layout):
    _, _, _, directories, resolve = layout
    dirs = directories("linked")
    control = dirs.root / "control"
    control.mkdir()
    dirs.home.rmdir()
    dirs.home.symlink_to(control, target_is_directory=True)
    with pytest.raises(SandboxError, match="真实子目录"):
        resolve(directories=dirs)


def test_read_root_cannot_reopen_protected_secret(layout):
    config, _, _, _, resolve = layout
    ssh = Path.home() / ".ssh"
    ssh.mkdir()
    config.sandbox = config.sandbox.model_copy(update={"read_roots": (ssh,)})
    with pytest.raises(SandboxError, match="重新开放受保护"):
        resolve()


def test_configured_write_root_cannot_reach_private_runs(layout):
    config, _, _, directories, resolve = layout
    region = directories("another").root.parent
    config.sandbox = config.sandbox.model_copy(update={"write_roots": (region,)})
    with pytest.raises(SandboxError, match="额外写目录"):
        resolve()


def test_git_indirection_is_not_read_by_trusted_resolver(layout, monkeypatch):
    _, work, _, _, resolve = layout
    (work / ".git").write_text("gitdir: /untrusted-external-path\n")

    def unexpected(*a, **kw):
        pytest.fail("resolver must not read repository-controlled metadata on host")

    monkeypatch.setattr(Path, "read_text", unexpected)
    with pytest.raises(SandboxError, match="可信管理作用域"):
        resolve()


def test_host_issued_git_metadata_is_readonly(layout, tmp_path):
    _, work, _, _, resolve = layout
    external = tmp_path / "git-common"
    external.mkdir()
    (work / ".git").write_text(f"gitdir: {external}\n")
    policy = resolve(git_metadata_roots=(external,)).policy
    assert external in policy.read_roots and external in policy.deny_write
    assert external not in policy.write_roots
