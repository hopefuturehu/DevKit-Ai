"""Stdlib-only integrity checks for the explicitly installed Node/npm tree.

Installation seals downloaded, checksum-verified artifacts after npm ci. Discovery
only reads files; it never executes an installed candidate to establish trust.
OS enforcement and system executable/library trust are separate checks.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

PREFIXES = ("node", "node_modules")
MAX_ENTRIES = 30_000
MANIFEST_LIMIT = 16_000_000


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


def _permissions(path: Path, info: os.stat_result) -> None:
    if info.st_uid not in {0, os.getuid()} or (
        not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022
    ):
        raise ValueError(f"运行时路径所有者或写入权限不可信: {path}")


def _root(root: Path, workspace: Path) -> None:
    if (
        not root.is_absolute()
        or root.resolve(strict=True) != root
        or root.is_relative_to(workspace)
        or workspace.is_relative_to(root)
    ):
        raise ValueError("运行时必须位于工作区外的规范绝对路径")
    for path in (root, *root.parents):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"运行时父路径不是普通目录: {path}")
        if path != root and info.st_uid == 0 and info.st_mode & stat.S_ISVTX:
            continue
        _permissions(path, info)


@dataclass(frozen=True)
class TreeState:
    files: tuple[tuple[str, tuple[int, ...]], ...]
    directories: tuple[tuple[str, tuple[int, ...]], ...]
    links: tuple[tuple[str, str, tuple[int, ...]], ...]


def read_installed(root: Path, name: str, *, limit: int) -> bytes:
    """Read fixed installation metadata/assets without following aliases."""
    path = root / name
    if not path.is_relative_to(root) or path.resolve(strict=True) != path:
        raise ValueError(f"安装文件必须是规范路径: {name}")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
        raise ValueError(f"安装文件不是有界普通独立文件: {name}")
    _permissions(path, info)
    for parent in path.parents:
        _permissions(parent, parent.lstat())
        if parent == root:
            break
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(info):
            raise ValueError("安装文件身份已改变")
        raw = stream.read(limit + 1)
        if (
            len(raw) > limit
            or _stamp(os.fstat(stream.fileno())) != _stamp(info)
            or _stamp(path.lstat()) != _stamp(info)
        ):
            raise ValueError("读取期间安装文件已改变")
    return raw


def _scan(root: Path, workspace: Path) -> TreeState:
    _root(root, workspace)
    files, directories, links = [], [], []
    pending = [root / prefix for prefix in PREFIXES]
    while pending:
        directory = pending.pop()
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"运行时清单根必须是普通目录: {directory}")
        _permissions(directory, info)
        directories.append((str(directory.relative_to(root)), _stamp(info)))
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                info = entry.stat(follow_symlinks=False)
                _permissions(path, info)
                relative = str(path.relative_to(root))
                if stat.S_ISLNK(info.st_mode):
                    target = os.readlink(path)
                    try:
                        resolved = path.resolve(strict=True)
                    except RuntimeError as exc:
                        raise ValueError("运行时符号链接包含循环") from exc
                    if not any(resolved.is_relative_to(root / p) for p in PREFIXES):
                        raise ValueError(f"运行时符号链接逃出安装目录: {path}")
                    links.append((relative, target, _stamp(info)))
                elif stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    files.append((relative, _stamp(info)))
                else:
                    raise ValueError(f"运行时包含硬链接或非普通文件: {path}")
                if len(files) + len(directories) + len(links) + len(pending) > MAX_ENTRIES:
                    raise ValueError("运行时文件清单超过限制")
    return TreeState(tuple(sorted(files)), tuple(sorted(directories)), tuple(sorted(links)))


def _digest(path: Path, expected: tuple[int, ...]) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if _stamp(os.fstat(stream.fileno())) != expected:
            raise ValueError(f"运行时校验期间文件已变化: {path}")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if _stamp(os.fstat(stream.fileno())) != expected or _stamp(path.lstat()) != expected:
            raise ValueError(f"运行时校验期间文件已变化: {path}")
    return digest


def seal_runtime(root: Path, *, workspace: Path) -> str:
    """Explicit installer only; never called as a discovery/launch fallback."""
    before = _scan(root, workspace.resolve())
    record = {
        "schema": 1,
        "files": {name: _digest(root / name, stamp) for name, stamp in before.files},
        "directories": [name for name, _ in before.directories],
        "links": {name: target for name, target, _ in before.links},
        "modes": {
            name: stat.S_IMODE(stamp[4]) for name, stamp in (*before.files, *before.directories)
        },
    }
    if _scan(root, workspace.resolve()) != before:
        raise ValueError("生成清单期间运行时已变化")
    raw = (json.dumps(record, sort_keys=True, indent=2) + "\n").encode()
    if len(raw) > MANIFEST_LIMIT:
        raise ValueError("运行时文件清单超过大小限制")
    path = root / "runtime-files.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class RuntimeTree:
    root: Path
    manifest_sha256: str
    state: TreeState


def inspect_tree(
    root: Path,
    workspace: Path,
    *,
    manifest_sha256: str,
    previous: RuntimeTree | None = None,
) -> RuntimeTree:
    workspace = workspace.resolve()
    before = _scan(root, workspace)
    raw = read_installed(root, "runtime-files.json", limit=MANIFEST_LIMIT)
    if hashlib.sha256(raw).hexdigest() != manifest_sha256:
        raise ValueError("运行时文件清单校验失败")
    if previous is not None:
        if (
            previous.root != root
            or previous.manifest_sha256 != manifest_sha256
            or previous.state != before
        ):
            raise ValueError("运行时安装内容或文件身份已改变")
        return previous
    record = json.loads(raw)
    if (
        not isinstance(record, dict)
        or record.keys() != {"schema", "files", "directories", "links", "modes"}
        or type(record["schema"]) is not int
        or record["schema"] != 1
        or not isinstance(record["files"], dict)
        or set(record["files"]) != {name for name, _ in before.files}
        or record["directories"] != [name for name, _ in before.directories]
        or record["links"] != {name: target for name, target, _ in before.links}
        or record["modes"]
        != {name: stat.S_IMODE(stamp[4]) for name, stamp in (*before.files, *before.directories)}
    ):
        raise ValueError("运行时安装内容与清单不一致")
    for name, stamp in before.files:
        if _digest(root / name, stamp) != record["files"][name]:
            raise ValueError(f"运行时文件摘要不符: {name}")
    if _scan(root, workspace) != before:
        raise ValueError("校验期间运行时已变化")
    return RuntimeTree(root, manifest_sha256, before)
