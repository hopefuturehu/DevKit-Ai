"""Host-owned directory handles; pathname text alone is never cleanup authority."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class DirectoryIdentity:
    path: str
    device: int
    inode: int
    owner: int

    @classmethod
    def capture(cls, path: Path) -> DirectoryIdentity:
        info = path.lstat()
        if (
            path.resolve() != path
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("沙箱私有目录必须为当前用户拥有的真实 0700 目录")
        return cls(str(path), info.st_dev, info.st_ino, info.st_uid)

    def verify(self) -> None:
        if self.capture(Path(self.path)) != self:
            raise ValueError("沙箱目录身份已改变；不能按路径删除或重新接管")

    def record(self) -> dict:
        return asdict(self)


@dataclass
class OwnedDirectory:
    identity: DirectoryIdentity
    removed: bool = False

    @property
    def path(self) -> Path:
        return Path(self.identity.path)

    @classmethod
    def create(cls, parent: Path, *, prefix: str) -> OwnedDirectory:
        return cls(DirectoryIdentity.capture(Path(tempfile.mkdtemp(dir=parent, prefix=prefix))))

    def remove(self) -> None:
        if self.removed:
            return
        self.identity.verify()
        if not shutil.rmtree.avoids_symlink_attacks:
            raise RuntimeError("当前 Python 缺少基于目录描述符的安全清理")
        # Do not follow directory entries controlled by a task. The parent is
        # host-owned (or the sticky system tmp directory), never task-writable.
        parent = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)
            if (info.st_dev, info.st_ino, info.st_uid) != (
                self.identity.device,
                self.identity.inode,
                self.identity.owner,
            ):
                raise ValueError("沙箱目录在清理前被替换")
            shutil.rmtree(self.path.name, dir_fd=parent)
        finally:
            os.close(parent)
        self.removed = True
