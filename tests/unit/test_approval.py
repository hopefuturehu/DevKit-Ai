import asyncio

import pytest
from rich.console import Console

from bot.cli.render import InteractiveApprovalHandler
from bot.policy import PolicyDecision, PolicyDecisionKind, ToolAction
from bot.tools.builtins import RunCommandTool


class PromptStub:
    def __init__(self, answer: str = "session") -> None:
        self.answer = answer

    async def prompt_async(self, prompt: str) -> str:
        return self.answer


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "approved", "scope"),
    [
        ("", True, "once"),
        ("y", True, "once"),
        ("s", True, "session"),
        ("a", True, "always"),
        ("n", False, "once"),
    ],
)
async def test_interactive_approval_accepts_single_key_shortcuts(
    answer: str,
    approved: bool,
    scope: str,
) -> None:
    handler = InteractiveApprovalHandler(Console(quiet=True))
    action = ToolAction(
        tool_name="run_command",
        arguments={"argv": ["make", "test"]},
        annotations=RunCommandTool.annotations,
    )
    decision = PolicyDecision(kind=PolicyDecisionKind.ASK, reason="test")

    approval_task = asyncio.create_task(handler.approve(action, decision))
    pending = await handler.next_request()
    await handler.resolve(pending, PromptStub(answer))
    response = await approval_task

    assert response.approved is approved
    assert response.scope.value == scope
