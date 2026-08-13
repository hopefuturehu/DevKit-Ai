"""WebSocket event sink that forwards AgentEvents to connected browser clients."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from bot.core.events import AgentEvent


class WebSocketEventSink:
    """Collects AgentEvents into an in-memory async queue for WebSocket consumption."""

    def __init__(self) -> None:
        self._queues: list[asyncio.Queue[str]] = []

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue()
        self._queues.append(q)
        return q

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        try:
            self._queues.remove(queue)
        except ValueError:
            pass

    async def publish(self, event: AgentEvent) -> None:
        payload = self._serialize_event(event)
        dead: list[asyncio.Queue[str]] = []
        for q in self._queues:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._queues.remove(q)

    @staticmethod
    def _serialize_event(event: AgentEvent) -> str:
        data: dict[str, Any] = {
            "id": event.id,
            "type": event.type.value,
            "session_id": event.session_id,
            "run_id": event.run_id,
            "sequence": event.sequence,
            "timestamp": event.timestamp.isoformat(),
            "payload": event.payload,
        }
        return json.dumps(data, ensure_ascii=False)
