from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from bot.config.models import PermissionsConfig
from bot.tools.base import ToolAnnotations


class PolicyDecisionKind(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: PolicyDecisionKind
    reason: str


class ToolAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str
    arguments: dict[str, Any]
    annotations: ToolAnnotations


class DefaultPolicyEngine:
    _sensitive_names = {
        ".env",
        ".ssh",
        ".aws",
        ".config/gcloud",
        "id_rsa",
        "id_ed25519",
    }
    _safe_commands = {
        "pwd",
        "ls",
        "rg",
        "grep",
        "find",
        "head",
        "tail",
        "sed",
        "wc",
        "file",
        "uname",
        "arch",
        "git",
    }
    _always_ask_commands = {
        "rm",
        "rmdir",
        "mv",
        "chmod",
        "chown",
        "sudo",
        "su",
        "dd",
        "mkfs",
        "shutdown",
        "reboot",
        "kill",
        "pkill",
    }

    def __init__(self, config: PermissionsConfig, workspace: Path) -> None:
        self.config = config
        self.workspace = workspace.resolve()

    def evaluate(self, action: ToolAction) -> PolicyDecision:
        annotations = action.annotations
        if annotations.secret_access:
            return PolicyDecision(
                kind=PolicyDecisionKind.DENY,
                reason="Tool 声明需要访问密钥，默认拒绝",
            )
        path_decision = self._check_paths(action.arguments)
        if path_decision:
            return path_decision
        if self.config.mode == "read-only" and not annotations.read_only:
            return PolicyDecision(
                kind=PolicyDecisionKind.DENY,
                reason="当前为 read-only 模式，禁止有副作用的 Tool",
            )
        if annotations.network_access:
            if self.config.network == "deny":
                return PolicyDecision(
                    kind=PolicyDecisionKind.DENY,
                    reason="当前策略禁止网络访问",
                )
            if self.config.network == "ask":
                return PolicyDecision(
                    kind=PolicyDecisionKind.ASK,
                    reason="该 Tool 将访问网络",
                )
        if annotations.destructive:
            return PolicyDecision(
                kind=PolicyDecisionKind.ASK,
                reason="该 Tool 被标记为破坏性操作",
            )
        if action.tool_name == "run_command":
            return self._evaluate_command(action.arguments)
        return PolicyDecision(kind=PolicyDecisionKind.ALLOW, reason="符合当前安全策略")

    def _check_paths(self, arguments: dict[str, Any]) -> PolicyDecision | None:
        for key in ("path", "cwd", "output_path"):
            value = arguments.get(key)
            if not isinstance(value, str) or not value:
                continue
            raw = Path(value).expanduser()
            candidate = raw if raw.is_absolute() else self.workspace / raw
            resolved = candidate.resolve(strict=False)
            lowered = resolved.as_posix().lower()
            if any(
                lowered.endswith(f"/{name}") or f"/{name}/" in lowered
                for name in self._sensitive_names
            ):
                return PolicyDecision(
                    kind=PolicyDecisionKind.DENY,
                    reason=f"拒绝访问敏感路径: {value}",
                )
            if self.config.workspace_only:
                try:
                    resolved.relative_to(self.workspace)
                except ValueError:
                    return PolicyDecision(
                        kind=PolicyDecisionKind.DENY,
                        reason=f"路径超出工作区: {value}",
                    )
        return None

    def _evaluate_command(self, arguments: dict[str, Any]) -> PolicyDecision:
        argv = arguments.get("argv")
        if not isinstance(argv, list) or not argv or not isinstance(argv[0], str):
            return PolicyDecision(kind=PolicyDecisionKind.DENY, reason="命令 argv 不合法")
        command = Path(argv[0]).name
        if command in self._always_ask_commands:
            return PolicyDecision(
                kind=PolicyDecisionKind.ASK,
                reason=f"命令 {command} 可能修改或破坏系统状态",
            )
        if command == "git":
            subcommand = argv[1] if len(argv) > 1 else ""
            if subcommand not in {"status", "diff", "log", "show", "branch", "rev-parse"}:
                return PolicyDecision(
                    kind=PolicyDecisionKind.ASK,
                    reason=f"git {subcommand or '<none>'} 可能修改仓库状态",
                )
        if command in self._safe_commands:
            return PolicyDecision(kind=PolicyDecisionKind.ALLOW, reason="只读命令允许执行")
        if self.config.mode == "full-access":
            return PolicyDecision(kind=PolicyDecisionKind.ALLOW, reason="full-access 模式允许命令")
        return PolicyDecision(
            kind=PolicyDecisionKind.ASK,
            reason=f"命令 {command} 不在自动允许列表中",
        )
