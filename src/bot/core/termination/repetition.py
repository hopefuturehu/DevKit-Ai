"""Bounded, persistent repetition evidence, independent of task progress epochs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bot.config.models import ProgressConfig
from bot.core.termination.identity import fingerprint


class RepeatRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_key: str
    subject: str | None = None
    resource_version: str | None = None
    count: int = Field(default=1, ge=1)
    position: int = Field(default=0, ge=0)
    reference: str | None = Field(default=None, pattern=r"^blob:[0-9a-f]{64}$")
    limited: bool = False
    warned: bool = False
    redelivered: bool = False


@dataclass(frozen=True)
class RepeatCheck:
    call_key: str
    count: int
    reference: str | None
    denied: bool


class RepeatGuard:
    def __init__(self, config: ProgressConfig) -> None:
        self.config = config
        self.records: dict[str, RepeatRecord] = {}
        self.blocked_turns = 0
        self.capacity_exhausted = False
        self._redeliveries: set[str] = set()
        self._step_denied = 0
        self._step_owned = False
        self._step_warning: dict[str, Any] | None = None
        self._step_limit: dict[str, Any] | None = None

    def limit_for(self, tool_name: str) -> int:
        limits = self.config.repeat_tool_limits
        if tool_name == "run_shell":
            return limits.get(tool_name, limits.get("run_command", self.config.repeat_limit))
        return limits.get(tool_name, self.config.repeat_limit)

    def check(
        self,
        call_key: str,
        *,
        cursor: int = 0,
        allow_redelivery: bool = False,
    ) -> RepeatCheck | None:
        record = self.records.get(call_key)
        if record is None:
            return None
        if (
            allow_redelivery
            and record.position > 0
            and record.position <= cursor
            and not record.redelivered
        ):
            self._redeliveries.add(call_key)
            return None
        if not record.limited:
            return None
        denied = self.config.repeat_guard_mode == "enforce"
        if denied:
            self._step_denied += 1
        return RepeatCheck(call_key, record.count, record.reference, denied)

    def observe(
        self,
        call_key: str,
        result_key: str,
        *,
        tool_name: str,
        eligible: bool,
        complete: bool,
        subject_key: str | None = None,
        resource_version: str | None = None,
        position: int = 0,
        reference: str | None = None,
        success: bool = True,
    ) -> bool:
        """Return whether this is new evidence (or an authorized redelivery)."""
        self._step_owned |= eligible and complete
        previous = self.records.get(call_key)
        if previous is not None and previous.limited and not complete:
            # An incomplete result cannot establish that a restricted operation
            # has acquired a different, complete observation.
            self._redeliveries.discard(call_key)
            return True
        subject = fingerprint(subject_key) if subject_key is not None else None
        version = fingerprint(resource_version) if resource_version is not None else None
        same = (
            previous is not None
            and previous.result_key == result_key
            and previous.subject == subject
            and previous.resource_version == version
        )
        # Process UUIDs are liveness subjects, not versions of completed evidence.
        if subject_key and subject_key.startswith("process:"):
            subject = None
            same = previous is not None and previous.result_key == result_key
        redelivery = call_key in self._redeliveries and success
        self._redeliveries.discard(call_key)
        if same and previous is not None:
            record = previous
            if redelivery:
                record.redelivered = True
            else:
                record.count += 1
            # Keep the delivery position being restored immutable after a grant.
            if not record.redelivered:
                record.position = position or record.position
        else:
            record = RepeatRecord(
                result_key=result_key,
                subject=subject,
                resource_version=version,
                position=position,
                reference=reference,
            )
            if call_key not in self.records and len(self.records) >= self.config.repeat_capacity:
                removable = next(
                    (key for key, item in self.records.items() if not item.limited),
                    None,
                )
                if removable is None:
                    self.capacity_exhausted = True
                    return not same
                del self.records[removable]
            self.records[call_key] = record
        if reference is not None:
            record.reference = reference
        pattern = {"tool": tool_name, "call_key": call_key, "count": record.count}
        if eligible and complete and not redelivery:
            if record.count >= self.limit_for(tool_name) and not record.limited:
                record.limited = True
                self._step_limit = pattern
            elif record.count >= self.config.repeat_warning and not record.warned:
                record.warned = True
                self._step_warning = pattern
        return not same or redelivery

    def invalidate_subject(self, subject_key: str, version: str | None = None) -> None:
        subject = fingerprint(subject_key)
        new_version = fingerprint(version) if version is not None else None
        for key, record in list(self.records.items()):
            if record.subject == subject and (
                version is None or record.resource_version != new_version
            ):
                del self.records[key]

    def finish_step(self, *, useful: bool) -> tuple[str, str, dict[str, Any]] | None:
        owned, denied = self._step_owned, self._step_denied
        warning, limit = self._step_warning, self._step_limit
        self._step_owned = False
        self._step_denied = 0
        self._step_warning = self._step_limit = None
        self._redeliveries.clear()
        if denied and not useful and limit is None:
            self.blocked_turns += 1
        else:
            self.blocked_turns = 0
        if self.config.repeat_guard_mode != "enforce":
            return None
        if self.capacity_exhausted:
            return (
                "finalize",
                "repeat_capacity_exhausted",
                {
                    "capacity": self.config.repeat_capacity,
                },
            )
        if self.blocked_turns >= self.config.repeat_blocked_turns:
            return (
                "finalize",
                "repeated_observation_after_recovery",
                {
                    "blocked_turns": self.blocked_turns,
                },
            )
        if limit:
            return "recover", "repeated_observation", limit
        if warning:
            return "warn", "repeated_observation", warning
        if owned or denied:
            return "continue", "repeat_guard_active", {"blocked_calls": denied}
        return None

    def snapshot(self) -> dict[str, Any]:
        return {
            "records": {key: value.model_dump() for key, value in self.records.items()},
            "blocked_turns": self.blocked_turns,
            "capacity_exhausted": self.capacity_exhausted,
        }

    def restore(self, state: Any) -> None:
        if not isinstance(state, dict):
            return
        try:
            records = {
                str(key): RepeatRecord.model_validate(value)
                for key, value in state.get("records", {}).items()
            }
            # Threshold/mode changes must not silently discard active limits.
            active = {key: value for key, value in records.items() if value.limited}
            if len(active) > self.config.repeat_capacity:
                self.capacity_exhausted = True
            self.records = dict(list(active.items())[: self.config.repeat_capacity])
            for key, value in records.items():
                if len(self.records) >= self.config.repeat_capacity:
                    break
                self.records.setdefault(key, value)
            self.blocked_turns = max(0, int(state.get("blocked_turns", 0)))
            self.capacity_exhausted |= bool(state.get("capacity_exhausted", False))
        except (AttributeError, TypeError, ValueError, ValidationError):
            # An unreadable restriction checkpoint must not grant executions.
            self.capacity_exhausted = True
