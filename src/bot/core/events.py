from __future__ import annotations

import json
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class EventType(StrEnum):
    RUN_STARTED = "run.started"
    ASSISTANT_DELTA = "assistant.delta"
    ASSISTANT_MESSAGE = "assistant.message"
    MODEL_USAGE = "model.usage"
    TOOL_REQUESTED = "tool.requested"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    TOOL_STARTED = "tool.started"
    TOOL_OUTPUT = "tool.output"
    TOOL_COMPLETED = "tool.completed"
    CONTEXT_COMPACTED = "context.compacted"
    RUN_STEERED = "run.steered"
    RUN_FAILED = "run.failed"
    RUN_COMPLETED = "run.completed"
    SKILL_DISCOVERED = "skill.discovered"
    SKILL_ACTIVATED = "skill.activated"
    SKILL_RESOURCE_LOADED = "skill.resource_loaded"
    SKILL_SKIPPED = "skill.skipped"
    SKILL_CONFLICT = "skill.conflict_detected"


class AgentEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex)
    schema_version: int = 1
    type: EventType
    session_id: str
    run_id: str
    sequence: int = 0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any] = Field(default_factory=dict)


class EventSink(Protocol):
    async def publish(self, event: AgentEvent) -> None: ...


class EventBus:
    def __init__(
        self,
        sinks: list[EventSink] | None = None,
        transform: Callable[[AgentEvent], AgentEvent] | None = None,
    ) -> None:
        self._sinks = list(sinks or [])
        self._sequence = 0
        self._transform = transform

    def add_sink(self, sink: EventSink) -> None:
        self._sinks.append(sink)

    async def emit(
        self,
        event_type: EventType,
        *,
        session_id: str,
        run_id: str,
        payload: dict[str, Any] | None = None,
    ) -> AgentEvent:
        self._sequence += 1
        event = AgentEvent(
            type=event_type,
            session_id=session_id,
            run_id=run_id,
            sequence=self._sequence,
            payload=payload or {},
        )
        if self._transform:
            event = self._transform(event)
        for sink in self._sinks:
            await sink.publish(event)
        return event


class JsonlEventSink:
    def __init__(self, stream: Any = None) -> None:
        self.stream = stream or sys.stdout

    async def publish(self, event: AgentEvent) -> None:
        self.stream.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n")
        self.stream.flush()


class CallbackEventSink:
    def __init__(self, callback: Callable[[AgentEvent], Awaitable[None]]) -> None:
        self.callback = callback

    async def publish(self, event: AgentEvent) -> None:
        await self.callback(event)


class MemoryEventSink:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    async def publish(self, event: AgentEvent) -> None:
        self.events.append(event)
