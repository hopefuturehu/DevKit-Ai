from __future__ import annotations

from typing import TYPE_CHECKING

from bot.execution.local import LocalExecutionTarget
from bot.sandbox.models import SandboxError, SandboxErrorCode
from bot.sandbox.policy import validate_security_context

if TYPE_CHECKING:
    from bot.config.models import AppConfig


def create_execution_target(config: AppConfig) -> LocalExecutionTarget:
    sandbox = validate_security_context(config)
    if sandbox.mode == "required":
        # Keep the gate closed until command AND file/network entrypoints use
        # the native backend. Accepting required but returning Local is unsafe.
        raise SandboxError(
            SandboxErrorCode.UNAVAILABLE,
            "原生工具接入尚未完成；required 下禁止执行，也不会回退到宿主执行",
        )
    return LocalExecutionTarget(
        max_managed_processes=config.agent.max_managed_processes,
        cleanup=config.agent.process_cleanup,
    )
