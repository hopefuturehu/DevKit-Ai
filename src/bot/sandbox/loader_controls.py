"""Fixed OS loader controls, inspected without running candidate programs.

This guard detects changed loader configuration and keeps tasks from writing
its paths, including absent ones. Shared-cache payloads are identity checked,
not cryptographically attested. Transitive library resolution is a separate
release gate; this module must not claim complete dynamic-loader integrity.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
from dataclasses import dataclass
from pathlib import Path

from .host_runtime import _ancestry, _resolve, _stamp

MANIFEST_NAME = "loader-controls.json"
MANIFEST_LIMIT = 1_000_000
CONTENT_LIMIT = 32_000_000
ENTRY_LIMIT = 256


def fixed_controls() -> dict[str, tuple[Path, str]]:
    if platform.system() == "Linux":
        return {
            "cache": (Path("/etc/ld.so.cache"), "file"),
            "preload": (Path("/etc/ld.so.preload"), "absent"),
            "config": (Path("/etc/ld.so.conf"), "file"),
            "config_directory": (Path("/etc/ld.so.conf.d"), "config_directory"),
        }
    if platform.system() == "Darwin":
        return {
            "dyld": (Path("/usr/lib/dyld"), "file"),
            "cache": (Path("/System/Library/dyld"), "identity_directory"),
            "cryptex_cache": (
                Path("/System/Volumes/Preboot/Cryptexes/OS/System/Library/dyld"),
                "identity_directory",
            ),
            "legacy_cache": (Path("/private/var/db/dyld"), "identity_directory"),
        }
    raise ValueError("加载器控制路径仅支持 macOS/Linux")


def _content(path: Path) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > CONTENT_LIMIT:
        raise ValueError(f"加载器配置不是有界独立普通文件: {path}")
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(info):
            raise ValueError("加载器配置读取前被替换")
        raw = stream.read(CONTENT_LIMIT + 1)
        if (
            len(raw) > CONTENT_LIMIT
            or _stamp(os.fstat(stream.fileno())) != _stamp(info)
            or _stamp(path.lstat()) != _stamp(info)
        ):
            raise ValueError("加载器配置读取期间改变")
    return raw


@dataclass(frozen=True)
class ControlState:
    path: Path
    kind: str
    exists: bool
    ancestry: tuple
    # Include each directory entry and its aliases; directory replacement and
    # added/removed cache or config files must invalidate a cached snapshot.
    entries: tuple
    content_sha256: tuple[tuple[str, str], ...]

    @property
    def protected_paths(self) -> tuple[Path, ...]:
        paths = {self.path, self.path.resolve()}
        for ancestry in (self.ancestry, *(entry[1] for entry in self.entries)):
            paths.update(
                Path(path) for path, stamp, target in ancestry if target or stat.S_ISREG(stamp[4])
            )
        return tuple(sorted(paths))


def _capture(path: Path, kind: str, workspace: Path, *, hash_content: bool) -> ControlState:
    # lexists distinguishes a missing path from a dangling symlink, which is
    # rejected by strict resolution rather than accepted as "not installed".
    exists = os.path.lexists(path)
    if kind == "absent" and exists:
        raise ValueError(f"原生后端不支持系统预加载配置: {path}")
    if not exists:
        parent = path.parent
        while not os.path.lexists(parent):
            parent = parent.parent
        ancestry = _ancestry(parent, workspace)
        if not parent.is_dir():
            raise ValueError("缺失加载器控制路径的父路径不是目录")
        return ControlState(path, kind, False, ancestry, (), ())
    ancestry = _ancestry(path, workspace)
    canonical = _resolve(path)
    entries = []
    hashes = []
    if kind == "file":
        if not canonical.is_file():
            raise ValueError("加载器配置路径不是普通文件")
        if hash_content:
            hashes.append((str(canonical), hashlib.sha256(_content(canonical)).hexdigest()))
    elif kind in {"config_directory", "identity_directory"}:
        if not canonical.is_dir():
            raise ValueError("加载器缓存/配置路径不是目录")
        # Collect bounded names before sorting rather than materializing an
        # arbitrarily large directory. No recursive or glob-based traversal.
        children = []
        with os.scandir(canonical) as iterator:
            for entry in iterator:
                if len(children) >= ENTRY_LIMIT:
                    raise ValueError("加载器目录条目超过限制")
                children.append(Path(entry.path))
        for child in sorted(children):
            child_ancestry = _ancestry(child, workspace)
            real = _resolve(child)
            if not real.is_file():
                raise ValueError("加载器目录含有非普通文件条目")
            if kind == "config_directory" and hash_content:
                hashes.append((str(real), hashlib.sha256(_content(real)).hexdigest()))
            if _ancestry(child, workspace) != child_ancestry:
                raise ValueError("加载器目录条目检查期间改变")
            entries.append((str(child), child_ancestry))
        # Directory mtimes matter here: additions/removals are loader inputs.
        entries.append((str(canonical), ((str(canonical), _stamp(canonical.stat()), ""),)))
    else:
        raise ValueError("未知加载器配置类型")
    if _ancestry(path, workspace) != ancestry:
        raise ValueError("加载器控制路径检查期间改变")
    return ControlState(path, kind, True, ancestry, tuple(entries), tuple(sorted(hashes)))


def _capture_all(workspace: Path, *, hash_content: bool) -> tuple[tuple[str, ControlState], ...]:
    return tuple(
        (name, _capture(path, kind, workspace, hash_content=hash_content))
        for name, (path, kind) in sorted(fixed_controls().items())
    )


def _record(states: tuple[tuple[str, ControlState], ...]) -> dict:
    # Serialize the entire fixed state, including absent-path ancestor and
    # shared-cache identities. A fresh target cannot silently bless changes.
    from dataclasses import asdict

    return {
        "schema": 1,
        "system": platform.system(),
        "architecture": platform.machine().lower(),
        "controls": {name: {**asdict(state), "path": str(state.path)} for name, state in states},
    }


def seal_loader_controls(root: Path, *, workspace: Path) -> str:
    states = _capture_all(workspace.resolve(), hash_content=True)
    LoaderControls("", states).verify(workspace.resolve())
    raw = (json.dumps(_record(states), sort_keys=True, indent=2) + "\n").encode()
    if len(raw) > MANIFEST_LIMIT:
        raise ValueError("加载器控制清单超过限制")
    path = root / MANIFEST_NAME
    with path.open("xb") as stream:
        stream.write(raw)
    path.chmod(0o600)
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class LoaderControls:
    manifest_sha256: str
    states: tuple[tuple[str, ControlState], ...]

    @property
    def protected_write_paths(self) -> tuple[Path, ...]:
        return tuple(sorted({path for _, state in self.states for path in state.protected_paths}))

    def verify(self, workspace: Path) -> None:
        current = _capture_all(workspace, hash_content=False)
        # Cached identity is valid only for exactly the sealed fixed set.
        from dataclasses import replace

        expected = tuple((name, replace(state, content_sha256=())) for name, state in self.states)
        if current != expected:
            raise ValueError("加载器控制路径身份或内容集合已改变")


def inspect_loader_controls(
    raw: bytes,
    *,
    manifest_sha256: str,
    workspace: Path,
    previous: LoaderControls | None = None,
) -> LoaderControls:
    if len(raw) > MANIFEST_LIMIT or hashlib.sha256(raw).hexdigest() != manifest_sha256:
        raise ValueError("加载器控制清单摘要不匹配")
    if previous is not None:
        if previous.manifest_sha256 != manifest_sha256:
            raise ValueError("加载器控制安装清单已改变")
        previous.verify(workspace)
        return previous
    states = _capture_all(workspace, hash_content=True)
    record = json.loads(raw)
    expected = json.loads(json.dumps(_record(states)))
    if not isinstance(record, dict) or type(record.get("schema")) is not int or record != expected:
        raise ValueError("加载器配置内容、平台或身份与安装记录不符")
    result = LoaderControls(manifest_sha256, states)
    result.verify(workspace)
    return result
