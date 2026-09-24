"""Managed native command backend, kept behind the closed production factory.

The trusted caller binds each invocation. No tool can submit a policy or choose
a launcher. Full startup attestation, tool coverage and recovery acceptance
remain release gates; metadata inspection alone is not an enforcement claim.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from bot.config.models import AppConfig
from bot.execution.base import EnvironmentCapabilities, ExecutionTarget, ProcessSpec, ProcessStatus
from bot.execution.local import LocalExecutionTarget, PreparedLaunch
from bot.sandbox.coordination import AdmissionCoordinator, AdmissionLease, owner_alive
from bot.sandbox.launch_status import LaunchStatusPipe
from bot.sandbox.models import ExecutionScope, SandboxError, SandboxErrorCode
from bot.sandbox.policy import (
    ResolvedSandbox,
    RunDirectories,
    resolve_policy,
    validate_security_context,
)
from bot.sandbox.resources import DirectoryIdentity, OwnedDirectory
from bot.sandbox.runtime import RUNTIME_ASSET_NAMES, inspect_runtime
from bot.sandbox.srt import compile_srt_config, launcher_environment, system_read_paths


@dataclass(frozen=True)
class _Run:
    directory: OwnedDirectory
    read_only: bool
    denied_paths: tuple[Path, ...] = ()


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
        self._runtime_fingerprint = tuple(
            (name, hashlib.sha256((self.runtime.root / name).read_bytes()).hexdigest())
            for name in RUNTIME_ASSET_NAMES
        )
        self.state_root = state_root.resolve()
        for root in (self.workspace, self.runtime.root):
            if self.state_root.is_relative_to(root) or root.is_relative_to(self.state_root):
                raise SandboxError(
                    SandboxErrorCode.CONFIG_INVALID, "私有状态区域与工作区/运行时重叠"
                )
        self.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._state_identity = DirectoryIdentity.capture(self.state_root)
        self.coordinator = AdmissionCoordinator()
        if any(
            root.is_relative_to(self.coordinator.root) or self.coordinator.root.is_relative_to(root)
            for root in (self.workspace, self.runtime.root, self.state_root)
        ):
            raise SandboxError(
                SandboxErrorCode.CONFIG_INVALID, "准入注册表不能与工作区或运行时重叠"
            )
        self._runs: dict[tuple[str, str], _Run] = {}
        self._bindings: dict[ExecutionScope, ResolvedSandbox] = {}
        self._worker_calls: set[ExecutionScope] = set()
        self._used_calls: set[tuple[str, str, str]] = set()
        self._startups: dict[ExecutionScope, LaunchStatusPipe] = {}
        self._leases: dict[ExecutionScope, AdmissionLease] = {}
        self._recovery_error = False
        self._recovery_checked = False
        self._recovered_processes: set[str] = set()
        super().__init__(
            max_managed_processes=config.agent.max_managed_processes,
            cleanup=config.agent.process_cleanup,
            strict_process_scope=True,
        )
        from bot.sandbox.preflight import NativePreflight, StartupGate

        self._preflight_gate = StartupGate(
            lambda: NativePreflight(
                self.runtime, self.state_root, self.config.agent.process_cleanup
            )
        )

    @property
    def has_live_processes(self) -> bool:
        return (
            self._recovery_error
            or bool(self._leases)
            or super().has_live_processes
            or self._preflight_gate.live
        )

    def _check_recovery(self) -> None:
        if not self._recovery_checked:
            self.restore_cleanups([])
        if any(
            pid in self._managed_processes
            and self._managed_process_alive(self._managed_processes[pid])
            for pid in self._recovered_processes
        ):
            raise SandboxError(
                SandboxErrorCode.CLEANUP_FAILED,
                "恢复的调用尚未确认清理，不能启动新调用",
                stage="recovery",
                executed=False,
            )

    @property
    def sandbox_capabilities(self):
        task = self._preflight_gate.task
        if task is None or not task.done():
            return self.runtime.capabilities
        if task.cancelled() or task.exception() is not None:
            return self.runtime.capabilities.model_copy(
                update={
                    "file_read": "unavailable",
                    "file_write": "unavailable",
                    "network_filter": "unavailable",
                    "evidence": ("live preflight failed or cancelled",),
                }
            )
        return task.result().capabilities

    def _inspect_current_runtime(self):
        inspection = inspect_runtime(
            self.workspace,
            root=self.runtime.root,
            worker_snapshot=self.runtime.worker,
            tree_snapshot=self.runtime.tree,
            host_snapshot=self.runtime.host,
            loader_snapshot=self.runtime.loader,
        )
        if not inspection.metadata_valid or inspection != self.runtime:
            raise SandboxError(SandboxErrorCode.UNAVAILABLE, "运行时安装记录已改变或缺少依赖")
        return inspection

    async def ensure_ready(self):
        if self._closed or self._recovery_error:
            raise SandboxError(SandboxErrorCode.UNAVAILABLE, "原生执行环境不可用")
        self._check_recovery()
        self._state_identity.verify()
        self._inspect_current_runtime()
        return await self._preflight_gate.ensure()

    async def start_process(self, spec: ProcessSpec) -> str:
        spec = ProcessSpec.model_validate(spec.model_dump()).model_copy(
            update={"cwd": spec.cwd.resolve()}
        )
        resolved = self._bound_policy(spec)
        try:
            resolved.admission.verify()
            await self.ensure_ready()
            return await super().start_process(spec)
        except BaseException:
            # A binding is removed only once launch resources take ownership.
            # Releasing a still-bound reservation cannot discard a live call.
            if spec.scope in self._bindings:
                self.discard_scope(spec.scope)
            raise

    @property
    def progress_scope(self) -> str:
        from bot.core.termination.identity import fingerprint

        return fingerprint(
            "native-srt-managed-v8-recovery-journal1",
            str(self.workspace),
            str(self.runtime.root),
            str(self.state_root),
            self.coordinator.identity.record(),
            self.runtime.capabilities.model_dump(mode="json"),
            self._runtime_fingerprint,
            self.runtime.worker.manifest_sha256 if self.runtime.worker else None,
            self.runtime.tree.manifest_sha256 if self.runtime.tree else None,
            self.runtime.host.manifest_sha256 if self.runtime.host else None,
            self.runtime.loader.manifest_sha256 if self.runtime.loader else None,
        )

    @property
    def requires_confinement(self) -> bool:
        return True

    def for_invocation(
        self,
        *,
        workspace: Path,
        session_id: str,
        run_id: str,
        tool_call_id: str,
        read_only: bool = False,
        denied_paths: tuple[Path, ...] = (),
    ) -> ExecutionTarget:
        from bot.execution.scoped import InvocationIdentity, NativeInvocationTarget

        if self._closed or self._recovery_error:
            raise SandboxError(SandboxErrorCode.UNAVAILABLE, "原生执行环境不可用")
        if workspace.resolve() != self.workspace:
            raise SandboxError(SandboxErrorCode.CONFIG_INVALID, "子工作区需要经过专用策略委派")
        if not all(
            isinstance(value, str) and value for value in (session_id, run_id, tool_call_id)
        ):
            raise SandboxError(SandboxErrorCode.CONFIG_INVALID, "调用缺少 session/run/tool 身份")
        return NativeInvocationTarget(
            self,
            InvocationIdentity(
                self.workspace, session_id, run_id, tool_call_id, read_only, tuple(denied_paths)
            ),
        )

    def authorize_process(self, process_id: str, *, session_id: str, run_id: str) -> None:
        managed = self._managed_processes.get(process_id)
        if (
            managed is None
            or (managed.launch is not None and managed.launch.metadata.get("purpose") == "worker")
            or (managed.spec.session_id, managed.spec.run_id)
            != (
                session_id,
                run_id,
            )
        ):
            raise SandboxError(
                SandboxErrorCode.POLICY_DENIED, "进程不属于当前 session/run", stage="ownership"
            )

    def discard_scope(self, scope: ExecutionScope) -> None:
        self._worker_calls.discard(scope)
        resolved = self._bindings.get(scope)
        if resolved is not None:
            if resolved.lease is not None:
                resolved.lease.release()
            del self._bindings[scope]
            self._used_calls.add((scope.session_id, scope.run_id, scope.tool_call_id))

    def bind_scope(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_call_id: str,
        read_only: bool = False,
        denied_paths: tuple[Path, ...] = (),
    ) -> ExecutionScope:
        """Trusted host API, invoked after tool authorization, never a model argument."""
        if self._closed or self._recovery_error:
            raise SandboxError(SandboxErrorCode.UNAVAILABLE, "执行环境已关闭或存在未解决的恢复记录")
        self._check_recovery()
        if not all(
            isinstance(value, str) and value for value in (session_id, run_id, tool_call_id)
        ):
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
            self._runs[key] = _Run(owned, read_only, tuple(denied_paths))
        run = self._runs[key]
        if run.read_only != read_only or run.denied_paths != tuple(denied_paths):
            raise SandboxError(SandboxErrorCode.CONFIG_INVALID, "运行期间不能改变 Run 的权限策略")
        run.directory.identity.verify()
        resolved = resolve_policy(
            self.config,
            workspace=self.workspace,
            directories=RunDirectories(run.directory.path),
            runtime_read_roots=(
                self.runtime.root,
                *system_read_paths(),
                *self.runtime.worker.read_roots,
                *self.runtime.host.read_roots,
            ),
            loader_control_paths=self.runtime.loader.protected_write_paths,
            coordinator=self.coordinator,
            read_only=read_only,
            protected_paths=(
                *run.denied_paths,
                *self.runtime.worker.denied_roots,
                self.coordinator.root,
            ),
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

    def prepare_worker(
        self,
        scope: ExecutionScope,
        operation: str,
        arguments: dict,
        *,
        workspace_only: bool,
        output_limit: int,
    ) -> tuple[list[str], dict]:
        from bot.sandbox.client import encode_request

        resolved = self._bindings.get(scope)
        if resolved is None or self.runtime.worker is None:
            raise SandboxError(SandboxErrorCode.CONFIG_INVALID, "worker 缺少未使用的可信作用域")
        policy = resolved.policy
        request = {
            "v": 1,
            "id": uuid4().hex,
            "op": operation,
            "args": arguments,
            "context": {
                "workspace": str(policy.workspace),
                "workspace_only": workspace_only,
                "read_only": not any(
                    policy.workspace.is_relative_to(p) for p in policy.write_roots
                ),
                "denied_paths": list(map(str, policy.deny_read)),
                "max_output_bytes": output_limit,
                "network_mode": policy.network_mode,
                "allowed_domains": list(policy.allowed_domains),
            },
        }
        encode_request(request)
        self._worker_calls.add(scope)
        return [
            str(self.runtime.worker.executable),
            "-I",
            "-S",
            "-B",
            "-X",
            f"pycache_prefix={self.runtime.root / 'python-cache'}",
            str(self.runtime.root / "worker.py"),
        ], request

    def _bound_policy(self, spec: ProcessSpec) -> ResolvedSandbox:
        scope = spec.scope
        if self._recovery_error or scope is None or scope not in self._bindings:
            raise SandboxError(SandboxErrorCode.CONFIG_INVALID, "缺少可信且未使用的调用作用域")
        if (spec.session_id, spec.run_id) != (scope.session_id, scope.run_id):
            raise SandboxError(SandboxErrorCode.CONFIG_INVALID, "调用身份与作用域不一致")
        if not spec.cwd.is_relative_to(scope.workspace):
            raise SandboxError(SandboxErrorCode.CONFIG_INVALID, "工作目录超出调用工作区")
        resolved = self._bindings[scope]
        if resolved.lease is None:
            raise SandboxError(SandboxErrorCode.UNAVAILABLE, "原生调用缺少共享准入占用")
        resolved.lease.verify()
        if any(spec.cwd.is_relative_to(path) for path in resolved.policy.deny_read):
            raise SandboxError(SandboxErrorCode.CONFIG_INVALID, "工作目录位于禁止读取区域")
        return resolved

    def _prepare_launch(self, spec: ProcessSpec) -> PreparedLaunch:
        resolved = self._bound_policy(spec)
        scope = spec.scope
        inspection = self._inspect_current_runtime()
        self._state_identity.verify()
        run = self._runs[(scope.session_id, scope.run_id)]
        run.directory.identity.verify()
        config = compile_srt_config(resolved.policy)
        control = OwnedDirectory.create(Path("/tmp").resolve(), prefix="bot-srt-")
        startup = None
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
            startup = LaunchStatusPipe()
        except BaseException:
            if startup is not None:
                startup.close()
            control.remove()
            raise
        executables = dict(inspection.executables)
        argv = (
            str(executables["node"]),
            str(executables["launcher"]),
            *startup.arguments,
            str(settings),
            "--",
            *spec.argv,
        )
        del self._bindings[scope]
        self._used_calls.add((scope.session_id, scope.run_id, scope.tool_call_id))
        self._startups[scope] = startup
        self._leases[scope] = resolved.lease

        def release():
            startup.close()
            control.remove()
            resolved.lease.release()
            self._startups.pop(scope, None)
            self._leases.pop(scope, None)

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
                "denied_paths": list(map(str, run.denied_paths)),
                "executed": None,
                "launch_status": {"state": "waiting", "stage": "launcher_status"},
                "purpose": "worker" if scope in self._worker_calls else "command",
                "admission_lease": resolved.lease.record(),
                "capabilities": self.sandbox_capabilities.model_dump(mode="json"),
            },
            release=release,
            pass_fds=(startup.writer,),
            after_spawn=startup.after_spawn,
        )

    def _before_spawn(self, spec: ProcessSpec, launch: PreparedLaunch, baseline) -> None:
        from bot.sandbox.recovery import prepared

        lease = self._leases[spec.scope]
        lease.save_recovery(prepared(self, spec, launch, baseline, lease.owner))

    async def _after_start(self, process_id: str) -> None:
        managed = self._managed_process(process_id)
        startup = self._startups.get(managed.spec.scope)
        metadata = managed.launch.metadata
        try:
            from bot.sandbox.recovery import registered

            lease = self._leases[managed.spec.scope]
            try:
                lease.save_recovery(registered(lease.recovery, managed))
            except Exception as failure:
                # A registry error's pre-admission executed=False cannot be
                # reused after spawn: the target may already have dispatched.
                raise SandboxError(
                    SandboxErrorCode.LAUNCH_FAILED,
                    "根进程恢复身份写入失败，必须先回收已创建的进程",
                    stage="recovery_journal",
                    executed=None,
                ) from failure
            if startup is None:
                raise SandboxError(
                    SandboxErrorCode.LAUNCH_FAILED,
                    "缺少可信启动状态通道",
                    stage="launcher_status",
                    executed=None,
                )
            acknowledgement = await startup.wait()
            metadata["launch_status"] = {
                "state": acknowledgement.state,
                "stage": acknowledgement.stage,
            }
            metadata["executed"] = acknowledgement.executed
            if acknowledgement.state != "dispatched":
                raise SandboxError(
                    SandboxErrorCode.LAUNCH_FAILED,
                    "可信启动器未能派发目标",
                    stage=f"launcher_{acknowledgement.stage}",
                    executed=acknowledgement.executed,
                )
        except BaseException as exc:
            if isinstance(exc, SandboxError):
                metadata["launch_error"] = {
                    "reason_code": exc.code.value,
                    "stage": exc.stage,
                    "executed": exc.executed,
                }
            cleanup = asyncio.create_task(
                self.terminate_process(process_id, reason="可信启动状态失败或启动期间取消")
            )
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            try:
                cleaned = cleanup.result()
                if cleaned.status == ProcessStatus.CLEANUP_FAILED:
                    raise RuntimeError("startup cleanup incomplete")
            except Exception as failure:
                raise SandboxError(
                    SandboxErrorCode.CLEANUP_FAILED,
                    "启动失败后的清理未确认，保留资源与占用",
                    stage="launcher_cleanup",
                    executed=metadata["executed"],
                ) from failure
            # Preserve the managed hard-timeout API even if it expires while
            # the launcher is preparing the sandbox, before its acknowledgement.
            if cleaned.status == ProcessStatus.TIMED_OUT and not isinstance(
                exc, asyncio.CancelledError
            ):
                return
            raise
        finally:
            if startup is not None:
                startup.close()

    def restore_cleanups(self, records: list[dict]) -> None:
        # SQLite/event payloads are display/audit data, not process or deletion
        # authority. Discover even crashes that never emitted a cleanup event.
        from bot.sandbox.recovery import cleanup_record

        del records
        if self._recovery_checked:
            return
        try:
            restored = []
            for entry in self.coordinator.snapshots():
                # Run-private roots identify this host-selected state area;
                # workspace overlap alone is not ownership of another manager.
                if not any(
                    Path(path).is_relative_to(self.state_root)
                    for path in entry["binding"]["rules"]["write_roots"]
                ) or owner_alive(entry["owner"]):
                    continue
                reference = self.coordinator.reference(entry["binding"])
                restored.append(cleanup_record(self, entry.get("recovery"), reference))
            # Validate every journal before transferring any owner. Local's
            # restore hook rechecks resource identities before taking a lease.
            for record in restored:
                super().restore_cleanups([record])
                # A numeric session can be reused while the host was absent,
                # including after its new leader already exited. Recovery may
                # follow verified identities/parents, never that stale SID.
                self._managed_processes[record["process_id"]].scope.sid = None
                self._recovered_processes.add(record["process_id"])
            self._recovery_checked = True
        except Exception:
            self._recovery_error = True
            raise

    def _restore_launch(self, record: dict) -> PreparedLaunch:
        metadata = record["launch_metadata"]
        spec = ProcessSpec.model_validate(record["spec"])
        scope = ExecutionScope.model_validate(metadata["scope"])
        denied_paths = metadata.get("denied_paths")
        if (
            metadata["backend"] != "srt"
            or metadata["backend_version"] != self.runtime.capabilities.backend_version
            or metadata["node_version"] != self.runtime.capabilities.node_version
            or spec.scope != scope
            or (spec.session_id, spec.run_id) != (scope.session_id, scope.run_id)
            or scope.workspace != self.workspace
            or type(metadata["read_only"]) is not bool
            or not isinstance(denied_paths, list)
            or any(
                not isinstance(path, str) or not Path(path).is_absolute() for path in denied_paths
            )
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
        if not control.path.exists() and not control.path.is_symlink():
            # A crash may occur after successful control removal but before
            # lease release. Absence grants no process-cleanup conclusion.
            control.removed = True
        else:
            control.identity.verify()
        owned.identity.verify()
        key = (scope.session_id, scope.run_id)
        restored_run = _Run(owned, metadata["read_only"], tuple(map(Path, denied_paths)))
        if key in self._runs:
            current = self._runs[key]
            if (
                current.directory.identity != owned.identity
                or current.read_only != restored_run.read_only
                or current.denied_paths != restored_run.denied_paths
            ):
                raise ValueError("同一 Run 的恢复目录或策略不一致")
        lease = self.coordinator.recover(
            metadata["admission_lease"], recovery=record["recovery_journal"]
        )
        self._runs[key] = restored_run
        self._leases[scope] = lease
        self._used_calls.add((scope.session_id, scope.run_id, scope.tool_call_id))

        def release():
            control.remove()
            lease.release()
            self._leases.pop(scope, None)

        return PreparedLaunch(tuple(spec.argv), {}, metadata=metadata, release=release)

    async def probe(self, executables: list[str] | None = None) -> EnvironmentCapabilities:
        # Fixed search locations only. Availability is not proof that a command
        # is authorized or compatible with the active sandbox policy.
        path = f"{self.runtime.root / 'node/bin'}:/usr/bin:/bin:/usr/sbin:/sbin"
        return EnvironmentCapabilities(
            operating_system=platform.system().lower(),
            architecture=platform.machine().lower(),
            executables={name: shutil.which(name, path=path) for name in executables or []},
        )

    async def _before_close(self) -> None:
        await self._preflight_gate.aclose()

    async def _after_close(self) -> None:
        if self._recovery_error:
            raise RuntimeError("process_cleanup_incomplete: 沙箱恢复身份未确认，保留资源")
        if self._leases:
            raise RuntimeError("process_cleanup_incomplete: 启动或恢复资源仍有未释放的准入占用")
        for scope in tuple(self._bindings):
            self.discard_scope(scope)
        for run in self._runs.values():
            run.directory.remove()
        self._runs.clear()
        self._bindings.clear()
        self._worker_calls.clear()
