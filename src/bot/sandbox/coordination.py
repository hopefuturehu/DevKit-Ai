"""Durable, cooperative admission leases for native calls sharing a host UID.

The OS sandbox remains the access boundary. Leases only coordinate participating
executors/brokers; they are never evidence that a process has exited. A crashed
owner leaves its lease occupied until authenticated cleanup explicitly releases
it. PID absence alone must never reap a lease.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import stat
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import psutil

from bot.sandbox.models import SandboxError, SandboxErrorCode, SandboxPolicy
from bot.sandbox.resources import DirectoryIdentity

RECORD_LIMIT = 1_000_000
LEASE_LIMIT = 4096
PATH_LIMIT = 4096
_LEASE_NAME = re.compile(r"lease-([0-9a-f]{32})\.json\Z")


def default_root() -> Path:
    # HOME/config/state_root must not partition cooperating executors into
    # independent registries. A pre-created foreign directory causes refusal.
    return Path("/tmp").resolve() / f"bot-native-admission-{os.getuid()}"


def _error(message: str, stage: str = "admission_registry") -> SandboxError:
    return SandboxError(SandboxErrorCode.UNAVAILABLE, message, stage=stage, executed=False)


def _encoded(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _binding_hash(binding: dict) -> str:
    return hashlib.sha256(_encoded(binding)).hexdigest()


def _owner() -> dict:
    return {"pid": os.getpid(), "started": psutil.Process().create_time(), "claim": uuid4().hex}


def owner_alive(owner: dict) -> bool:
    try:
        return psutil.Process(owner["pid"]).create_time() == owner["started"]
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied as exc:
        raise _error("无法确认原准入所有者身份") from exc


def validate_owner(owner: dict) -> None:
    if (
        not isinstance(owner, dict)
        or owner.keys() != {"pid", "started", "claim"}
        or type(owner["pid"]) is not int
        or owner["pid"] <= 0
        or type(owner["started"]) not in (int, float)
        or not math.isfinite(owner["started"])
        or owner["started"] <= 0
        or not isinstance(owner["claim"], str)
        or re.fullmatch(r"[0-9a-f]{32}", owner["claim"]) is None
    ):
        raise ValueError("owner identity")


def _file(info: os.stat_result) -> tuple:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
        or info.st_size > RECORD_LIMIT
    ):
        raise _error("准入记录必须为当前用户拥有的有界独立 0600 普通文件")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _rules(policy: SandboxPolicy) -> dict[str, list[str]]:
    return {
        key: list(map(str, getattr(policy, key)))
        for key in ("read_roots", "write_roots", "deny_read", "deny_write")
    }


def _regions(rules: dict, write: bool):
    denied = tuple(map(Path, rules["deny_write" if write else "deny_read"]))
    for name in rules["write_roots" if write else "read_roots"]:
        root = Path(name)
        # Explicit private HOME/tmp/cache read roots carve back into the
        # otherwise hidden runs parent. Only denies inside this grant apply.
        yield root, tuple(path for path in denied if path.is_relative_to(root))


def _intersects(left, right) -> bool:
    first, first_denies = left
    second, second_denies = right
    if first.is_relative_to(second):
        intersection = first
    elif second.is_relative_to(first):
        intersection = second
    else:
        return False
    return not any(intersection.is_relative_to(path) for path in (*first_denies, *second_denies))


def conflicts(first: dict, second: dict) -> bool:
    for writer, other in ((first, second), (second, first)):
        if any(
            _intersects(left, right)
            for left in _regions(writer, True)
            for write in (False, True)
            for right in _regions(other, write)
        ):
            return True
    return False


def _validate(record: dict, lease_id: str) -> None:
    try:
        if not isinstance(record, dict):
            raise ValueError("record fields")
        binding, owner = record["binding"], record["owner"]
        if binding.keys() != {"v", "id", "rules"} or type(binding["v"]) is not int:
            raise ValueError("binding fields")
        if binding["v"] not in (1, 2) or binding["id"] != lease_id:
            raise ValueError("binding identity")
        expected = {"binding", "owner"} | ({"recovery"} if binding["v"] == 2 else set())
        if record.keys() != expected or (
            binding["v"] == 2
            and record["recovery"] is not None
            and not isinstance(record["recovery"], dict)
        ):
            raise ValueError("record fields")
        rules = binding["rules"]
        if rules.keys() != {"read_roots", "write_roots", "deny_read", "deny_write"}:
            raise ValueError("rules fields")
        for values in rules.values():
            if not isinstance(values, list) or len(values) > PATH_LIMIT:
                raise ValueError("path count")
            for value in values:
                if (
                    not isinstance(value, str)
                    or len(value) > 8192
                    or "\0" in value
                    or not Path(value).is_absolute()
                    or os.path.normpath(value) != value
                ):
                    raise ValueError("invalid path")
        validate_owner(owner)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise _error("准入记录损坏；拒绝忽略未解决的占用") from exc


class AdmissionCoordinator:
    def __init__(self, root: Path | None = None):
        # An alternate root is a trusted host/testing API, never a tool option.
        self.root = root if root is not None else default_root()
        if not self.root.is_absolute() or self.root.resolve() != self.root:
            raise _error("准入注册表必须使用规范绝对路径")
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.identity = DirectoryIdentity.capture(self.root)

    @contextmanager
    def _locked(self):
        self.identity.verify()
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        mutex = None
        try:
            opened = os.fstat(directory)
            if (opened.st_dev, opened.st_ino) != (self.identity.device, self.identity.inode):
                raise _error("准入注册表目录已被替换")
            mutex = os.open(
                "mutex", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory
            )
            before = _file(os.fstat(mutex))
            try:
                fcntl.flock(mutex, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise _error("准入注册表正被其他调用更新", "admission_busy") from exc
            if _file(os.stat("mutex", dir_fd=directory, follow_symlinks=False)) != before:
                raise _error("准入互斥文件身份已改变")
            self.identity.verify()
            yield directory
            self.identity.verify()
        finally:
            if mutex is not None:
                os.close(mutex)
            os.close(directory)

    def _read(self, directory: int, lease_id: str) -> dict:
        name = f"lease-{lease_id}.json"
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            before = _file(os.fstat(stream.fileno()))
            raw = stream.read(RECORD_LIMIT + 1)
            if (
                len(raw) > RECORD_LIMIT
                or _file(os.fstat(stream.fileno())) != before
                or _file(os.stat(name, dir_fd=directory, follow_symlinks=False)) != before
            ):
                raise _error("准入记录读取期间改变")
        try:
            record = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise _error("准入记录不是有效的有界 JSON") from exc
        _validate(record, lease_id)
        return record

    def _publish(self, directory: int, record: dict) -> None:
        lease_id = record["binding"]["id"]
        _validate(record, lease_id)
        raw = _encoded(record)
        if len(raw) > RECORD_LIMIT:
            raise _error("准入记录超过限制")
        temporary = f".pending-{uuid4().hex}"
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                temporary, f"lease-{lease_id}.json", src_dir_fd=directory, dst_dir_fd=directory
            )
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass

    def acquire(self, policy: SandboxPolicy) -> AdmissionLease:
        binding = {"v": 2, "id": uuid4().hex, "rules": _rules(policy)}
        record = {"binding": binding, "owner": _owner(), "recovery": None}
        _validate(record, binding["id"])
        if any(
            _intersects(region, (self.root, ()))
            for write in (False, True)
            for region in _regions(binding["rules"], write)
        ):
            raise _error("调用不能访问准入注册表区域")
        with self._locked() as directory:
            for lease_id in self._ids(directory):
                if lease_id == binding["id"]:
                    raise _error("准入占用标识冲突")
                other = self._read(directory, lease_id)
                if conflicts(binding["rules"], other["binding"]["rules"]):
                    raise _error(
                        "工作区读写区域仍被其他调用占用，需先确认其清理完成", "admission_conflict"
                    )
            self._publish(directory, record)
        return AdmissionLease(self, binding, record["owner"])

    def _ids(self, directory: int) -> list[str]:
        leases = []
        with os.scandir(directory) as entries:
            for entry in entries:
                match = _LEASE_NAME.fullmatch(entry.name)
                if match:
                    if len(leases) >= LEASE_LIMIT:
                        raise _error("未解决的准入记录超过限制")
                    leases.append(match[1])
                elif (
                    entry.name != "mutex"
                    and re.fullmatch(r"\.pending-[0-9a-f]{32}", entry.name) is None
                ):
                    raise _error("准入注册表包含无法识别的记录")
        return leases

    def snapshots(self) -> list[dict]:
        """Trusted host inventory, never a tool-accessible record interface."""
        with self._locked() as directory:
            return [self._read(directory, lease_id) for lease_id in self._ids(directory)]

    def reference(self, binding: dict) -> dict:
        return {
            "registry": self.identity.record(),
            "id": binding["id"],
            "binding_sha256": _binding_hash(binding),
        }

    def recover(self, metadata: dict, *, recovery: dict | None = None) -> AdmissionLease:
        if (
            not isinstance(metadata, dict)
            or metadata.keys() != {"registry", "id", "binding_sha256"}
            or metadata["registry"] != self.identity.record()
            or not isinstance(metadata["id"], str)
            or re.fullmatch(r"[0-9a-f]{32}", metadata["id"]) is None
        ):
            raise _error("准入恢复记录身份无效")
        with self._locked() as directory:
            record = self._read(directory, metadata["id"])
            if _binding_hash(record["binding"]) != metadata["binding_sha256"]:
                raise _error("准入恢复记录与持久占用不一致")
            if recovery is not None and record.get("recovery") != recovery:
                raise _error("恢复日志在校验后发生改变")
            if owner_alive(record["owner"]):
                raise _error("原准入所有者仍存活，不能接管其清理")
            # This transfers bookkeeping ownership only, never process signal
            # authority or permission to start another command.
            record["owner"] = _owner()
            self._publish(directory, record)
        return AdmissionLease(
            self, record["binding"], record["owner"], recovery=record.get("recovery")
        )


@dataclass
class AdmissionLease:
    coordinator: AdmissionCoordinator
    binding: dict
    owner: dict
    released: bool = False
    recovery: dict | None = None

    def record(self) -> dict:
        return self.coordinator.reference(self.binding)

    def _check(self, directory: int) -> None:
        if (
            self.released
            or self.owner["pid"] != os.getpid()
            or self.owner["started"] != psutil.Process().create_time()
        ):
            raise _error("准入占用已释放或不属于当前宿主")
        current = self.coordinator._read(directory, self.binding["id"])
        expected = {"binding": self.binding, "owner": self.owner}
        if self.binding["v"] == 2:
            expected["recovery"] = self.recovery
        if current != expected:
            raise _error("准入占用的身份或所有者已改变")

    def verify(self) -> None:
        with self.coordinator._locked() as directory:
            self._check(directory)

    def save_recovery(self, recovery: dict) -> None:
        if self.binding["v"] != 2:
            raise _error("旧准入记录不能补写启动权限或恢复日志")
        # Detach caller objects, including nested guard/metadata structures.
        value = deepcopy(recovery)
        record = {"binding": self.binding, "owner": self.owner, "recovery": value}
        with self.coordinator._locked() as directory:
            self._check(directory)
            try:
                self.coordinator._publish(directory, record)
            except BaseException:
                # rename may have committed before a directory fsync failed.
                # Recognize only these exact bytes/owner, never rebaseline a
                # different record. Reraise so the caller still handles the
                # failed write, including cleanup when spawn already occurred.
                try:
                    if self.coordinator._read(directory, self.binding["id"]) == record:
                        self.recovery = value
                except Exception:
                    pass
                raise
        self.recovery = value

    def release(self) -> None:
        if self.released:
            return
        with self.coordinator._locked() as directory:
            self._check(directory)
            os.unlink(f"lease-{self.binding['id']}.json", dir_fd=directory)
            os.fsync(directory)
        self.released = True
