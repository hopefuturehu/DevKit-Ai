"""Persistent task recovery limits, independent of progress epochs and transport retries."""

from __future__ import annotations

from time import time
from typing import Any

from bot.config.models import RecoveryConfig


class RecoveryController:
    def __init__(self, config: RecoveryConfig, state: dict[str, Any]) -> None:
        self.config = config
        self.state = state

    @property
    def active(self) -> bool:
        return self.state.get("episode_deadline") is not None

    def remaining_seconds(self) -> float | None:
        deadlines = [self.state.get("task_deadline"), self.state.get("episode_deadline")]
        known = [deadline for deadline in deadlines if deadline is not None]
        return max(0, min(known) - time()) if known else None

    def expired_reason(self) -> str | None:
        remaining = self.remaining_seconds()
        if remaining is None or remaining > 0:
            return None
        task = self.state.get("task_deadline")
        episode = self.state.get("episode_deadline")
        return (
            "recovery_timeout"
            if episode is not None and (task is None or episode < task)
            else "max_wall_time_seconds"
        )

    def can_recover(self) -> bool:
        remaining = self.remaining_seconds()
        return (
            self.config.enabled
            and (remaining is None or remaining > 0)
            and self.state["task_attempts"] < self.config.max_attempts_per_task
            and self.state["episode_attempts"] < self.config.max_attempts_per_episode
        )

    def next_attempt(self) -> dict[str, Any]:
        if not self.can_recover():
            raise ValueError("recovery_exhausted")
        state = dict(self.state)
        state["task_attempts"] += 1
        state["episode_attempts"] += 1
        if not self.active:
            state["episode_deadline"] = time() + self.config.max_episode_seconds
        state["cap_next_response"] = True
        return state

    def effective_progress(self) -> bool:
        if not self.active:
            return False
        self.state.update(episode_deadline=None, episode_attempts=0)
        return True

    @property
    def reserved_cost(self) -> float:
        return self.state.get("charged_cost", 0) + sum(self.state.get("pending_cost", {}).values())
