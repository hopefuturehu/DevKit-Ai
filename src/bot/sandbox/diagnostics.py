"""Fixed sandbox diagnostics; no model, business command or audit cleanup authority."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from bot.config.models import AppConfig
from bot.execution.native import NativeSandboxExecutionTarget
from bot.sandbox.coordination import AdmissionCoordinator, owner_alive
from bot.sandbox.models import SandboxError
from bot.sandbox.policy import validate_security_context
from bot.sandbox.resources import DirectoryIdentity
from bot.sandbox.runtime import inspect_runtime

DISPLAY_LIMIT = 256


def diagnostic_state_root(workspace: Path) -> Path:
    key = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:32]
    return Path("~/.bot/sandbox/diagnostics").expanduser().resolve() / key


def failure(exc: Exception, stage: str) -> dict:
    return {
        "code": exc.code.value if isinstance(exc, SandboxError) else "SANDBOX_DIAGNOSTIC_FAILED",
        "stage": exc.stage if isinstance(exc, SandboxError) else stage,
        "message": str(exc)[:2000],
    }


def recovery_snapshot(state_root: Path) -> dict:
    """Bounded read-only presence report, never an authenticated cleanup plan."""
    result = {
        "status": "empty",
        "directories": [],
        "leases": [],
        "errors": [],
        "cleanup_authority": False,
    }
    try:
        if state_root.exists() or state_root.is_symlink():
            identity = DirectoryIdentity.capture(state_root)
            for path in state_root.iterdir():
                if len(result["directories"]) >= DISPLAY_LIMIT:
                    raise ValueError("状态目录条目超过诊断显示上限，不能断言无遗留资源")
                result["directories"].append(
                    {
                        "path": str(path),
                        "kind": "preflight"
                        if path.name.startswith("preflight-")
                        else "run"
                        if path.name.startswith("run-")
                        else "unclassified",
                    }
                )
            identity.verify()
        for entry in AdmissionCoordinator.read_snapshots():
            if not any(
                Path(path).is_relative_to(state_root)
                for path in entry["binding"]["rules"]["write_roots"]
            ):
                continue
            if len(result["leases"]) >= DISPLAY_LIMIT:
                raise ValueError("相关占用超过诊断显示上限，不能断言清理完成")
            journal = entry.get("recovery") or {}
            # Journal values are display data here. Native recovery separately
            # validates the complete record before obtaining any authority.
            phase = journal.get("phase")
            if phase not in {"prepared", "registered", "unidentified"}:
                phase = "unverified"
            result["leases"].append(
                {
                    "id": entry["binding"]["id"],
                    "phase": phase,
                    "owner": "alive" if owner_alive(entry["owner"]) else "absent",
                }
            )
        if result["directories"] or result["leases"]:
            result["status"] = "pending"
    except Exception as exc:
        result["status"] = "unknown"
        result["errors"].append(failure(exc, "recovery_inventory"))
    return result


class SandboxDiagnostic:
    """Lifecycle compatible with the CLI's existing signal/shutdown wrapper."""

    def __init__(
        self,
        config: AppConfig,
        workspace: Path,
        *,
        probe=False,
        runtime_root: Path | None = None,
        state_root: Path | None = None,
    ):
        self.config = config
        self.workspace = workspace.resolve()
        self.runtime_root = runtime_root
        self.state_root = (
            state_root.expanduser().resolve()
            if state_root is not None
            else diagnostic_state_root(workspace)
        )
        self.probe = probe
        self.target = None
        self.cancelled = False
        self.report = {
            "schema_version": 1,
            "ok": False,
            "configured_mode": config.sandbox.mode,
            "workspace": str(self.workspace),
            "state_root": str(self.state_root),
            "state_selection": "explicit" if state_root is not None else "diagnostics_only",
            "business_execution_enabled": False,  # Production factory remains closed.
            "business_execution_attempted": False,
            "profile": "fixed disposable checks; network deny; no project command",
            "runtime": None,
            "capabilities": None,
            "probe": {"requested": probe, "status": "not_run"},
            "cleanup": {"status": "not_run"},
            "recovery_before": None,
            "recovery_after": None,
            "errors": [],
        }

    async def run(self) -> None:
        try:
            validate_security_context(self.config)
            inspection = inspect_runtime(self.workspace, root=self.runtime_root)
            self.report["runtime"] = {
                "root": str(inspection.root),
                "metadata_valid": inspection.metadata_valid,
                "errors": list(inspection.errors),
            }
            self.report["capabilities"] = inspection.capabilities.model_dump(mode="json")
            self.report["recovery_before"] = recovery_snapshot(self.state_root)
            if not self.probe or not inspection.metadata_valid:
                return
            # This explicit diagnostic uses the same backend with only its
            # fixed startup checks. No project paths, network allowlist, model
            # credentials or business argv are copied into its configuration.
            config = AppConfig.model_validate(
                {
                    "sandbox": {"mode": "required"},
                    "permissions": {"network": "deny"},
                    "agent": {"process_cleanup": self.config.agent.process_cleanup.model_dump()},
                }
            )
            self.target = NativeSandboxExecutionTarget(
                config,
                workspace=self.workspace,
                runtime_root=inspection.root,
                state_root=self.state_root,
            )
            self.report["probe"]["status"] = "running"
            await self.target.prepare_run()
            self.report["capabilities"] = self.target.sandbox_capabilities.model_dump(mode="json")
            self.report["probe"]["status"] = "passed"
        except asyncio.CancelledError:
            self.cancelled = True
            self.report["probe"]["status"] = "cancelled"
        except Exception as exc:
            self.report["probe"]["status"] = "failed" if self.probe else "not_run"
            self.report["errors"].append(failure(exc, "diagnostic_startup"))
            if self.target is not None:
                self.report["capabilities"] = self.target.sandbox_capabilities.model_dump(
                    mode="json"
                )

    async def aclose(self) -> None:
        if self.target is not None:
            try:
                await self.target.aclose()
                self.report["cleanup"]["status"] = "complete"
            except Exception as exc:
                self.report["cleanup"]["status"] = "incomplete"
                self.report["errors"].append(failure(exc, "diagnostic_cleanup"))
        self.report["recovery_after"] = (
            recovery_snapshot(self.state_root) if self.probe else self.report["recovery_before"]
        )
        state = self.report["recovery_after"]
        if self.target is not None and state["status"] != "empty":
            self.report["cleanup"]["status"] = "incomplete"
        runtime = self.report["runtime"]
        self.report["ok"] = bool(
            runtime
            and runtime["metadata_valid"]
            and not self.report["errors"]
            and state
            and state["status"] == "empty"
            and (
                not self.probe
                or self.report["probe"]["status"] == "passed"
                and self.report["cleanup"]["status"] == "complete"
            )
        )

    def exit_code(self, signal_exit: int | None = None) -> int:
        if self.report["cleanup"]["status"] == "incomplete":
            return 1
        if signal_exit is not None:
            return signal_exit
        if self.cancelled:
            return 130
        return 0 if self.report["ok"] else 1


def render_diagnostic(report: dict) -> list[str]:
    lines = [
        f"Sandbox 配置模式: {report['configured_mode']}",
        "业务执行: 未开放（固定探针通过也不会启用 required 工厂）",
        f"诊断状态目录: {report['state_root']} ({report['state_selection']})",
    ]
    runtime, capabilities = report["runtime"], report["capabilities"]
    if runtime:
        lines.append(
            f"安装记录: {'有效' if runtime['metadata_valid'] else '无效'}；{runtime['root']}"
        )
        lines.extend(runtime["errors"])
    if capabilities:
        lines.append(
            f"锁定版本: SRT={capabilities['backend_version'] or '未知'}；"
            f"Node={capabilities['node_version'] or '未知'}"
        )
        lines.append(
            "能力: "
            + ", ".join(
                f"{key}={capabilities[key]}"
                for key in (
                    "file_read",
                    "file_write",
                    "network_filter",
                    "process_isolation",
                    "resource_limits",
                )
            )
        )
    lines.append(f"固定自检: {report['probe']['status']}；清理: {report['cleanup']['status']}")
    if not report["probe"]["requested"]:
        lines.append("本次只读检查，未运行隔离探针；使用 --sandbox-probe 显式恢复并自检。")
    state = report["recovery_after"] or report["recovery_before"]
    if state:
        lines.append(
            f"恢复状态: {state['status']}；目录 {len(state['directories'])}，"
            f"占用 {len(state['leases'])}（只读清单不授予清理权限）"
        )
        lines.extend(error["message"] for error in state["errors"])
        if state["status"] != "empty":
            lines.append("仍有未确认资源；保留状态目录，用相同工作区、状态目录和锁定运行时复核。")
    lines.extend(
        f"{error['code']} / {error['stage']}: {error['message']}" for error in report["errors"]
    )
    return lines
