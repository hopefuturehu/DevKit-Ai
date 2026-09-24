"""Read-only identities for the fixed OS executables used by the pinned adapter.

The explicit installer establishes digests from administrator-owned files. No
candidate executable is run during installation or inspection here. This does
not attest dynamic libraries, loader configuration, the OS, or kernel integrity.
The module stays stdlib-only so the standalone installer can load it directly.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
from dataclasses import dataclass
from pathlib import Path

MANIFEST_NAME = "host-executables.json"
MANIFEST_LIMIT = 100_000
FILE_LIMIT = 64_000_000
SYSTEM_OWNER = 0


def fixed_executables() -> dict[str, Path]:
    # Match launcher.mjs, bwrap-launcher.sh, and pinned SRT's whichSync/env
    # and Linux bridge paths. Never discover alternatives via inherited PATH.
    paths = {"sh": "/bin/sh", "bash": "/bin/bash", "env": "/usr/bin/env", "which": "/usr/bin/which"}
    if platform.system() == "Darwin":
        paths["sandbox-exec"] = "/usr/bin/sandbox-exec"
    elif platform.system() == "Linux":
        paths.update({name: f"/usr/bin/{name}" for name in ("bwrap", "socat", "rg", "true")})
    else:
        raise ValueError("系统启动程序仅支持 macOS/Linux")
    return {name: Path(path) for name, path in paths.items()}


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_gid,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _resolve(path: Path) -> Path:
    try:
        return path.resolve(strict=True)
    except RuntimeError as exc:
        raise ValueError("系统启动程序路径存在符号链接循环") from exc


def _ancestry(requested: Path, workspace: Path) -> tuple:
    """Include intermediate symlinks, their targets, and every parent identity."""
    pending = [requested]
    states = {}
    links = 0
    while pending:
        current = pending.pop()
        if not current.is_absolute():
            raise ValueError("系统启动程序必须使用绝对路径")
        resolved = _resolve(current)
        if resolved.is_relative_to(workspace):
            raise ValueError("系统启动程序不能来自工作区")
        for path in (current, *current.parents, resolved, *resolved.parents):
            if path in states:
                continue
            info = path.lstat()
            if info.st_uid not in {0, SYSTEM_OWNER}:
                raise ValueError(f"系统启动程序及父路径必须由管理员拥有: {path}")
            if stat.S_ISLNK(info.st_mode):
                links += 1
                if links > 40:
                    raise ValueError("系统启动程序符号链接过多")
                target = os.readlink(path)
                states[path] = (_stamp(info), target)
                pending.append(path.parent / target)
            elif stat.S_ISDIR(info.st_mode):
                if info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
                    raise ValueError(f"系统启动程序父目录可被非管理员写入: {path}")
                # Unrelated entries may change; replacement/owner/mode may not.
                states[path] = (_stamp(info)[:5], "")
            elif stat.S_ISREG(info.st_mode):
                if info.st_mode & 0o022 or info.st_nlink != 1:
                    raise ValueError(f"系统启动程序不能可写或带有硬链接: {path}")
                states[path] = (_stamp(info), "")
            else:
                raise ValueError(f"系统启动程序路径类型无效: {path}")
    return tuple((str(path), *value) for path, value in sorted(states.items()))


@dataclass(frozen=True)
class ExecutableState:
    requested: Path
    canonical: Path
    ancestry: tuple


def _inspect(
    path: Path, workspace: Path, *, hash_content: bool
) -> tuple[ExecutableState, str | None]:
    ancestry = _ancestry(path, workspace)
    canonical = _resolve(path)
    info = canonical.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or not info.st_mode & 0o111
        or not os.access(canonical, os.X_OK)
        or info.st_size > FILE_LIMIT
    ):
        raise ValueError(f"系统启动程序不是有界可执行普通文件: {path}")
    digest = None
    if hash_content:
        fd = os.open(canonical, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            if _stamp(os.fstat(stream.fileno())) != _stamp(info):
                raise ValueError("系统启动程序在读取前被替换")
            content = stream.read(FILE_LIMIT + 1)
            if len(content) > FILE_LIMIT or _stamp(os.fstat(stream.fileno())) != _stamp(info):
                raise ValueError("系统启动程序在读取期间改变")
            digest = hashlib.sha256(content).hexdigest()
    if _ancestry(path, workspace) != ancestry or _resolve(path) != canonical:
        raise ValueError("系统启动程序路径在检查期间改变")
    return ExecutableState(path, canonical, ancestry), digest


def seal_host_executables(root: Path, *, workspace: Path) -> str:
    entries = {}
    for name, path in sorted(fixed_executables().items()):
        state, digest = _inspect(path, workspace.resolve(), hash_content=True)
        entries[name] = {
            "requested": str(path),
            "canonical": str(state.canonical),
            "sha256": digest,
        }
    record = {
        "schema": 1,
        "system": platform.system(),
        "architecture": platform.machine().lower(),
        "executables": entries,
    }
    raw = (json.dumps(record, sort_keys=True, indent=2) + "\n").encode()
    path = root / MANIFEST_NAME
    with path.open("xb") as stream:
        stream.write(raw)
    path.chmod(0o600)
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class HostExecutables:
    manifest_sha256: str
    executables: tuple[tuple[str, ExecutableState], ...]

    @property
    def read_roots(self) -> tuple[Path, ...]:
        # Preserve intermediate aliases as readonly policy inputs too. A
        # canonical file deny alone cannot protect a writable link directory.
        paths = {state.canonical for _, state in self.executables}
        for _, state in self.executables:
            paths.add(state.requested)
            paths.update(Path(path) for path, _, target in state.ancestry if target)
        return tuple(sorted(paths))

    def verify(self, workspace: Path) -> None:
        expected = fixed_executables()
        if {name: state.requested for name, state in self.executables} != expected:
            raise ValueError("系统启动程序路径集合已改变")
        for _, previous in self.executables:
            current, _ = _inspect(previous.requested, workspace, hash_content=False)
            if current != previous:
                raise ValueError(f"系统启动程序身份已改变: {previous.requested}")


def inspect_host_executables(
    raw: bytes, *, manifest_sha256: str, workspace: Path, previous: HostExecutables | None = None
) -> HostExecutables:
    if len(raw) > MANIFEST_LIMIT or hashlib.sha256(raw).hexdigest() != manifest_sha256:
        raise ValueError("系统启动程序清单摘要不匹配")
    if previous is not None:
        if previous.manifest_sha256 != manifest_sha256:
            raise ValueError("系统启动程序安装清单已改变")
        previous.verify(workspace)
        return previous
    record = json.loads(raw)
    if (
        not isinstance(record, dict)
        or record.keys() != {"schema", "system", "architecture", "executables"}
        or type(record["schema"]) is not int
        or record["schema"] != 1
        or record["system"] != platform.system()
        or record["architecture"] != platform.machine().lower()
    ):
        raise ValueError("系统启动程序清单版本或平台不匹配")
    expected = fixed_executables()
    if (
        not isinstance(record["executables"], dict)
        or record["executables"].keys() != expected.keys()
    ):
        raise ValueError("系统启动程序清单集合无效")
    entries = []
    for name, path in sorted(expected.items()):
        entry = record["executables"][name]
        if not isinstance(entry, dict) or entry.keys() != {"requested", "canonical", "sha256"}:
            raise ValueError("系统启动程序清单条目无效")
        state, digest = _inspect(path, workspace, hash_content=True)
        if entry != {"requested": str(path), "canonical": str(state.canonical), "sha256": digest}:
            raise ValueError(f"系统启动程序内容或链接目标改变: {name}")
        entries.append((name, state))
    return HostExecutables(manifest_sha256, tuple(entries))
