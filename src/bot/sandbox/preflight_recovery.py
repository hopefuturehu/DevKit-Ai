"""Durable ownership of fixed preflight resources, outside task write grants.

The parent state directory is trusted host storage. Its manifest is neither an
audit event nor a process journal: only the admission journal grants PID cleanup
authority. The coordinator mutex serializes ownership transfer across hosts.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from bot.sandbox.coordination import (
    RECORD_LIMIT,
    _file,
    _owner,
    conflicts,
    owner_alive,
    validate_owner,
)
from bot.sandbox.resources import DirectoryIdentity, OwnedDirectory


def _directory(value: dict, *, missing=False) -> OwnedDirectory:
    if (
        not isinstance(value, dict)
        or value.keys() != {"path", "device", "inode", "owner"}
        or not isinstance(value["path"], str)
        or not Path(value["path"]).is_absolute()
        or any(type(value[key]) is not int for key in ("device", "inode", "owner"))
        or value["device"] < 0
        or value["inode"] <= 0
        or value["owner"] != os.getuid()
    ):
        raise ValueError("自检资源目录身份无效")
    owned = OwnedDirectory(DirectoryIdentity(**value))
    if missing and not owned.path.exists() and not owned.path.is_symlink():
        if owned.path.resolve() != owned.path:
            raise ValueError("自检资源目录路径已改变")
        owned.removed = True
    else:
        owned.identity.verify()
    return owned


class ProbeResources:
    def __init__(self, parent, owned: OwnedDirectory, record: dict):
        self.parent = parent
        self.owned = owned
        self.record = deepcopy(record)
        self.socket_directory = _directory(record["socket_directory"], missing=True)

    @staticmethod
    def _open(owned: OwnedDirectory) -> int:
        owned.identity.verify()
        directory = os.open(owned.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(directory)
        if (info.st_dev, info.st_ino, info.st_uid) != (
            owned.identity.device,
            owned.identity.inode,
            owned.identity.owner,
        ):
            os.close(directory)
            raise ValueError("自检资源目录在打开时改变")
        return directory

    @staticmethod
    def _read(owned: OwnedDirectory) -> dict:
        directory = ProbeResources._open(owned)
        try:
            fd = os.open(
                "resources.json", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory
            )
            with os.fdopen(fd, "rb") as stream:
                before = _file(os.fstat(stream.fileno()))
                raw = stream.read(RECORD_LIMIT + 1)
                if (
                    len(raw) > RECORD_LIMIT
                    or _file(os.fstat(stream.fileno())) != before
                    or _file(os.stat("resources.json", dir_fd=directory, follow_symlinks=False))
                    != before
                ):
                    raise ValueError("自检资源记录读取期间改变")
            owned.identity.verify()
            return json.loads(raw)
        finally:
            os.close(directory)

    @staticmethod
    def _publish(owned: OwnedDirectory, record: dict) -> None:
        owned.identity.verify()
        raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        if len(raw) > RECORD_LIMIT:
            raise ValueError("自检资源记录超过限制")
        directory = ProbeResources._open(owned)
        temporary = f".resources-{uuid4().hex}"
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, "resources.json", src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)

    @classmethod
    def create(cls, parent, owned, socket_directory, manager):
        parent._state_identity.verify()
        record = {
            "schema": 2,
            "parent_scope": parent.cleanup_scope,
            "parent_directory": parent._state_identity.record(),
            "directory": owned.identity.record(),
            "socket_directory": socket_directory.identity.record(),
            "workspace_directory": DirectoryIdentity.capture(manager.workspace).record(),
            "state_directory": manager._state_identity.record(),
            "child_scope": manager.cleanup_scope,
            "owner": _owner(),
        }
        with parent.coordinator._locked():
            if (owned.path / "resources.json").exists():
                raise ValueError("自检资源记录已存在")
            cls._publish(owned, record)
            parent_fd = os.open(parent.state_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        return cls(parent, owned, record)

    @classmethod
    def recover(cls, parent, path: Path):
        parent._state_identity.verify()
        owned = OwnedDirectory(DirectoryIdentity.capture(path))
        with parent.coordinator._locked():
            record = cls._read(owned)
            if (
                not isinstance(record, dict)
                or record.keys()
                != {
                    "schema",
                    "parent_scope",
                    "parent_directory",
                    "directory",
                    "socket_directory",
                    "workspace_directory",
                    "state_directory",
                    "child_scope",
                    "owner",
                }
                or type(record["schema"]) is not int
                or record["schema"] != 2
                or record["parent_scope"] != parent.cleanup_scope
                or record["parent_directory"] != parent._state_identity.record()
                or record["directory"] != owned.identity.record()
                or path.parent != parent.state_root
                or not path.name.startswith("preflight-")
                or not isinstance(record["child_scope"], str)
                or not record["child_scope"]
            ):
                raise ValueError("自检资源记录不完整或父级运行时/目录身份不一致")
            work = _directory(record["workspace_directory"])
            state = _directory(record["state_directory"])
            sockets = _directory(record["socket_directory"], missing=True)
            if (
                work.path != path / "work"
                or state.path != path / "runs"
                or sockets.path.parent != Path("/tmp").resolve()
                or not sockets.path.name.startswith("bot-check-")
            ):
                raise ValueError("自检恢复目录超出固定资源区域")
            # A live owner, including another recoverer, cannot be overtaken.
            validate_owner(record["owner"])
            if owner_alive(record["owner"]):
                raise ValueError("自检资源所有者仍存活，不能接管")
            record["owner"] = _owner()
            cls._publish(owned, record)
        return cls(parent, owned, record)

    def remove(self) -> None:
        if self.owned.removed:
            return
        self.parent._state_identity.verify()
        with self.parent.coordinator._locked() as registry:
            if self._read(self.owned) != self.record or not owner_alive(self.record["owner"]):
                raise ValueError("自检资源所有权已改变，不能删除")
            if self.record["owner"]["pid"] != os.getpid():
                raise ValueError("自检资源不属于当前宿主")
            # Async process cleanup may have yielded since recovery verified
            # these identities. Do not delete replacement work/state trees.
            _directory(self.record["workspace_directory"])
            _directory(self.record["state_directory"])
            roots = [str(self.owned.path), str(self.socket_directory.path)]
            rules = {"read_roots": roots, "write_roots": roots, "deny_read": [], "deny_write": []}
            coordinator = self.parent.coordinator
            if any(
                conflicts(rules, coordinator._read(registry, lease_id)["binding"]["rules"])
                for lease_id in coordinator._ids(registry)
            ):
                raise ValueError("自检资源仍有未解决的进程占用，不能删除")
            self.socket_directory.remove()
            self.owned.remove()


class RecoveredPreflight:
    def __init__(self, resources: ProbeResources, manager):
        self.resources = resources
        self.manager = manager

    @property
    def pending(self):
        return not self.resources.owned.removed

    async def aclose(self):
        await self.manager.aclose()
        # Endpoint descriptors belonged to the authenticated dead host. The
        # socket pathname may survive; never remove it before child cleanup.
        self.resources.remove()
