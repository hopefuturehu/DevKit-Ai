"""Keep terminal interrupts inside the task that owns the interaction."""

import asyncio
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass

from prompt_toolkit import PromptSession
from prompt_toolkit.document import Document
from prompt_toolkit.history import InMemoryHistory


class PromptInterrupted(Exception):
    """A Ctrl+C keypress, without KeyboardInterrupt escaping an asyncio task."""


@dataclass
class InterruptState:
    handler: Callable[[], None] | None = None


interrupt_state: ContextVar[InterruptState | None] = ContextVar("cli_interrupt", default=None)


async def read_prompt(session: PromptSession[str], message: str, *, approval: bool = False) -> str:
    session.interrupt_exception = PromptInterrupted
    # The Runtime owns OS signals, including while prompts are opening/closing.
    if not isinstance(session, PromptSession):
        return await session.prompt_async(message, handle_sigint=False)
    if not approval:
        try:
            answer = await session.prompt_async(
                message,
                handle_sigint=False,
                default=getattr(session, "task_draft", ""),
            )
        except asyncio.CancelledError:
            buffer = session.default_buffer
            session.task_draft = Document(buffer.text, buffer.cursor_position)
            raise
        else:
            session.task_draft = ""
            return answer
    # Approval choices must never enter task history or inherit task suggestions.
    attributes = ("history", "completer", "auto_suggest", "multiline")
    previous = {name: getattr(session, name) for name in attributes}
    buffer_history = session.default_buffer.history
    session.approval_mode = True
    session.history = session.default_buffer.history = InMemoryHistory()
    session.completer = session.auto_suggest = None
    session.multiline = False
    try:
        return await session.prompt_async(message, handle_sigint=False)
    finally:
        session.approval_mode = False
        for name, value in previous.items():
            setattr(session, name, value)
        session.default_buffer.history = buffer_history
