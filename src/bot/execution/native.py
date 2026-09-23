"""Managed native command backend, kept behind the closed production factory.

The trusted caller binds each invocation. No tool can submit a policy or choose
a launcher. Full startup attestation, tool coverage and recovery acceptance
remain release gates; metadata inspection alone is not an enforcement claim.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path

from bot.config.models import AppConfig
from bot.execution.base import EnvironmentCapabilities, ProcessSpec
from bot.execution.local import LocalExecutionTarget, PreparedLaunch
from bot.sandbox.models import ExecutionScope, SandboxError, SandboxErrorCode
from bot.sandbox.policy import (
    ResolvedSandbox,
    RunDirectories,
    resolve_policy,
    validate_security_context,
)
from bot.sandbox.resources import DirectoryIdentity, OwnedDirectory
from bot.sandbox.runtime import inspect_runtime
from bot.sandbox.srt import compile_srt_config, launcher_environment, system_read_paths


@dataclass
class _Run:
    directory: OwnedDirectory
    read_only: bool


class NativeSandboxExecutionTarget(LocalExecutionTarget):
    def __init__(
        self,
        config: AppConfig,
        *,
        workspace: Path,
        runtime_root: Path,
        state_root: Path,
    ) -> None:
        sandbox = validate_security_context(config)
        if sandbox.mode != "required":
            raise SandboxError(SandboxErrorCode.CONFIG_INVALID, "Native 后端需要 required 配置")
        if sandbox.network.mode == "allowlist" and config.permissions.network != "deny":
            raise SandboxError(SandboxErrorCode.UNAVAILABLE, "allowlist 出口尚未通过完整原生验收")
        self.config = config.model_copy(deep=True)
        self.workspace = workspace.resolve(strict=True)
        self.runtime = inspect_runtime(self.workspace, root=runtime_root)
        if not self.runtime.metadata_valid:
            raise SandboxError(SandboxErrorCode.UNAVAILABLE, "; ".join(self.runtime.errors))
        self.state_root = state_root.resolve()
        for root in (self.workspace, self.runtime.root):
            if self.state_root.is_relative_to(root) or root.is_relative_to(self.state_root):
                raise SandboxError(
                    SandboxErrorCode.CONFIG_INVALID, "私有状态区域与工作区/运行时重叠"
                )
        self.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._state_identity = DirectoryIdentity.capture(self.state_root)
        self._runs: dict[tuple[str, str], _Run] = {}
        self._bindings: dict[ExecutionScope, ResolvedSandbox] = {}
        self._used_calls: set[tuple[str, str, str]] = set()
        self._recovery_error = False
        super().__init__(
            max_managed_processes=config.agent.max_managed_processes,
            cleanup=config.agent.process_cleanup,
            strict_process_scope=True,
        )

    @property
    def progress_scope(self) -> str:
        from bot.core.termination.identity import fingerprint

        return fingerprint(
            "native-srt-managed-v1",
            str(self.runtime.root),
            str(self.state_root),
            self.runtime.capabilities.model_dump(mode="json"),
            hashlib.sha256((self.runtime.root / "launcher.mjs").read_bytes()).hexdigest(),
        )

    def bind_scope(
        self, *, session_id: str, run_id: str, tool_call_id: str, read_only: bool = False
    ) -> ExecutionScope:
        """Trusted host API, invoked after tool authorization, never a model argument."""
        if self._closed or self._recovery_error:
            raise SandboxError(SandboxErrorCode.UNAVAILABLE, "执行环境已关闭或存在未解决的恢复记录")
        if not all((session_id, run_id, tool_call_id)):
            raise SandboxError(
                SandboxErrorCode.CONFIG_INVALID, "调用必须绑定 session/run/tool 标识"
            )
        call = (session_id, run_id, tool_call_id)
        if call in self._used_calls or any(
            (s.session_id, s.run_id, s.tool_call_id) == call for s in self._bindings
        ):
            raise SandboxError(SandboxErrorCode.CONFIG_INVALID, "调用作用域不能重复使用")
        self._state_identity.verify()
        key = (session_id, run_id)
        if key not in self._runs:
            owned = OwnedDirectory.create(self.state_root, prefix="run-")
            try:
                for name in ("home", "tmp", "cache"):
                    (owned.path / name).mkdir(mode=0o700)
            except BaseException:
                owned.remove()
                raise
            self._runs[key] = _Run(owned, read_only)
        run = self._runs[key]
        if run.read_only != read_only:
            raise SandboxError(SandboxErrorCode.CONFIG_INVALID, "运行期间不能改变 Run 的只读策略")
        run.directory.identity.verify()
        resolved = resolve_policy(
            self.config,
            workspace=self.workspace,
            directories=RunDirectories(run.directory.path),
            runtime_read_roots=(self.runtime.root, *system_read_paths()),
            read_only=read_only,
        )
        scope = ExecutionScope(
            session_id=session_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            workspace=self.workspace,
            policy_id=resolved.policy.policy_id,
        )
        self._bindings[scope] = resolved
        return scope

    def _prepare_launch(self, spec: ProcessSpec) -> PreparedLaunch:
        scope = spec.scope
        if self._recovery_error or scope is None or scope not in self._bindings:
            raise SandboxError(SandboxErrorCode.CONFIG_INVALID, "缺少可信且未使用的调用作用域")
        if (spec.session_id, spec.run_id) != (scope.session_id, scope.run_id):
            raise SandboxError(SandboxErrorCode.CONFIG_INVALID, "调用身份与作用域不一致")
        if not spec.cwd.is_relative_to(scope.workspace):
            raise SandboxError(SandboxErrorCode.CONFIG_INVALID, "工作目录超出调用工作区")
        resolved = self._bindings[scope]
        if any(spec.cwd.is_relative_to(path) for path in resolved.policy.deny_read):
            raise SandboxError(SandboxErrorCode.CONFIG_INVALID, "工作目录位于禁止读取区域")
        inspection = inspect_runtime(self.workspace, root=self.runtime.root)
        if not inspection.metadata_valid or inspection != self.runtime:
            raise SandboxError(SandboxErrorCode.UNAVAILABLE, "运行时安装记录已改变或缺少依赖")
        self._state_identity.verify()
        run = self._runs[(scope.session_id, scope.run_id)]
        run.directory.identity.verify()
        config = compile_srt_config(resolved.policy)
        control = OwnedDirectory.create(Path("/tmp").resolve(), prefix="bot-srt-")
        try:
            environment = launcher_environment(
                resolved.policy,
                runtime_root=self.runtime.root,
                control_temp=control.path,
                extra=spec.env,
            )
            settings = control.path / "policy.json"
            fd = os.open(settings, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(config, stream)
            # Keep policy compilation and the last admission check in the same
            # synchronous launch preparation, under the manager's start lock.
            resolved.admission.verify()
        except BaseException:
            control.remove()
            raise
        executables = dict(inspection.executables)
        argv = (
            str(executables["node"]),
            str(executables["launcher"]),
            str(settings),
            "--",
            *spec.argv,
        )
        del self._bindings[scope]
        self._used_calls.add((scope.session_id, scope.run_id, scope.tool_call_id))
        return PreparedLaunch(
            argv,
            environment,
            metadata={
                "backend": "srt",
                "backend_version": self.runtime.capabilities.backend_version,
                "node_version": self.runtime.capabilities.node_version,
                "scope": scope.model_dump(mode="json"),
                "launcher_argv": list(argv),
                "control_directory": control.identity.record(),
                "run_directory": run.directory.identity.record(),
                "read_only": run.read_only,
                "executed": None,
            },
            release=control.remove,
        )

    def restore_cleanups(self, records: list[dict]) -> None:
        # Recovery may preserve cleanup authority, never a permission to launch
        # a new command. Invalid native records block further execution.
        try:
            if any(
                record.get("launch_metadata", {}).get("backend") == "srt"
                and record.get("cleanup_scope") != self.cleanup_scope
                for record in records
            ):
                raise ValueError("原生清理记录来自其他运行时身份；需要先解决旧资源")
            super().restore_cleanups(records)
        except Exception:
            self._recovery_error = True
            raise

    def _restore_launch(self, record: dict) -> PreparedLaunch:
        metadata = record["launch_metadata"]
        spec = ProcessSpec.model_validate(record["spec"])
        scope = ExecutionScope.model_validate(metadata["scope"])
        if (
            metadata["backend"] != "srt"
            or metadata["backend_version"] != self.runtime.capabilities.backend_version
            or metadata["node_version"] != self.runtime.capabilities.node_version
            or spec.scope != scope
            or (spec.session_id, spec.run_id) != (scope.session_id, scope.run_id)
            or scope.workspace != self.workspace
            or type(metadata["read_only"]) is not bool
        ):
            raise ValueError("沙箱恢复记录身份不一致")
        control = OwnedDirectory(DirectoryIdentity(**metadata["control_directory"]))
        owned = OwnedDirectory(DirectoryIdentity(**metadata["run_directory"]))
        if (
            control.path.parent != Path("/tmp").resolve()
            or not control.path.name.startswith("bot-srt-")
            or owned.path.parent != self.state_root
            or not owned.path.name.startswith("run-")
        ):
            raise ValueError("沙箱恢复目录超出可信资源区域")
        control.identity.verify()
        owned.identity.verify()
        key = (scope.session_id, scope.run_id)
        if key in self._runs and self._runs[key].directory.identity != owned.identity:
            raise ValueError("同一 Run 的恢复目录不一致")
        self._runs[key] = _Run(owned, metadata["read_only"])
        self._used_calls.add((scope.session_id, scope.run_id, scope.tool_call_id))
        return PreparedLaunch(tuple(spec.argv), {}, metadata=metadata, release=control.remove)

    async def probe(self, executables: list[str] | None = None) -> EnvironmentCapabilities:
        # Fixed search locations only. Availability is not proof that a command
        # is authorized or compatible with the active sandbox policy.
        path = f"{self.runtime.root / 'node/bin'}:/usr/bin:/bin:/usr/sbin:/sbin"
        return EnvironmentCapabilities(
            operating_system=platform.system().lower(),
            architecture=platform.machine().lower(),
            executables={name: shutil.which(name, path=path) for name in executables or []},
        )

    async def aclose(self) -> None:
        await super().aclose()
        if self._recovery_error:
            raise RuntimeError("process_cleanup_incomplete: 沙箱恢复身份未确认，保留资源")
        for run in self._runs.values():
            run.directory.remove()
        self._runs.clear()
        self._bindings.clear()
