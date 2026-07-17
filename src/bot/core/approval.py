from __future__ import annotations

from typing import Protocol

from bot.policy import PolicyDecision, ToolAction


class ApprovalHandler(Protocol):
    async def approve(self, action: ToolAction, decision: PolicyDecision) -> bool: ...


class DenyApprovalHandler:
    async def approve(self, action: ToolAction, decision: PolicyDecision) -> bool:
        return False


class AllowApprovalHandler:
    """Useful for trusted automation and deterministic tests."""

    async def approve(self, action: ToolAction, decision: PolicyDecision) -> bool:
        return True
