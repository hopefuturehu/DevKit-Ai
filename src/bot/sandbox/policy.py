from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bot.sandbox.admission import WorkspaceAdmission, inspect_roots
from bot.sandbox.config import (
    SandboxConfig,
    assert_at_most,
    assert_permissions_at_most,
    canonical_root,
)
from bot.sandbox.models import SandboxError, SandboxErrorCode, SandboxPolicy

if TYPE_CHECKING:
    from bot.config.models import AppConfig


def validate_security_context(config: AppConfig) -> SandboxConfig:
    """Recheck derived config before selecting a backend or resolving a scope.

    Pydantic's model_copy(update=...) does not validate updates. The immutable
    trust snapshot survives copies so a child cannot turn required into off.
    Direct AppConfig construction is a trusted host API, not a tool input.
    """
    try:
        # Re-validate permission values as well: model_copy skips nested
        # validation and must not turn e.g. the string 'false' into authority.
        from bot.config.models import PermissionsConfig

        effective = SandboxConfig.model_validate(config.sandbox.model_dump(warnings=False))
        permissions = PermissionsConfig.model_validate(
            config.permissions.model_dump(warnings=False), strict=True
        )
        trust = config._sandbox_trust
        if trust is not None and trust.require_sandbox and effective.mode != "required":
            raise ValueError("派生配置不能关闭本次加载已要求的 required 沙箱")
        if trust is not None and (trust.sandbox.mode == "required" or effective.mode == "required"):
            assert_at_most(effective, trust.sandbox)
            assert_permissions_at_most(permissions, trust.permissions)
        return effective
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise SandboxError(SandboxErrorCode.CONFIG_INVALID, str(exc)) from exc


@dataclass(frozen=True)
class RunDirectories:
    root: Path

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def temp(self) -> Path:
        return self.root / "tmp"

    @property
    def cache(self) -> Path:
        return self.root / "cache"


@dataclass(frozen=True)
class ResolvedSandbox:
    policy: SandboxPolicy
    admission: WorkspaceAdmission


def _paths(values) -> tuple[Path, ...]:
    return tuple(sorted({canonical_root(value) for value in values}))


def _denials(values) -> tuple[Path, ...]:
    # Protect both the configured name and its current target. Keeping only
    # resolve() would leave the configured name unprotected after replacement.
    paths = set()
    for value in values:
        paths.add(canonical_root(value))
        paths.add(Path(os.path.abspath(Path(value).expanduser())))
    return tuple(sorted(paths))


def _inside(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _configured_path(value: str, workspace: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else workspace / path


def resolve_policy(
    config: AppConfig,
    *,
    workspace: Path,
    directories: RunDirectories,
    runtime_read_roots: tuple[Path, ...],
    parent: SandboxPolicy | None = None,
    read_only: bool = False,
    protected_paths: tuple[Path, ...] = (),
    git_metadata_roots: tuple[Path, ...] = (),
) -> ResolvedSandbox:
    """Resolve a host-selected workspace. No model-supplied policy is accepted.

    A normal child can only narrow its parent's workspace. Worktree delegation
    requires a separate broker grant and is deliberately not accepted here yet.
    """
    sandbox = validate_security_context(config)
    try:
        workspace = canonical_root(workspace)
        private = canonical_root(directories.root)
        if not workspace.is_dir() or not private.is_dir():
            raise ValueError("workspace 和任务私有目录必须已由宿主创建")
        if private.parent.is_relative_to(workspace) or workspace.is_relative_to(private.parent):
            raise ValueError("任务私有目录区域必须位于工作区外，且不能包含工作区")
        if directories.root != private:
            raise ValueError("任务私有目录必须使用规范路径")
        owned = (directories.home, directories.temp, directories.cache)
        if any(not path.is_dir() or path.resolve() != path for path in owned):
            raise ValueError("任务 home/tmp/cache 必须为私有根目录的真实子目录")
        runtime_roots = _paths(runtime_read_roots)
        if not runtime_roots or any(not root.exists() for root in runtime_roots):
            raise ValueError("必须提供已安装的可信运行时路径")
        if any(
            workspace.is_relative_to(root) or root.is_relative_to(workspace)
            for root in runtime_roots
        ):
            raise ValueError("工作区不能与可信运行时目录重叠")
        if any(
            private.parent.is_relative_to(root) or root.is_relative_to(private.parent)
            for root in runtime_roots
        ):
            raise ValueError("可信运行时不能与任务私有目录区域重叠")
        if any(
            private.parent.is_relative_to(root) or root.is_relative_to(private.parent)
            for root in sandbox.write_roots
        ):
            raise ValueError("额外写目录不能覆盖或进入任务私有目录区域")
        for root in (*sandbox.read_roots, *sandbox.write_roots):
            if not root.is_dir():
                raise ValueError(f"额外目录不存在: {root}")
        state = config.state_path(workspace)
        worktrees_path = workspace / config.subagents.worktree_dir
        worktrees = worktrees_path.resolve()
        if not worktrees.is_relative_to(workspace):
            raise ValueError("受管 worktree 根目录逃逸工作区")
        if worktrees == workspace:
            raise ValueError("受管 worktree 根目录不能等于工作区")
        git = workspace / ".git"
        # The worktree broker must resolve external gitdir/commondir using its
        # own confined, bounded protocol. Do not follow repository-controlled
        # metadata to read arbitrary host files in this trusted resolver.
        if git.is_file() and not git_metadata_roots:
            raise ValueError("工作树 .git 文件需要可信管理作用域提供 Git 元数据目录")
        metadata = _paths(git_metadata_roots)
        if any(not root.is_dir() for root in metadata):
            raise ValueError("Git 元数据目录不存在")
        git_roots = [git, *metadata]
        trust = config._sandbox_trust
        controls = [
            state,
            *(Path(str(state) + suffix) for suffix in ("-wal", "-shm", "-journal")),
            _configured_path(config.storage.state_path, workspace),
            workspace / ".bot/config.toml",
            config.memory_path(workspace),
            _configured_path(config.memory.path, workspace),
            config.project_agent_path(workspace),
            workspace / config.agents.project_path,
            config.user_agent_path(),
            *protected_paths,
        ]
        if trust:
            controls.extend(
                [
                    trust.trusted_config,
                    *trust.source_paths,
                    *([trust.project_config] if trust.project_config else []),
                ]
            )
        secrets = [
            workspace / ".env",
            *(Path.home() / name for name in (".ssh", ".aws", ".azure", ".kube", ".config/gcloud")),
        ]
        protected = _denials([*controls, *secrets])
        denied_read = _denials([*protected, worktrees_path, private.parent])
        denied_write = _denials([*protected, worktrees_path, *runtime_roots, *git_roots])
        if _inside(workspace, protected):
            raise ValueError("工作区位于受保护路径内")
        mutable_read = _paths([workspace, *sandbox.read_roots, *sandbox.write_roots])
        writable = _paths([workspace, *sandbox.write_roots])
        network = sandbox.network.mode if config.permissions.network != "deny" else "deny"
        domains = sandbox.network.allowed_domains if network == "allowlist" else ()
        readonly = read_only or config.permissions.mode == "read-only"
        if parent is not None:
            if not workspace.is_relative_to(parent.workspace) or _inside(
                workspace, parent.deny_read
            ):
                raise ValueError("子任务工作区超出父策略；worktree 必须使用专用委派")
            if not all(_inside(root, parent.read_roots) for root in mutable_read):
                raise ValueError("子任务不能增加读取目录")
            writable = tuple(root for root in writable if _inside(root, parent.write_roots))
            readonly |= not _inside(workspace, parent.write_roots)
            if not all(_inside(root, parent.read_roots) for root in (*runtime_roots, *metadata)):
                raise ValueError("子任务不能增加运行时或 Git 元数据读取目录")
            denied_read = _denials([*denied_read, *parent.deny_read])
            denied_write = _denials([*denied_write, *parent.deny_write])
            protected = _denials([*protected, *parent.protected_paths])
            if parent.network_mode == "deny":
                network, domains = "deny", ()
            else:
                domains = tuple(
                    d
                    for d in domains
                    if d in parent.allowed_domains or d.partition(":")[0] in parent.allowed_domains
                )
                if not domains:
                    network = "deny"
        if any(_inside(root, denied_read) for root in (*mutable_read, *runtime_roots, *metadata)):
            raise ValueError("读取根目录不能重新开放受保护区域")
        admission = inspect_roots(mutable_read, excluded=denied_read)
        policy = SandboxPolicy(
            workspace=workspace,
            read_roots=_paths([*mutable_read, *runtime_roots, *metadata, *owned]),
            write_roots=_paths([*(() if readonly else writable), *owned]),
            deny_read=_denials([*denied_read, *admission.hardlink_denies]),
            deny_write=_denials([*denied_write, *admission.hardlink_denies]),
            protected_paths=protected,
            network_mode=network,
            allowed_domains=domains,
            environment_names=("CI", "TERM", "LANG", "LC_ALL"),
            home_dir=directories.home,
            temp_dir=directories.temp,
            cache_dir=directories.cache,
        )
        return ResolvedSandbox(policy, admission)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SandboxError(SandboxErrorCode.CONFIG_INVALID, str(exc)) from exc
