"""Compile explicit SRT policy and a minimal launcher environment.

This module does not select a backend, start processes or grant capabilities.
The native target must own resource creation, admission and process cleanup.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

from bot.sandbox.config import canonical_domain, canonical_root
from bot.sandbox.models import SandboxError, SandboxErrorCode, SandboxPolicy

_SAFE_ENVIRONMENT = frozenset({"CI", "TERM", "LANG", "LC_ALL"})
_IMPLICIT_PATH_DENIES = (
    "/tmp/claude",
    "/private/tmp/claude",
    "/dev/dtracehelper",
    "/dev/autofs_nowait",
)


def _linux_symlink_parent_guards(policy: SandboxPolicy) -> set[str]:
    """Bind-mounting a symlink protects its target, not its directory entry.

    Hide writable parents of protected symlinks as well. A narrow
    broker/worktree grant does not need such a guard for an ancestor it cannot
    write in the first place. Never resolve away the configured deny spelling
    before checking its components.
    """
    guards: set[str] = set()
    for denied in policy.deny_write:
        for component in (denied, *denied.parents):
            if not component.is_symlink():
                continue
            parent = component.parent.resolve()
            if any(parent.is_relative_to(root) for root in policy.write_roots):
                if parent in policy.write_roots:
                    raise ValueError(
                        f"保护符号链接需要隐藏整个写入根；请把控制文件放入独立目录: {component}"
                    )
                if any(root.is_relative_to(parent) for root in policy.read_roots):
                    raise ValueError(f"受保护符号链接的父目录不能另设读取授权: {parent}")
                guards.add(str(parent))
    return guards


def system_read_paths() -> tuple[Path, ...]:
    """OS libraries and required devices; never grant macOS /System/Volumes."""
    names = [
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
        "/usr/lib",
        "/usr/share/locale",
        "/usr/share/zoneinfo",
        "/dev/null",
        "/dev/zero",
        "/dev/random",
        "/dev/urandom",
        "/dev/tty",
    ]
    if platform.system() == "Darwin":
        names += [
            "/dev/fd",
            "/System/Library",
            "/Library/Apple/System/Library",
            "/private/etc/ssl",
            "/private/etc/hosts",
            "/private/etc/resolv.conf",
        ]
    elif platform.system() == "Linux":
        names += [
            "/lib",
            "/lib64",
            "/proc",
            "/etc/ld.so.cache",
            "/etc/ssl",
            "/etc/hosts",
            "/etc/resolv.conf",
            "/etc/nsswitch.conf",
            "/etc/passwd",
            "/etc/group",
        ]
    else:
        raise SandboxError(SandboxErrorCode.UNAVAILABLE, "原生后端仅支持 macOS/Linux")
    return tuple(sorted({Path(name).resolve() for name in names if Path(name).exists()}))


def compile_srt_config(policy: SandboxPolicy) -> dict:
    try:
        policy = SandboxPolicy.model_validate(policy.model_dump(warnings=False))
        for path in (
            *policy.read_roots,
            *policy.write_roots,
            *policy.deny_read,
            *policy.deny_write,
        ):
            canonical_root(path)  # Reject glob/control characters; preserve deny aliases below.
        domains = tuple(canonical_domain(value) for value in policy.allowed_domains)
        if policy.network_mode == "allowlist" and not domains:
            raise ValueError("白名单模式必须指定域名")
        for root in policy.write_roots:
            if any(
                root == Path(name) or root.is_relative_to(Path(name))
                for name in _IMPLICIT_PATH_DENIES
            ):
                raise ValueError("任务写目录不能位于 SRT 默认共享目录内")
        alias_guards = (
            _linux_symlink_parent_guards(policy) if platform.system() == "Linux" else set()
        )
    except (OSError, ValueError) as exc:
        raise SandboxError(SandboxErrorCode.CONFIG_INVALID, str(exc)) from exc

    def guarded_denials(paths) -> list[str]:
        # A hidden control directory subsumes its descendants. Keeping missing
        # child mounts would make bwrap try to create them inside the read-only
        # parent. No read carve-out under such a parent is accepted above.
        return sorted(
            {
                *alias_guards,
                *(
                    str(path)
                    for path in paths
                    if not any(
                        Path(path) != Path(guard) and Path(path).is_relative_to(guard)
                        for guard in alias_guards
                    )
                ),
            }
        )

    return {
        "filesystem": {
            "denyRead": guarded_denials(("/", *policy.deny_read, *_IMPLICIT_PATH_DENIES)),
            "allowRead": list(map(str, policy.read_roots)),
            "allowWrite": list(map(str, policy.write_roots)),
            "denyWrite": guarded_denials((*policy.deny_write, *_IMPLICIT_PATH_DENIES)),
        },
        "network": {
            "allowedDomains": list(domains),
            "deniedDomains": [],
            "allowUnixSockets": [],
            "allowAllUnixSockets": False,
            "allowLocalBinding": False,
            # Pinned SRT already rejects loopback/link-local/metadata. Private
            # networks are legitimate upstream defaults, but never implicit here.
            "deniedResolvedAddresses": [
                "10.0.0.0/8",
                "172.16.0.0/12",
                "192.168.0.0/16",
                "100.64.0.0/10",
                "fc00::/7",
            ],
        },
        "enableWeakerNestedSandbox": False,
        "enableWeakerNetworkIsolation": False,
        "allowPty": False,
        "allowGitConfig": False,
        "allowAppleEvents": False,
    }


def launcher_environment(
    policy: SandboxPolicy,
    *,
    runtime_root: Path,
    control_temp: Path,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """SRT's temp files are separate from the task's HOME/tmp/cache.

    CLAUDE_CODE_TMPDIR makes SRT pass the task tmp to the inner command; TMPDIR
    here belongs to the launcher and must remain outside every task read root.
    This is a full environment, not an overlay on os.environ.
    """
    extra = extra or {}
    try:
        runtime_root = canonical_root(runtime_root)
        control_temp = canonical_root(control_temp)
        if runtime_root not in policy.read_roots or runtime_root not in policy.deny_write:
            raise ValueError("运行时必须只读授权")
        if any(
            control_temp.is_relative_to(root) for root in (*policy.read_roots, *policy.write_roots)
        ):
            raise ValueError("启动器临时目录不能被任务读取或写入")
        if not control_temp.is_dir():
            raise ValueError("启动器临时目录必须由宿主提前创建")
        # SRT creates Unix sockets beneath TMPDIR. Reserve space for its
        # generated filenames under macOS's 104-byte sockaddr_un limit.
        if len(os.fsencode(control_temp)) > 64:
            raise ValueError("启动器临时目录过长；请由宿主分配短路径控制目录")
        allowed = _SAFE_ENVIRONMENT.intersection(policy.environment_names)
        if any(key not in allowed for key in extra):
            raise ValueError("命令 env 含有未授权的环境变量名称")
        if any(not isinstance(value, str) or "\0" in value for value in extra.values()):
            raise ValueError("命令 env 必须为不含 NUL 的字符串")
    except (OSError, ValueError) as exc:
        raise SandboxError(SandboxErrorCode.CONFIG_INVALID, str(exc)) from exc
    return {
        "PATH": f"{runtime_root / 'node/bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(policy.home_dir),
        "TMPDIR": str(control_temp),
        "CLAUDE_CODE_TMPDIR": str(policy.temp_dir),
        "XDG_CACHE_HOME": str(policy.cache_dir),
        "XDG_CONFIG_HOME": str(policy.home_dir / ".config"),
        "XDG_DATA_HOME": str(policy.home_dir / ".local/share"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        **extra,
    }
