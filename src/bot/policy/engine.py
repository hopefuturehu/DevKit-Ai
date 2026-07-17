from __future__ import annotations

import shlex
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
        "python",
        "python3",
        "perl",
        "ruby",
        "node",
        "curl",
        "wget",
        "ssh",
        "scp",
        "nc",
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
        if action.tool_name == "run_shell":
            return self._evaluate_shell(action.arguments)
        if action.tool_name in {"ksys", "tuner"} and action.arguments.get("workload"):
            return PolicyDecision(
                kind=PolicyDecisionKind.ASK,
                reason=f"{action.tool_name} 将启动用户指定的 workload",
            )
        return PolicyDecision(kind=PolicyDecisionKind.ALLOW, reason="符合当前安全策略")

    def _check_paths(self, arguments: dict[str, Any]) -> PolicyDecision | None:
        for value in self._iter_path_values(arguments):
            raw = Path(value).expanduser()
            candidate = raw if raw.is_absolute() else self.workspace / raw
            resolved = candidate.resolve(strict=False)
            lowered = resolved.as_posix().lower()
            path_parts = {part.lower() for part in resolved.parts}
            sensitive = any(
                name in path_parts if "/" not in name else f"/{name}/" in f"/{lowered}/"
                for name in self._sensitive_names
            )
            sensitive = sensitive or resolved.name.lower().startswith(".env")
            if sensitive:
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

    @staticmethod
    def _iter_path_values(arguments: dict[str, Any]):
        for key, value in arguments.items():
            path_key = key == "cwd" or key.endswith("path") or key.endswith("paths")
            if not path_key:
                continue
            if isinstance(value, str) and value:
                yield value
            elif isinstance(value, list):
                yield from (item for item in value if isinstance(item, str) and item)

    def _evaluate_command(self, arguments: dict[str, Any]) -> PolicyDecision:
        argv = arguments.get("argv")
        if not isinstance(argv, list) or not argv or not isinstance(argv[0], str):
            return PolicyDecision(kind=PolicyDecisionKind.DENY, reason="命令 argv 不合法")
        command = Path(argv[0]).name
        if command in {"sh", "bash", "zsh", "fish"} and "-c" in argv[1:]:
            return PolicyDecision(
                kind=PolicyDecisionKind.DENY,
                reason="run_command 禁止通过 shell -c 绕过策略，请显式使用 run_shell",
            )
        if command in self._always_ask_commands:
            return PolicyDecision(
                kind=PolicyDecisionKind.ASK,
                reason=f"命令 {command} 可能修改或破坏系统状态",
            )
        if command == "git":
            subcommand = argv[1] if len(argv) > 1 else ""
            if subcommand not in {"status", "diff", "log", "show", "rev-parse"}:
                return PolicyDecision(
                    kind=PolicyDecisionKind.ASK,
                    reason=f"git {subcommand or '<none>'} 可能修改仓库状态",
                )
        if command == "find" and any(item in argv[1:] for item in {"-delete", "-exec", "-execdir"}):
            return PolicyDecision(
                kind=PolicyDecisionKind.ASK,
                reason="find 参数可能执行命令或删除文件",
            )
        if command == "sed" and any(item == "-i" or item.startswith("-i") for item in argv[1:]):
            return PolicyDecision(
                kind=PolicyDecisionKind.ASK,
                reason="sed -i 会原地修改文件",
            )
        if command in self._safe_commands:
            return PolicyDecision(kind=PolicyDecisionKind.ALLOW, reason="只读命令允许执行")
        if self.config.mode == "full-access":
            return PolicyDecision(kind=PolicyDecisionKind.ALLOW, reason="full-access 模式允许命令")
        return PolicyDecision(
            kind=PolicyDecisionKind.ASK,
            reason=f"命令 {command} 不在自动允许列表中",
        )

    def _evaluate_shell(self, arguments: dict[str, Any]) -> PolicyDecision:
        script = arguments.get("script")
        if not isinstance(script, str) or not script.strip():
            return PolicyDecision(kind=PolicyDecisionKind.DENY, reason="Shell script 不能为空")
        if any(marker in script for marker in ("`", "$(", ">", "<")):
            return PolicyDecision(
                kind=PolicyDecisionKind.ASK,
                reason="Shell 脚本包含命令替换或重定向",
            )
        if "\n" in script or "\r" in script:
            return PolicyDecision(
                kind=PolicyDecisionKind.ASK,
                reason="多行 Shell 脚本需要显式审批",
            )
        try:
            lexer = shlex.shlex(script, posix=True, punctuation_chars=";&|")
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError as exc:
            return PolicyDecision(kind=PolicyDecisionKind.DENY, reason=f"Shell 解析失败: {exc}")
        segments: list[list[str]] = [[]]
        for token in tokens:
            if token in {";", "&&", "||", "|", "&"}:
                if not segments[-1]:
                    return PolicyDecision(
                        kind=PolicyDecisionKind.DENY,
                        reason="Shell 脚本存在空命令段",
                    )
                segments.append([])
            elif token and set(token) <= set(";&|"):
                return PolicyDecision(
                    kind=PolicyDecisionKind.DENY,
                    reason=f"不支持的 Shell 控制符: {token}",
                )
            else:
                segments[-1].append(token)
        if not segments[-1]:
            return PolicyDecision(kind=PolicyDecisionKind.DENY, reason="Shell 脚本结尾不完整")
        decisions = [self._evaluate_command({"argv": segment}) for segment in segments]
        denied = [
            decision.reason for decision in decisions if decision.kind == PolicyDecisionKind.DENY
        ]
        if denied:
            return PolicyDecision(kind=PolicyDecisionKind.DENY, reason="; ".join(denied))
        ask = [decision.reason for decision in decisions if decision.kind == PolicyDecisionKind.ASK]
        if ask:
            return PolicyDecision(kind=PolicyDecisionKind.ASK, reason="; ".join(ask))
        return PolicyDecision(kind=PolicyDecisionKind.ALLOW, reason="所有 Shell 命令段均为只读命令")
