from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from bot.sandbox.models import SandboxError, SandboxErrorCode


@dataclass(frozen=True)
class PathStamp:
    path: Path
    device: int
    inode: int
    mode: int
    links: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, path: Path, info: os.stat_result) -> PathStamp:
        return cls(
            path,
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_nlink,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )


@dataclass(frozen=True)
class WorkspaceAdmission:
    stamps: tuple[PathStamp, ...]
    hardlink_denies: tuple[Path, ...]

    def verify(self) -> None:
        """Check again immediately before launch; never repair a stale policy in place.

        This is an admission check, not an OS boundary. The resulting aliases
        must also be compiled as OS read/write denies. Other writers controlled
        by the host must honor the same admission/launch coordination.
        """
        try:
            for before in self.stamps:
                if PathStamp.from_stat(before.path, before.path.lstat()) != before:
                    raise ValueError(f"策略生成期间路径发生变化: {before.path}")
        except (OSError, ValueError) as exc:
            raise SandboxError(SandboxErrorCode.CONFIG_INVALID, str(exc)) from exc


def inspect_roots(
    roots: tuple[Path, ...],
    *,
    excluded: tuple[Path, ...] = (),
    max_entries: int = 250_000,
    timeout_seconds: float = 15,
) -> WorkspaceAdmission:
    """Inspect metadata without following directory symlinks or opening file data.

    Both linked aliases and directory/file identities are retained. A scan that
    is unreadable, changing, or too large is a refusal, never an empty allowlist.
    """
    deadline = monotonic() + timeout_seconds
    stamps: dict[Path, PathStamp] = {}
    aliases: set[Path] = set()

    def ignored(path: Path) -> bool:
        return any(path == root or path.is_relative_to(root) for root in excluded)

    def add(path: Path, info: os.stat_result) -> None:
        if monotonic() > deadline or (path not in stamps and len(stamps) >= max_entries):
            raise ValueError("sandbox 路径检查超过限制；目标未启动")
        stamp = PathStamp.from_stat(path, info)
        if path in stamps and stamp != stamps[path]:
            raise ValueError(f"准入扫描期间路径发生变化: {path}")
        stamps[path] = stamp
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            if any(c in str(path) for c in "*?[]{}"):
                raise ValueError(f"无法安全表达硬链接别名的拒绝规则: {path}")
            aliases.add(path)

    def failure(exc: OSError) -> None:
        raise exc

    try:
        for root in roots:
            if ignored(root):
                continue
            info = root.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"准入根目录不能为符号链接: {root}")
            add(root, info)
            if not stat.S_ISDIR(info.st_mode):
                continue
            for directory, dirs, files, fd in os.fwalk(
                root, follow_symlinks=False, onerror=failure
            ):
                base = Path(directory)
                # Compare the descriptor to the pathname as well as keeping a
                # later stamp, so a replaced ancestor is not silently scanned.
                opened = os.fstat(fd)
                named = base.lstat()
                if (named.st_dev, named.st_ino, named.st_mode) != (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_mode,
                ):
                    raise ValueError(f"准入扫描期间目录被替换: {base}")
                add(base, opened)
                dirs[:] = [name for name in dirs if not ignored(base / name)]
                for name in [*dirs, *files]:
                    path = base / name
                    if not ignored(path):
                        add(path, os.stat(name, dir_fd=fd, follow_symlinks=False))
    except (OSError, ValueError) as exc:
        raise SandboxError(SandboxErrorCode.CONFIG_INVALID, str(exc)) from exc
    admission = WorkspaceAdmission(tuple(stamps.values()), tuple(sorted(aliases)))
    admission.verify()
    return admission
