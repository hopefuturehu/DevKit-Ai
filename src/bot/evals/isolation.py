"""Bounded fixture snapshots and independent evaluation workspaces.

Snapshots never follow symlinks. They are held by the evaluator, so the Agent
cannot change the pre-run baseline or the grader source by editing its workspace.
This is filesystem/state isolation, not an OS sandbox for hostile native code.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

IGNORED = frozenset(
    {
        ".git",
        ".bot",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tox",
        "artifacts",
        "dist",
        "build",
        ".DS_Store",
    }
)


@dataclass(frozen=True)
class Entry:
    kind: str
    mode: int
    content: bytes = b""

    def record(self) -> dict:
        return {
            "kind": self.kind,
            "mode": self.mode,
            "bytes": len(self.content),
            "sha256": hashlib.sha256(self.content).hexdigest(),
        }


Snapshot = dict[str, Entry]


def ignored(name: str) -> bool:
    return name in IGNORED or (name.startswith(".env") and name != ".env.example")


def snapshot(
    root: Path,
    *,
    limit: int,
    exclude: tuple[Path, ...] = (),
    inputs: list[str] | None = None,
    fixture: bool = False,
) -> Snapshot:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"目录不存在: {root}")
    result: Snapshot = {}
    total = 0

    def visit(path: Path) -> None:
        nonlocal total
        if any(path == blocked or blocked in path.parents for blocked in exclude):
            return
        name = path.relative_to(root).as_posix()
        # Agent-created paths must remain visible to change assertions. Only the
        # dedicated runtime state directory is excluded from the final snapshot.
        parts = Path(name).parts
        if (fixture and any(ignored(part) for part in parts)) or (
            not fixture and parts[0] in {".bot", ".git"}
        ):
            return
        if name in result:
            return
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISLNK(metadata.st_mode):
            if fixture:
                raise ValueError(f"fixture 不允许符号链接: {name}")
            entry = Entry("symlink", mode, os.readlink(path).encode())
        elif stat.S_ISDIR(metadata.st_mode):
            entry = Entry("directory", mode)
        elif stat.S_ISREG(metadata.st_mode):
            if metadata.st_size > limit - total:
                raise ValueError(f"快照超过 {limit} 字节上限: {name}")
            # O_NOFOLLOW also closes the lstat/open symlink replacement race.
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ValueError(f"快照只接受普通文件: {name}")
                entry = Entry("file", mode, handle.read(limit - total + 1))
        else:
            raise ValueError(f"快照不接受特殊文件: {name}")
        total += len(entry.content)
        if total > limit or len(result) >= 10000:
            raise ValueError("快照超过字节数或 10000 个条目上限")
        result[name] = entry
        if entry.kind == "directory":
            for child in sorted(path.iterdir()):
                visit(child)

    for raw in inputs if inputs is not None else [p.name for p in sorted(root.iterdir())]:
        path = root / raw
        # Reject any symlink in explicitly selected fixture ancestors, too.
        if fixture and root not in path.resolve().parents and path.resolve() != root:
            raise ValueError(f"fixture 路径越界: {raw}")
        for parent in (path, *path.parents):
            if parent == root:
                break
            if fixture and parent.is_symlink():
                raise ValueError(f"fixture 路径包含符号链接: {raw}")
        if path == root:
            for child in sorted(root.iterdir()):
                visit(child)
        else:
            visit(path)
    return result


def materialize(entries: Snapshot, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, entry in sorted(
        entries.items(), key=lambda item: (len(Path(item[0]).parts), item[0])
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if entry.kind == "directory":
            path.mkdir(exist_ok=True)
        elif entry.kind == "file":
            path.write_bytes(entry.content)
            path.chmod(entry.mode & 0o777)
        else:
            # Do not materialize Agent symlinks into a host-side grader tree.
            # File verifiers reject them, and their changes remain in the manifest.
            continue
    for name, entry in sorted(entries.items(), reverse=True):
        if entry.kind == "directory":
            (root / name).chmod(entry.mode & 0o777)


def inventory(entries: Snapshot) -> dict[str, dict]:
    return {name: entry.record() for name, entry in sorted(entries.items())}


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def changes(before: Snapshot, after: Snapshot) -> dict[str, str]:
    return {
        name: "created" if name not in before else "deleted" if name not in after else "modified"
        for name in sorted(before.keys() | after.keys())
        if before.get(name) != after.get(name)
    }


def fresh_file(name: str, before: Snapshot, after: Snapshot) -> bool:
    old, new = before.get(name), after.get(name)
    return (
        new is not None
        and new.kind == "file"
        and (old is None or old.kind != "file" or old.content != new.content)
    )
