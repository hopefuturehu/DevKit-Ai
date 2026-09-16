import asyncio
from types import SimpleNamespace

import pytest
from rich.console import Console

from bot.cli.app import _prompt_with_background_approvals
from bot.cli.interrupts import PromptInterrupted
from bot.cli.render import InteractiveApprovalHandler
from bot.policy import PolicyDecision, PolicyDecisionKind, ToolAction
from bot.tools.builtins import RunCommandTool


class PromptStub:
    def __init__(self, answer: str = "session") -> None:
        self.answer = answer

    async def prompt_async(self, prompt: str, **kwargs) -> str:
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


@pytest.mark.asyncio
async def test_interrupt_is_not_hidden_by_a_simultaneous_background_approval():
    handler = InteractiveApprovalHandler(Console(quiet=True))
    action = ToolAction(
        tool_name="run_command",
        arguments={"argv": ["make", "test"]},
        annotations=RunCommandTool.annotations,
    )
    waiter = asyncio.create_task(
        handler.approve(action, PolicyDecision(kind=PolicyDecisionKind.ASK, reason="test"))
    )

    class InterruptedPrompt:
        async def prompt_async(self, prompt, **kwargs):
            if prompt == "> ":
                raise PromptInterrupted
            pytest.fail("Ctrl+C must be handled before opening the new approval prompt")

    try:
        with pytest.raises(PromptInterrupted):
            await _prompt_with_background_approvals(
                SimpleNamespace(approval_handler=handler), InterruptedPrompt()
            )
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_approval_is_not_presented_in_the_next_run():
    handler = InteractiveApprovalHandler(Console(quiet=True))
    action = ToolAction(
        tool_name="run_command",
        arguments={"argv": ["make", "test"]},
        annotations=RunCommandTool.annotations,
    )
    decision = PolicyDecision(kind=PolicyDecisionKind.ASK, reason="test")
    old = asyncio.create_task(handler.approve(action, decision))
    await asyncio.sleep(0)
    old.cancel()
    await asyncio.gather(old, return_exceptions=True)
    current = asyncio.create_task(handler.approve(action, decision))
    try:
        async with asyncio.timeout(1):
            pending = await handler.next_request()
            assert not pending.future.done()
            await handler.resolve(pending, PromptStub("n"))
            assert not (await current).approved
    finally:
        current.cancel()
        await asyncio.gather(current, return_exceptions=True)
