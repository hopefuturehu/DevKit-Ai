"""Keep terminal interrupts inside the task that owns the interaction."""

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass

from prompt_toolkit import PromptSession


class PromptInterrupted(Exception):
    """A Ctrl+C keypress, without KeyboardInterrupt escaping an asyncio task."""


@dataclass
class InterruptState:
    handler: Callable[[], None] | None = None


interrupt_state: ContextVar[InterruptState | None] = ContextVar("cli_interrupt", default=None)


async def read_prompt(session: PromptSession[str], message: str) -> str:
    session.interrupt_exception = PromptInterrupted
    # The Runtime owns OS signals, including while prompts are opening/closing.
    return await session.prompt_async(message, handle_sigint=False)
