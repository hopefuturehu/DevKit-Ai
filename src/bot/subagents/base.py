from __future__ import annotations

from typing import Protocol

from bot.core.models import ToolCall, ToolDefinition
from bot.tools import ToolResult


class SubagentController(Protocol):
    def definitions(self) -> list[ToolDefinition]: ...

    async def start(self) -> None: ...

    async def execute(
        self,
        tool_call: ToolCall,
        *,
        parent_session_id: str,
        parent_run_id: str,
    ) -> ToolResult: ...

    async def collect_required_results(self, parent_session_id: str) -> list[dict]: ...

    async def cancel_required(self, parent_session_id: str, reason: str) -> None: ...

    def list_tasks(self, parent_session_id: str) -> list[dict]: ...

    async def shutdown(self) -> None: ...
