"""Historical run analysis: net duration, approval waits and stop reasons.

The workbench already exposes live runs and per-run traces. This module answers a
different question: across *all* sessions in the workspace, how long did runs
actually take once human approval waiting is removed, and why did they stop?

Statistical contract (also surfaced in the UI so numbers are never ambiguous):

* ``total_seconds``  = ``completed_at - started_at`` from the ``runs`` row.
* ``approval_seconds`` = measure of the **union** of approval waiting intervals.
  Intervals are built from ``approval.requested`` -> matching
  ``approval.resolved`` events paired by ``approval_id``. Overlapping or
  duplicated intervals are merged, so the same wall-clock second is never
  subtracted twice.
* ``net_seconds`` = ``total_seconds - approval_seconds``, clamped at zero.
* Runs without ``completed_at`` (still running, or crashed before the row was
  finalized) have no measurable total. They are reported as ``unknown`` rather
  than being silently treated as zero.
* Unpaired ``approval.requested`` events (no resolution recorded) are counted and
  reported, but contribute no interval: the wait length is unknown, not zero.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Terminal event types that explain *why* a run stopped, in precedence order.
# ``run.finished`` is emitted for every run and carries the authoritative
# ``termination_reason``; the others are emitted by the finalizer for non-success
# outcomes and carry a more specific reason code.
STOP_EVENT_TYPES = (
    "run.cancelled",
    "run.blocked",
    "run.limit_reached",
    "run.failed",
    "run.completed",
    "run.finished",
)

# Statuses that mean the run reached a terminal state.
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "blocked", "limit_reached"}

# A gap between consecutive events longer than this is reported as a suspicious
# "no evidence" window. It is *not* subtracted from net duration: the absence of
# events is not proof that nothing happened.
DEFAULT_GAP_THRESHOLD_SECONDS = 120.0


class AnalysisError(ValueError):
    """Raised for invalid analysis requests (bad filters, unknown sort keys)."""


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp from the event log, tolerating ``Z``."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def merge_intervals(
    intervals: Iterable[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Merge overlapping/adjacent intervals into a minimal disjoint set.

    Invalid intervals (end before start) are dropped. The result is sorted and
    guaranteed non-overlapping, which is what makes the union measure correct
    when the same approval is requested twice or two approvals overlap.
    """
    ordered = sorted((start, end) for start, end in intervals if end > start)
    merged: list[tuple[datetime, datetime]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def interval_seconds(intervals: Iterable[tuple[datetime, datetime]]) -> float:
    return sum((end - start).total_seconds() for start, end in intervals)


@dataclass
class ApprovalWait:
    """One approval waiting interval, or an unpaired request."""

    approval_id: str
    requested_at: str
    resolved_at: str | None = None
    seconds: float | None = None
    approved: bool | None = None
    tool_name: str | None = None
    paired: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "requested_at": self.requested_at,
            "resolved_at": self.resolved_at,
            "seconds": self.seconds,
            "approved": self.approved,
            "tool_name": self.tool_name,
            "paired": self.paired,
        }


@dataclass
class RunAnalysis:
    """Computed timing/stop-reason facts for a single run."""

    run_id: str
    session_id: str
    status: str
    started_at: str | None
    completed_at: str | None
    total_seconds: float | None
    approval_seconds: float
    net_seconds: float | None
    approval_intervals: list[tuple[datetime, datetime]] = field(default_factory=list)
    approval_waits: list[ApprovalWait] = field(default_factory=list)
    unpaired_approvals: int = 0
    duplicate_approvals: int = 0
    stop_reason: str = "unknown"
    stop_reason_detail: str | None = None
    stop_event_type: str | None = None
    termination_reason: str | None = None
    error: str | None = None
    gaps: list[dict[str, Any]] = field(default_factory=list)
    event_count: int = 0
    last_event_at: str | None = None
    duration_known: bool = True
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "total_seconds": self.total_seconds,
            "approval_seconds": self.approval_seconds,
            "net_seconds": self.net_seconds,
            "approval_intervals": [
                {
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "seconds": (end - start).total_seconds(),
                }
                for start, end in self.approval_intervals
            ],
            "approval_waits": [wait.as_dict() for wait in self.approval_waits],
            "unpaired_approvals": self.unpaired_approvals,
            "duplicate_approvals": self.duplicate_approvals,
            "stop_reason": self.stop_reason,
            "stop_reason_detail": self.stop_reason_detail,
            "stop_event_type": self.stop_event_type,
            "termination_reason": self.termination_reason,
            "error": self.error,
            "gaps": self.gaps,
            "event_count": self.event_count,
            "last_event_at": self.last_event_at,
            "duration_known": self.duration_known,
            "notes": self.notes,
        }


def _payload(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def build_approval_waits(events: list[dict[str, Any]]) -> tuple[list[ApprovalWait], int, int]:
    """Pair approval requests with resolutions.

    Returns ``(waits, unpaired_count, duplicate_count)``.

    Duplicates matter because the same ``approval_id`` can legitimately appear
    more than once (retries, replays). Only the *first* request is paired with the
    *first* resolution that follows it; later repeats are counted as duplicates
    and contribute no extra interval, which keeps the union measure honest.
    """
    requests: dict[str, list[dict[str, Any]]] = {}
    resolutions: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for event in events:
        payload = _payload(event.get("payload_json"))
        approval_id = payload.get("approval_id")
        if not approval_id:
            continue
        if approval_id not in order:
            order.append(approval_id)
        if event["type"] == "approval.requested":
            requests.setdefault(approval_id, []).append(event)
        elif event["type"] == "approval.resolved":
            resolutions.setdefault(approval_id, []).append(event)

    waits: list[ApprovalWait] = []
    unpaired = 0
    duplicates = 0
    for approval_id in order:
        requested = requests.get(approval_id, [])
        resolved = resolutions.get(approval_id, [])
        if not requested:
            # A resolution without a request carries no measurable wait.
            duplicates += len(resolved)
            continue
        duplicates += max(0, len(requested) - 1) + max(0, len(resolved) - 1)
        first_request = requested[0]
        request_payload = _payload(first_request.get("payload_json"))
        start = parse_timestamp(first_request.get("timestamp"))
        # The first resolution at or after the request closes the interval.
        match = None
        for candidate in resolved:
            candidate_time = parse_timestamp(candidate.get("timestamp"))
            if candidate_time is not None and start is not None and candidate_time >= start:
                match = candidate
                break
        if match is None or start is None:
            unpaired += 1
            waits.append(
                ApprovalWait(
                    approval_id=approval_id,
                    requested_at=first_request.get("timestamp") or "",
                    tool_name=request_payload.get("name"),
                    paired=False,
                )
            )
            continue
        end = parse_timestamp(match.get("timestamp"))
        if end is None:
            unpaired += 1
            waits.append(
                ApprovalWait(
                    approval_id=approval_id,
                    requested_at=first_request.get("timestamp") or "",
                    tool_name=request_payload.get("name"),
                    paired=False,
                )
            )
            continue
        resolve_payload = _payload(match.get("payload_json"))
        waits.append(
            ApprovalWait(
                approval_id=approval_id,
                requested_at=first_request.get("timestamp") or "",
                resolved_at=match.get("timestamp"),
                seconds=max(0.0, (end - start).total_seconds()),
                approved=resolve_payload.get("approved"),
                tool_name=request_payload.get("name"),
                paired=True,
            )
        )
    return waits, unpaired, duplicates


def detect_gaps(
    events: list[dict[str, Any]],
    *,
    threshold_seconds: float = DEFAULT_GAP_THRESHOLD_SECONDS,
    window_end: datetime | None = None,
) -> list[dict[str, Any]]:
    """Report long windows with no recorded event.

    These are *unknown* windows, not idle time: the log simply has no evidence.
    They are surfaced for investigation and never subtracted from net duration.
    """
    stamps: list[tuple[datetime, str]] = []
    for event in events:
        moment = parse_timestamp(event.get("timestamp"))
        if moment is not None:
            stamps.append((moment, event["type"]))
    stamps.sort(key=lambda item: item[0])
    gaps: list[dict[str, Any]] = []
    for (previous_time, previous_type), (current_time, current_type) in zip(
        stamps, stamps[1:], strict=False
    ):
        seconds = (current_time - previous_time).total_seconds()
        if seconds >= threshold_seconds:
            gaps.append(
                {
                    "start": previous_time.isoformat(),
                    "end": current_time.isoformat(),
                    "seconds": seconds,
                    "after_event": previous_type,
                    "before_event": current_type,
                    "unknown": True,
                }
            )
    if window_end is not None and stamps:
        tail = (window_end - stamps[-1][0]).total_seconds()
        if tail >= threshold_seconds:
            gaps.append(
                {
                    "start": stamps[-1][0].isoformat(),
                    "end": window_end.isoformat(),
                    "seconds": tail,
                    "after_event": stamps[-1][1],
                    "before_event": None,
                    "unknown": True,
                }
            )
    return gaps


def resolve_stop_reason(
    events: list[dict[str, Any]], status: str, error: str | None
) -> tuple[str, str | None, str | None, str | None]:
    """Return ``(stop_reason, detail, event_type, termination_reason)``.

    ``run.finished`` is authoritative because it is emitted for every run and
    carries ``termination_reason``. More specific terminal events are preferred
    when present, since they carry the reason code chosen by the finalizer.
    """
    by_type: dict[str, dict[str, Any]] = {}
    for event in events:
        if event["type"] in STOP_EVENT_TYPES:
            by_type[event["type"]] = event

    finished = by_type.get("run.finished")
    finished_payload = _payload(finished.get("payload_json")) if finished else {}
    termination_reason = finished_payload.get("termination_reason")

    for event_type in ("run.cancelled", "run.blocked", "run.limit_reached", "run.failed"):
        event = by_type.get(event_type)
        if event is None:
            continue
        payload = _payload(event.get("payload_json"))
        reason = payload.get("termination_reason") or event_type.removeprefix("run.")
        detail = payload.get("message") or payload.get("error")
        return reason, detail, event_type, termination_reason or reason

    if by_type.get("run.completed") or (finished and finished_payload.get("status") == "completed"):
        return "completed", None, "run.completed", termination_reason or "completed"

    if finished:
        reason = termination_reason or finished_payload.get("status") or status
        detail = finished_payload.get("error")
        return reason, detail, "run.finished", termination_reason

    # No terminal event at all: fall back to the runs row, and say so.
    if status in TERMINAL_STATUSES:
        return status, error, None, None
    return "unknown", error, None, None


def analyze_run(
    run: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    gap_threshold_seconds: float = DEFAULT_GAP_THRESHOLD_SECONDS,
    now: datetime | None = None,
) -> RunAnalysis:
    """Compute timing and stop-reason facts for one run from its events."""
    started = parse_timestamp(run.get("started_at"))
    completed = parse_timestamp(run.get("completed_at"))
    status = run.get("status") or "unknown"

    waits, unpaired, duplicates = build_approval_waits(events)
    intervals = merge_intervals(
        (parse_timestamp(wait.requested_at), parse_timestamp(wait.resolved_at))
        for wait in waits
        if wait.paired and wait.resolved_at
    )
    approval_seconds = interval_seconds(intervals)

    notes: list[str] = []
    duration_known = True
    total_seconds: float | None = None
    if started is None:
        duration_known = False
        notes.append("缺少开始时间，无法计算总耗时")
    elif completed is None:
        duration_known = False
        notes.append("缺少结束时间，总耗时与净耗时记为未知")
    else:
        total_seconds = max(0.0, (completed - started).total_seconds())

    net_seconds: float | None = None
    if total_seconds is not None:
        net_seconds = max(0.0, total_seconds - approval_seconds)
        if approval_seconds > total_seconds:
            notes.append("审批等待并集超过总耗时，净耗时按 0 计")
    if unpaired:
        notes.append(f"{unpaired} 次审批请求没有配对结果，等待时长未知且未扣除")
    if duplicates:
        notes.append(f"{duplicates} 条重复审批事件已去重，未重复扣除")

    stop_reason, detail, stop_event_type, termination_reason = resolve_stop_reason(
        events, status, run.get("error")
    )

    window_end = completed
    if window_end is None and status not in TERMINAL_STATUSES:
        window_end = now
    gaps = detect_gaps(events, threshold_seconds=gap_threshold_seconds, window_end=window_end)

    last_event_at = None
    for event in events:
        stamp = event.get("timestamp")
        if stamp and (last_event_at is None or stamp > last_event_at):
            last_event_at = stamp

    return RunAnalysis(
        run_id=run["id"],
        session_id=run["session_id"],
        status=status,
        started_at=run.get("started_at"),
        completed_at=run.get("completed_at"),
        total_seconds=total_seconds,
        approval_seconds=approval_seconds,
        net_seconds=net_seconds,
        approval_intervals=intervals,
        approval_waits=waits,
        unpaired_approvals=unpaired,
        duplicate_approvals=duplicates,
        stop_reason=stop_reason,
        stop_reason_detail=detail,
        stop_event_type=stop_event_type,
        termination_reason=termination_reason,
        error=run.get("error"),
        gaps=gaps,
        event_count=len(events),
        last_event_at=last_event_at,
        duration_known=duration_known,
        notes=notes,
    )


def _field(item: RunAnalysis | dict[str, Any], name: str, default: Any = None) -> Any:
    """Read a field from either a RunAnalysis or its serialized dict form.

    The query layer serializes runs with ``as_dict()`` before the endpoint
    aggregates them, so ``summarize`` must tolerate both shapes.
    """
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def summarize(analyses: list[RunAnalysis | dict[str, Any]]) -> dict[str, Any]:
    """Aggregate stop-reason counts and duration statistics over a run set."""
    reasons: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for analysis in analyses:
        reason = _field(analysis, "stop_reason") or "unknown"
        status = _field(analysis, "status") or "unknown"
        reasons[reason] = reasons.get(reason, 0) + 1
        statuses[status] = statuses.get(status, 0) + 1

    known = [item for item in analyses if _field(item, "net_seconds") is not None]
    totals = [item for item in analyses if _field(item, "total_seconds") is not None]
    nets = [_field(item, "net_seconds") for item in known]
    approvals = [_field(item, "approval_seconds") or 0 for item in analyses]

    def _stats(values: list[float]) -> dict[str, float | None]:
        if not values:
            return {"count": 0, "sum": None, "mean": None, "min": None, "max": None}
        return {
            "count": len(values),
            "sum": sum(values),
            "mean": sum(values) / len(values),
            "min": min(values),
            "max": max(values),
        }

    return {
        "runs": len(analyses),
        "duration_known": len(known),
        "duration_unknown": len(analyses) - len(known),
        "stop_reasons": dict(sorted(reasons.items(), key=lambda item: (-item[1], item[0]))),
        "statuses": dict(sorted(statuses.items(), key=lambda item: (-item[1], item[0]))),
        "total_seconds": _stats([_field(item, "total_seconds") for item in totals]),
        "net_seconds": _stats(nets),
        "approval_seconds": _stats(approvals),
        "approval_waits": sum(len(_field(item, "approval_waits") or []) for item in analyses),
        "unpaired_approvals": sum(_field(item, "unpaired_approvals") or 0 for item in analyses),
        "duplicate_approvals": sum(_field(item, "duplicate_approvals") or 0 for item in analyses),
        "runs_with_gaps": sum(1 for item in analyses if _field(item, "gaps")),
    }


def compare_runs(left: RunAnalysis, right: RunAnalysis) -> dict[str, Any]:
    """Compare two runs on the dimensions the analysis table exposes."""

    def _delta(a: float | None, b: float | None) -> float | None:
        if a is None or b is None:
            return None
        return b - a

    return {
        "left": left.as_dict(),
        "right": right.as_dict(),
        "delta": {
            "total_seconds": _delta(left.total_seconds, right.total_seconds),
            "net_seconds": _delta(left.net_seconds, right.net_seconds),
            "approval_seconds": _delta(left.approval_seconds, right.approval_seconds),
            "event_count": right.event_count - left.event_count,
        },
        "same_stop_reason": left.stop_reason == right.stop_reason,
    }
