"""Bounded, descriptor-relative Skill reads; never executes a Skill script."""

from __future__ import annotations

import os
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from bot.sandbox.models import SandboxError, SandboxErrorCode
from bot.sandbox.policy import host_read_denials

if TYPE_CHECKING:
    from bot.config.models import AppConfig

SKILL_BYTES = 32_000
RESOURCE_BYTES = 60_000
CATALOG_BYTES = 512_000
CATALOG_ENTRIES = 128


class SkillReadError(ValueError):
    pass


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid


def _version(info: os.stat_result) -> tuple[int, ...]:
    return (*_identity(info), info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _configured_root(value: str, workspace: Path) -> Path:
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise SkillReadError("Skill 根目录无效")
    path = Path(value).expanduser()
    if ".." in path.parts:
        raise SkillReadError("Skill 根目录不能包含 ..")
    # Do not resolve project-selected symlinks into new read permissions.
    return path if path.is_absolute() else workspace / path


def authorized_root(config: AppConfig, workspace: Path) -> Path:
    root = _configured_root(config.skills.path, workspace)
    if root == workspace or root in workspace.parents or root == Path.home():
        raise SkillReadError("Skill 必须使用专用子目录")
    if not root.is_relative_to(workspace):
        trust = config._sandbox_trust
        if (
            trust is None
            or trust.skill_path is None
            or root != _configured_root(trust.skill_path, workspace)
        ):
            raise SkillReadError("工作区外的 Skill 根目录必须由可信配置精确授权")
    return root


def denied_paths(config: AppConfig, workspace: Path, protected: tuple[Path, ...]):
    values = (
        *host_read_denials(config, workspace),
        workspace / ".git",
        workspace / config.subagents.worktree_dir,
        *protected,
    )
    return tuple(sorted({p for value in values for p in (value, value.resolve())}))


class SkillResourceBroker:
    def __init__(self, config: AppConfig, workspace: Path, protected: tuple[Path, ...]):
        self.workspace = workspace.resolve()
        self.root = authorized_root(config, self.workspace)
        self.denied = denied_paths(config, self.workspace, protected)
        self.identities: dict[Path, tuple[int, ...]] = {}
        self._check(self.root)

    def assert_matches(self, config: AppConfig, workspace: Path, protected: tuple[Path, ...]):
        try:
            if (
                workspace.resolve() != self.workspace
                or authorized_root(config, self.workspace) != self.root
                or denied_paths(config, self.workspace, protected) != self.denied
            ):
                raise SkillReadError("Skill broker 的配置或工作区已改变")
        except (OSError, ValueError) as exc:
            raise SandboxError(
                SandboxErrorCode.POLICY_DENIED,
                "Skill broker 授权已失效",
                stage="skill_broker",
                executed=False,
            ) from exc

    def _check(self, path: Path):
        if any(path.is_relative_to(denied) for denied in self.denied):
            raise SkillReadError("Skill 路径位于受保护区域")

    @contextmanager
    def directory(self, path: Path):
        """Pin every ancestor; verify path bindings again before returning data."""
        self._check(path)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        with ExitStack() as stack:
            current = os.open(path.anchor, flags)
            stack.callback(os.close, current)
            chain = []
            selected = Path(path.anchor)
            for part in path.parts[1:]:
                selected /= part
                child = os.open(part, flags, dir_fd=current)
                stack.callback(os.close, child)
                identity = _identity(os.fstat(child))
                if selected in self.identities and self.identities[selected] != identity:
                    raise SkillReadError("Skill 目录身份已改变，请重新扫描")
                chain.append((current, part, identity))
                current = child
            yield current
            for parent, name, identity in chain:
                if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != identity:
                    raise SkillReadError("Skill 读取期间目录发生替换")

    def read(self, path: Path, limit: int) -> str:
        self._check(path)
        with self.directory(path.parent) as parent:
            fd = os.open(
                path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent,
            )
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise SkillReadError("Skill 只允许读取无硬链接的普通文件")
                if before.st_size > limit:
                    raise SkillReadError("Skill 文件超过读取上限")
                chunks = bytearray()
                while len(chunks) <= limit:
                    chunk = os.read(fd, min(16_384, limit + 1 - len(chunks)))
                    if not chunk:
                        break
                    chunks.extend(chunk)
                if len(chunks) > limit:
                    raise SkillReadError("Skill 文件超过读取上限")
                if (
                    _version(before) != _version(os.fstat(fd))
                    or _version(before)
                    != _version(os.stat(path.name, dir_fd=parent, follow_symlinks=False))
                ):
                    raise SkillReadError("Skill 读取期间文件发生变化")
                content = chunks.decode("utf-8")
                if "\x00" in content:
                    raise SkillReadError("Skill 文件不是文本")
            finally:
                os.close(fd)
        return content

    def resource(self, skill_directory: Path, relative: str) -> str:
        parts = relative.split("/")
        if (
            len(relative.encode()) > 1024
            or len(parts) < 2
            or len(parts) > 16
            or parts[0] not in {"references", "scripts", "assets"}
            or any(not p or p in {".", ".."} or "\\" in p for p in parts)
            or any(ord(c) < 32 or ord(c) == 127 for c in relative)
        ):
            raise SkillReadError("Skill 资源必须是 references/scripts/assets 下的规范相对路径")
        if skill_directory not in self.identities or skill_directory.parent != self.root:
            raise SkillReadError("Skill 目录未获扫描授权")
        return self.read(skill_directory.joinpath(*parts), RESOURCE_BYTES)
