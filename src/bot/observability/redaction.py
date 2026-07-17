from __future__ import annotations

import re
from typing import Any

from bot.core.events import AgentEvent
from bot.core.models import ChatMessage
from bot.tools import ToolResult


class Redactor:
    _ansi_pattern = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
    _bearer_pattern = re.compile(r"(?i)(bearer\s+)[a-z0-9._~+/=-]{8,}")
    _assignment_pattern = re.compile(
        r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*)[^\s,;]{4,}"
    )

    def __init__(self, secrets: list[str] | None = None) -> None:
        self.secrets = sorted(
            {secret for secret in secrets or [] if len(secret) >= 4}, key=len, reverse=True
        )

    def redact_text(self, text: str) -> str:
        text = self._ansi_pattern.sub("", text)
        text = "".join(
            character for character in text if character in "\n\t" or ord(character) >= 32
        )
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        text = self._bearer_pattern.sub(r"\1[REDACTED]", text)
        return self._assignment_pattern.sub(r"\1[REDACTED]", text)

    def redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact(item) for item in value)
        return value

    def redact_event(self, event: AgentEvent) -> AgentEvent:
        return event.model_copy(update={"payload": self.redact(event.payload)})

    def redact_message(self, message: ChatMessage) -> ChatMessage:
        data = self.redact(message.model_dump(mode="python"))
        return ChatMessage.model_validate(data)

    def redact_tool_result(self, result: ToolResult) -> ToolResult:
        return ToolResult.model_validate(self.redact(result.model_dump(mode="python")))
