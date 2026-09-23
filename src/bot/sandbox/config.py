from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class FrozenConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def canonical_root(value: str | Path) -> Path:
    text = str(value)
    if (
        not text
        or any(ord(c) < 32 or ord(c) == 127 for c in text)
        or any(c in text for c in "*?[]{}")
    ):
        raise ValueError("sandbox 路径必须为不含控制字符或 glob 的明确路径")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError("sandbox 额外目录必须使用绝对路径或 ~/ 路径")
    return path.resolve()


def canonical_domain(value: str) -> str:
    if not value or value != value.strip() or any(c in value for c in "/*?@[]\\#"):
        raise ValueError("sandbox 域名必须是精确 hostname，可附带端口，不支持 URL/IP/glob")
    host, separator, port = value.partition(":")
    if separator and (not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535):
        raise ValueError("sandbox 域名端口必须在 1..65535 范围内")
    try:
        host = host.encode("idna").decode("ascii").lower().removesuffix(".")
    except UnicodeError as exc:
        raise ValueError("sandbox 域名无效") from exc
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("sandbox 公网白名单不接受 IP 字面量")
    if (
        len(host) > 253
        or not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", host
        )
        or host.endswith(".localhost")
        or host.rpartition(".")[2].isdigit()
    ):
        raise ValueError("sandbox 白名单必须是完整公网域名")
    return host + (f":{int(port)}" if separator else "")


class SandboxNetworkConfig(FrozenConfig):
    mode: Literal["deny", "allowlist"] = "deny"
    allowed_domains: tuple[str, ...] = ()

    @field_validator("allowed_domains")
    @classmethod
    def validate_domains(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({canonical_domain(value) for value in values}))

    @model_validator(mode="after")
    def validate_mode(self) -> SandboxNetworkConfig:
        if self.mode == "deny" and self.allowed_domains:
            raise ValueError("sandbox.network.mode=deny 时 allowed_domains 必须为空")
        if self.mode == "allowlist" and not self.allowed_domains:
            raise ValueError("sandbox.network.mode=allowlist 必须指定精确域名")
        return self


class SandboxConfig(FrozenConfig):
    mode: Literal["off", "required"] = "off"
    backend: Literal["srt"] = "srt"
    read_roots: tuple[Path, ...] = ()
    write_roots: tuple[Path, ...] = ()
    network: SandboxNetworkConfig = SandboxNetworkConfig()

    @field_validator("read_roots", "write_roots", mode="before")
    @classmethod
    def validate_roots(cls, values) -> tuple[Path, ...]:
        if not isinstance(values, (list, tuple)):
            raise ValueError("sandbox roots 必须为路径列表")
        return tuple(sorted({canonical_root(value) for value in values}))


@dataclass(frozen=True)
class PermissionCeiling:
    mode: str
    auto_approve: bool
    workspace_only: bool
    network: str


@dataclass(frozen=True)
class SandboxTrust:
    sandbox: SandboxConfig
    permissions: PermissionCeiling
    trusted_config: Path
    project_config: Path | None
    require_sandbox: bool = False
    trusted_overrides: tuple[str, ...] = ()
    source_paths: tuple[Path, ...] = ()


def assert_at_most(candidate: SandboxConfig, ceiling: SandboxConfig) -> None:
    """Reject widening; silently clipping an invalid request hides config mistakes."""
    if ceiling.mode == "required" and candidate.mode != "required":
        raise ValueError("项目或派生配置不能关闭 required 沙箱")
    for name in ("read_roots", "write_roots"):
        roots = getattr(ceiling, name)
        if any(
            not any(path.is_relative_to(root) for root in roots)
            for path in getattr(candidate, name)
        ):
            raise ValueError(f"项目或派生配置不能扩大 sandbox.{name}")
    if ceiling.network.mode == "deny" and candidate.network.mode != "deny":
        raise ValueError("项目或派生配置不能放宽 sandbox.network")
    if candidate.network.mode == "allowlist":
        for domain in candidate.network.allowed_domains:
            host = domain.partition(":")[0]
            if (
                domain not in ceiling.network.allowed_domains
                and host not in ceiling.network.allowed_domains
            ):
                raise ValueError("项目或派生配置不能扩大 sandbox.network.allowed_domains")


def assert_permissions_at_most(candidate, ceiling: PermissionCeiling) -> None:
    modes = {"read-only": 0, "safe": 1, "full-access": 2}
    networks = {"deny": 0, "ask": 1, "allow": 2}
    if (
        modes[candidate.mode] > modes[ceiling.mode]
        or networks[candidate.network] > networks[ceiling.network]
        or (candidate.auto_approve and not ceiling.auto_approve)
        or (ceiling.workspace_only and not candidate.workspace_only)
    ):
        raise ValueError("项目或派生配置不能放宽 permissions 安全上限")
