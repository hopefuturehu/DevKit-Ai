from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutionScope(FrozenModel):
    session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    workspace: Path
    policy_id: str = Field(min_length=1)


class SandboxPolicy(FrozenModel):
    workspace: Path
    read_roots: tuple[Path, ...]
    write_roots: tuple[Path, ...]
    deny_read: tuple[Path, ...]
    deny_write: tuple[Path, ...]
    protected_paths: tuple[Path, ...]
    network_mode: Literal["deny", "allowlist"] = "deny"
    allowed_domains: tuple[str, ...] = ()
    environment_names: tuple[str, ...] = ()
    home_dir: Path
    temp_dir: Path
    cache_dir: Path

    @model_validator(mode="after")
    def validate_paths(self) -> SandboxPolicy:
        paths = (
            self.workspace,
            self.home_dir,
            self.temp_dir,
            self.cache_dir,
            *self.read_roots,
            *self.write_roots,
            *self.deny_read,
            *self.deny_write,
            *self.protected_paths,
        )
        if any(not path.is_absolute() for path in paths):
            raise ValueError("sandbox 策略只接受绝对路径")
        if self.network_mode == "deny" and self.allowed_domains:
            raise ValueError("断网策略不能包含允许的域名")
        return self

    @property
    def policy_id(self) -> str:
        raw = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()


Capability = Literal["enforced", "unavailable", "unverified"]


class SandboxCapabilities(FrozenModel):
    backend: Literal["srt"] = "srt"
    backend_version: str | None = None
    node_version: str | None = None
    os_version: str
    architecture: str
    file_read: Capability = "unverified"
    file_write: Capability = "unverified"
    network_filter: Capability = "unverified"
    process_isolation: Capability = "unverified"
    resource_limits: Capability = "unavailable"
    evidence: tuple[str, ...] = ()


class SandboxLaunch(FrozenModel):
    logical_argv: tuple[str, ...] = Field(min_length=1)
    launcher_argv: tuple[str, ...] = Field(min_length=1)
    cwd: Path
    environment: tuple[tuple[str, str], ...]
    scope: ExecutionScope
    resource_paths: tuple[Path, ...] = ()


class SandboxErrorCode(StrEnum):
    UNAVAILABLE = "SANDBOX_UNAVAILABLE"
    CONFIG_INVALID = "SANDBOX_CONFIG_INVALID"
    LAUNCH_FAILED = "SANDBOX_LAUNCH_FAILED"
    POLICY_DENIED = "POLICY_DENIED"
    COMMAND_FAILED = "COMMAND_FAILED"
    PROTOCOL_ERROR = "TOOL_PROTOCOL_ERROR"
    CLEANUP_FAILED = "CLEANUP_FAILED"


class SandboxError(RuntimeError):
    def __init__(
        self,
        code: SandboxErrorCode,
        message: str,
        *,
        stage: str = "preflight",
        executed: bool | None = False,
    ):
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.stage = stage
        self.executed = executed
