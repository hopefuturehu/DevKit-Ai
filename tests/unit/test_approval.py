import asyncio

import pytest
from rich.console import Console

from bot.cli.render import InteractiveApprovalHandler
from bot.policy import PolicyDecision, PolicyDecisionKind, ToolAction
from bot.tools.builtins import RunCommandTool


class PromptStub:
    async def prompt_async(self, prompt: str) -> str:
        return "session"


@pytest.mark.asyncio
async def test_interactive_approval_is_resolved_by_shared_input_coordinator() -> None:
    handler = InteractiveApprovalHandler(Console(quiet=True))
    action = ToolAction(
        tool_name="run_command",
        arguments={"argv": ["make", "test"]},
        annotations=RunCommandTool.annotations,
    )
    decision = PolicyDecision(kind=PolicyDecisionKind.ASK, reason="test")

    approval_task = asyncio.create_task(handler.approve(action, decision))
    pending = await handler.next_request()
    await handler.resolve(pending, PromptStub())
    response = await approval_task

    assert response.approved
    assert response.scope.value == "session"
