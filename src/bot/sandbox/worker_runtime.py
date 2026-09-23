"""Explicit worker installation and read-only interpreter integrity inspection.

Only installation executes the host-selected interpreter. Command execution
never probes a candidate binary or searches PATH. This module is stdlib-only so
the installer can load it without importing the bot application or site hooks.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MAX_FILES = 30_000
MANIFEST_LIMIT = 16_000_000
EXCLUDED = {"site-packages", "dist-packages"}

_PROBE = r"""
import ctypes, json, os, pathlib, platform, runpy, sys, sysconfig
runpy.run_path(sys.argv[1], run_name='trusted_worker_install_probe')
if sys.implementation.name != 'cpython' or sys.version_info < (3, 12):
    raise RuntimeError('worker requires CPython >= 3.12')
paths=set()
if sys.platform == 'darwin':
    lib=ctypes.CDLL(None)
    lib._dyld_image_count.restype=ctypes.c_uint32
    lib._dyld_get_image_name.argtypes=[ctypes.c_uint32]
    lib._dyld_get_image_name.restype=ctypes.c_char_p
    paths.update(lib._dyld_get_image_name(i).decode() for i in range(lib._dyld_image_count()))
elif sys.platform.startswith('linux'):
    for line in pathlib.Path('/proc/self/maps').read_text().splitlines():
        parts=line.split(maxsplit=5)
        if len(parts)==6 and parts[-1].startswith('/'):
            paths.add(parts[-1])
else:
    raise RuntimeError('worker supports macOS/Linux only')
print(json.dumps({'executable':str(pathlib.Path(sys.executable).resolve()),
 'stdlib':str(pathlib.Path(sysconfig.get_path('stdlib')).resolve()),
 'version':platform.python_version(), 'system':platform.system(),
 'machine':platform.machine(),
 'libraries':sorted({str(pathlib.Path(p).resolve()) for p in paths if pathlib.Path(p).is_file()})}))
"""


def _trusted(path: Path, workspace: Path) -> Path:
    if not path.is_absolute():
        raise ValueError(f"worker 运行时路径必须是绝对路径: {path}")
    resolved = path.resolve(strict=True)
    if not resolved.is_absolute() or resolved.is_relative_to(workspace):
        raise ValueError(f"worker 运行时不能来自工作区: {path}")
    for part in (resolved, *resolved.parents):
        info = part.stat()
        if info.st_uid not in {0, os.getuid()}:
            raise ValueError(f"worker 运行时路径所有者无效: {part}")
        if info.st_mode & 0o022:
            # A sticky system temporary parent cannot be replaced by the task;
            # the actual installation and all descendants must be private.
            if part != resolved and info.st_uid == 0 and info.st_mode & stat.S_ISVTX:
                continue
            raise ValueError(f"worker 运行时路径可由其他用户修改: {part}")
    return resolved


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _tree(stdlib: Path) -> tuple[set[Path], set[Path]]:
    files, directories = set(), set()

    def unreadable(exc):
        raise exc

    for directory, dirs, names in os.walk(stdlib, followlinks=False, onerror=unreadable):
        base = Path(directory)
        directories.add(base)
        dirs[:] = sorted(name for name in dirs if name not in EXCLUDED | {"__pycache__"})
        for name in dirs:
            if (base / name).is_symlink():
                raise ValueError("标准库目录不能使用外部符号链接")
        for name in names:
            path = base / name
            if path.is_symlink():
                raise ValueError("标准库文件不能使用符号链接")
            if not path.is_file():
                raise ValueError("标准库包含非普通文件")
            files.add(path)
            if len(files) + len(directories) > MAX_FILES:
                raise ValueError("标准库文件清单超过限制")
    return files, directories


def install_worker(root: Path, *, workspace: Path, python: Path | None = None) -> str:
    """Run only from the explicit trusted installer, before publishing root."""
    workspace = workspace.resolve()
    executable = _trusted(python or Path(sys._base_executable), workspace)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("worker Python 不可执行")
    source = Path(__file__).with_name("worker.py")
    worker = root / "worker.py"
    worker.write_bytes(source.read_bytes())
    worker.chmod(0o600)
    cache = root / "python-cache"
    cache.mkdir(mode=0o700, exist_ok=True)
    if list(cache.iterdir()):
        raise ValueError("worker Python cache 必须为空")
    probe = subprocess.run(
        [
            str(executable),
            "-I",
            "-S",
            "-B",
            "-X",
            f"pycache_prefix={cache}",
            "-c",
            _PROBE,
            str(worker),
        ],
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        cwd=root,
        capture_output=True,
        timeout=30,
        check=True,
    )
    if len(probe.stdout) > 1_000_000:
        raise ValueError("worker Python 探测响应过大")
    metadata = json.loads(probe.stdout)
    metadata["cache_mode"] = "empty-prefix"
    stdlib = _trusted(Path(metadata["stdlib"]), workspace)
    if workspace.is_relative_to(stdlib):
        raise ValueError("工作区不能位于 Python 标准库内")
    files, _ = _tree(stdlib)
    libraries = {_trusted(Path(p), workspace) for p in metadata["libraries"]}
    executable = _trusted(Path(metadata["executable"]), workspace)
    metadata["files"] = {
        str(p): _sha(_trusted(p, workspace)) for p in sorted(files | libraries | {executable})
    }
    metadata["worker_sha256"] = _sha(worker)
    data = (json.dumps(metadata, sort_keys=True, indent=2) + "\n").encode()
    if len(data) > MANIFEST_LIMIT:
        raise ValueError("worker Python 清单超过限制")
    record = root / "worker-python.json"
    record.write_bytes(data)
    record.chmod(0o600)
    return hashlib.sha256(data).hexdigest()


def _stamp(path: Path) -> tuple[int, ...]:
    info = path.stat()
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


@dataclass(frozen=True)
class WorkerRuntime:
    executable: Path
    stdlib: Path
    version: str
    manifest_sha256: str
    read_roots: tuple[Path, ...]
    denied_roots: tuple[Path, ...]
    files: tuple[tuple[Path, tuple[int, ...]], ...]
    directories: tuple[tuple[Path, tuple[int, ...]], ...]

    def verify(self, workspace: Path) -> None:
        for path, previous in self.files:
            if _trusted(path, workspace) != path or _stamp(path) != previous:
                raise ValueError(f"worker Python 安装身份已改变: {path}")
        for path, previous in self.directories:
            if _trusted(path, workspace) != path or _stamp(path)[:4] != previous[:4]:
                raise ValueError(f"worker Python 目录身份已改变: {path}")
        files, _ = _tree(self.stdlib)
        if files != {p for p, _ in self.files if p.is_relative_to(self.stdlib)}:
            raise ValueError("worker Python 标准库内容已改变")


def inspect_worker(
    root: Path, workspace: Path, *, manifest_sha256: str, previous: WorkerRuntime | None = None
) -> WorkerRuntime:
    record = _trusted(root / "worker-python.json", workspace)
    with record.open("rb") as stream:
        data = stream.read(MANIFEST_LIMIT + 1)
    if len(data) > MANIFEST_LIMIT or hashlib.sha256(data).hexdigest() != manifest_sha256:
        raise ValueError("worker Python 清单校验失败")
    cache = _trusted(root / "python-cache", workspace)
    if cache != root / "python-cache" or list(cache.iterdir()):
        raise ValueError("worker Python cache 必须是可信空目录")
    if previous is not None and previous.manifest_sha256 == manifest_sha256:
        previous.verify(workspace)
        return previous
    metadata = json.loads(data)
    if metadata.get("cache_mode") != "empty-prefix":
        raise ValueError("worker Python 缺少隔离缓存配置")
    if metadata["system"] != platform.system() or metadata["machine"] != platform.machine():
        raise ValueError("worker Python 平台不匹配")
    if _sha(root / "worker.py") != metadata["worker_sha256"]:
        raise ValueError("worker 实现与安装清单不匹配")
    stdlib = _trusted(Path(metadata["stdlib"]), workspace)
    if workspace.is_relative_to(stdlib):
        raise ValueError("工作区不能位于 Python 标准库内")
    executable = _trusted(Path(metadata["executable"]), workspace)
    if not os.access(executable, os.X_OK):
        raise ValueError("worker Python 不可执行")
    libraries = {_trusted(Path(p), workspace) for p in metadata["libraries"]}
    files, directories = _tree(stdlib)
    expected = files | libraries | {executable}
    if set(metadata["files"]) != set(map(str, expected)):
        raise ValueError("worker Python 安装内容清单不一致")
    stamps = []
    for path in sorted(expected):
        before = _stamp(_trusted(path, workspace))
        if _sha(path) != metadata["files"][str(path)] or _stamp(path) != before:
            raise ValueError(f"worker Python 文件校验失败: {path}")
        stamps.append((path, before))
    # Keep parents of mapped libraries/executable in the fast verification set,
    # as well as every stdlib directory (new files invalidate admission).
    directories.update(p.parent for p in libraries | {executable})
    result = WorkerRuntime(
        executable=executable,
        stdlib=stdlib,
        version=metadata["version"],
        manifest_sha256=manifest_sha256,
        read_roots=tuple(sorted({stdlib, executable, *libraries})),
        denied_roots=tuple(stdlib / name for name in sorted(EXCLUDED)),
        files=tuple(stamps),
        directories=tuple((p, _stamp(_trusted(p, workspace))) for p in sorted(directories)),
    )
    result.verify(workspace)
    return result
