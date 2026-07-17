from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from bot.policy import PolicyDecision, ToolAction


class ApprovalScope(StrEnum):
    ONCE = "once"
    SESSION = "session"
    ALWAYS = "always"


class ApprovalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool
    scope: ApprovalScope = ApprovalScope.ONCE


class ApprovalHandler(Protocol):
    async def approve(self, action: ToolAction, decision: PolicyDecision) -> ApprovalResponse: ...


class DenyApprovalHandler:
    async def approve(self, action: ToolAction, decision: PolicyDecision) -> ApprovalResponse:
        return ApprovalResponse(approved=False)


class AllowApprovalHandler:
    """Useful for trusted automation and deterministic tests."""

    async def approve(self, action: ToolAction, decision: PolicyDecision) -> ApprovalResponse:
        return ApprovalResponse(approved=True)
