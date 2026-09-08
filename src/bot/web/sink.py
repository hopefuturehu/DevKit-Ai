"""Scoped, bounded live notifications; SQLite is the source of truth."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from bot.core.events import AgentEvent


class WebSocketEventSink:
    def __init__(self, max_queue: int = 1024) -> None:
        self.max_queue = max_queue
        self._queues: dict[asyncio.Queue[str], Callable[[AgentEvent], bool]] = {}
        self._overflow: set[asyncio.Queue[str]] = set()
        self.enrich: Callable[[AgentEvent], dict] = lambda event: event.model_dump(mode="json")

    def subscribe(self, predicate=None) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self.max_queue)
        self._queues[queue] = predicate or (lambda event: False)
        return queue

    def configure(self, queue: asyncio.Queue[str], predicate) -> None:
        self._queues[queue] = predicate
        self._overflow.discard(queue)
        while not queue.empty():
            queue.get_nowait()

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._queues.pop(queue, None)
        self._overflow.discard(queue)

    async def publish(self, event: AgentEvent) -> None:
        payload = None
        for queue, predicate in tuple(self._queues.items()):
            if queue in self._overflow or not predicate(event):
                continue
            if payload is None:
                payload = json.dumps(self.enrich(event), ensure_ascii=False)
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(json.dumps({"type": "resync_required"}))
                self._overflow.add(queue)

    @staticmethod
    def _serialize_event(event: AgentEvent) -> str:
        return event.model_dump_json()
