"""Boundary tests for historical run analysis (net duration, approvals, stops).

These cover the cases that make the statistics easy to get wrong: overlapping
approval intervals, unpaired requests, duplicate events, cancelled runs and runs
with no end time.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from bot.core.events import AgentEvent, EventType
from bot.sessions import SQLiteSessionStore
from bot.web.analysis import (
    analyze_run,
    build_approval_waits,
    detect_gaps,
    merge_intervals,
    resolve_stop_reason,
    summarize,
)
from bot.web.queries import WebQueries
from bot.web.server import sort_analyses

BASE = datetime(2026, 9, 18, 10, 0, 0, tzinfo=UTC)


def stamp(offset_seconds: float) -> str:
    return (BASE + timedelta(seconds=offset_seconds)).isoformat()


def event(kind: str, offset_seconds: float, **payload) -> dict:
    return {
        "type": kind,
        "timestamp": stamp(offset_seconds),
        "payload_json": json.dumps(payload),
    }


def run_row(**overrides) -> dict:
    row = {
        "id": "r1",
        "session_id": "s1",
        "status": "completed",
        "started_at": stamp(0),
        "completed_at": stamp(100),
        "error": None,
    }
    row.update(overrides)
    return row


# --- interval union -------------------------------------------------------


def test_merge_intervals_merges_overlap_and_adjacency():
    intervals = [
        (BASE, BASE + timedelta(seconds=10)),
        (BASE + timedelta(seconds=5), BASE + timedelta(seconds=20)),
        (BASE + timedelta(seconds=20), BASE + timedelta(seconds=30)),
    ]
    merged = merge_intervals(intervals)
    assert merged == [(BASE, BASE + timedelta(seconds=30))]


def test_merge_intervals_drops_invalid_and_keeps_disjoint():
    intervals = [
        (BASE + timedelta(seconds=10), BASE + timedelta(seconds=5)),
        (BASE, BASE + timedelta(seconds=5)),
        (BASE + timedelta(seconds=30), BASE + timedelta(seconds=40)),
    ]
    merged = merge_intervals(intervals)
    assert merged == [
        (BASE, BASE + timedelta(seconds=5)),
        (BASE + timedelta(seconds=30), BASE + timedelta(seconds=40)),
    ]


def test_overlapping_approvals_are_not_double_counted():
    """Two approvals overlapping in wall-clock time subtract only the union."""
    events = [
        event("approval.requested", 10, approval_id="a", name="run_command"),
        event("approval.requested", 15, approval_id="b", name="apply_patch"),
        event("approval.resolved", 25, approval_id="a", approved=True),
        event("approval.resolved", 30, approval_id="b", approved=True),
    ]
    analysis = analyze_run(run_row(), events)
    # Union is 10s..30s = 20s, not 15s + 15s = 30s.
    assert analysis.approval_seconds == pytest.approx(20.0)
    assert analysis.total_seconds == pytest.approx(100.0)
    assert analysis.net_seconds == pytest.approx(80.0)


def test_duplicate_approval_events_are_deduplicated():
    events = [
        event("approval.requested", 10, approval_id="a", name="run_command"),
        event("approval.requested", 10, approval_id="a", name="run_command"),
        event("approval.resolved", 20, approval_id="a", approved=True),
        event("approval.resolved", 20, approval_id="a", approved=True),
    ]
    analysis = analyze_run(run_row(), events)
    assert analysis.approval_seconds == pytest.approx(10.0)
    assert analysis.duplicate_approvals == 2
    assert analysis.unpaired_approvals == 0
    assert any("重复审批事件" in note for note in analysis.notes)


def test_unpaired_approval_is_counted_but_not_subtracted():
    """A request with no resolution has unknown wait length, not zero."""
    events = [
        event("approval.requested", 10, approval_id="a", name="run_command"),
        event("tool.completed", 40, tool_call_id="t1"),
    ]
    analysis = analyze_run(run_row(), events)
    assert analysis.unpaired_approvals == 1
    assert analysis.approval_seconds == 0.0
    assert analysis.net_seconds == pytest.approx(100.0)
    assert analysis.approval_waits[0].paired is False
    assert analysis.approval_waits[0].seconds is None
    assert any("没有配对结果" in note for note in analysis.notes)


def test_resolution_before_request_does_not_create_negative_interval():
    """A resolution that precedes its request cannot close a valid interval."""
    events = [
        event("approval.resolved", 5, approval_id="a", approved=True),
        event("approval.requested", 10, approval_id="a", name="run_command"),
    ]
    waits, unpaired, duplicates = build_approval_waits(events)
    assert unpaired == 1
    # The stray resolution is not a duplicate of a paired request, so it is not
    # counted as one; it simply contributes no interval.
    assert duplicates == 0
    assert waits[0].paired is False
    assert waits[0].seconds is None


def test_approval_union_larger_than_total_clamps_net_to_zero():
    """Clock skew must not produce a negative net duration."""
    events = [
        event("approval.requested", -50, approval_id="a", name="run_command"),
        event("approval.resolved", 150, approval_id="a", approved=True),
    ]
    analysis = analyze_run(run_row(), events)
    assert analysis.approval_seconds == pytest.approx(200.0)
    assert analysis.net_seconds == 0.0
    assert any("净耗时按 0 计" in note for note in analysis.notes)


# --- missing end time / cancellation --------------------------------------


def test_run_without_completed_at_reports_unknown_duration():
    events = [event("run.started", 0, prompt="任务")]
    analysis = analyze_run(run_row(status="running", completed_at=None), events)
    assert analysis.duration_known is False
    assert analysis.total_seconds is None
    assert analysis.net_seconds is None
    assert any("缺少结束时间" in note for note in analysis.notes)


def test_cancelled_run_keeps_measurable_duration_and_reason():
    events = [
        event("run.started", 0, prompt="任务"),
        event("approval.requested", 10, approval_id="a", name="run_command"),
        event("approval.resolved", 40, approval_id="a", approved=False),
        event("run.cancelled", 60, termination_reason="cancelled", error="运行已取消"),
    ]
    analysis = analyze_run(
        run_row(status="cancelled", completed_at=stamp(60), error="运行已取消"), events
    )
    assert analysis.stop_reason == "cancelled"
    assert analysis.total_seconds == pytest.approx(60.0)
    assert analysis.approval_seconds == pytest.approx(30.0)
    assert analysis.net_seconds == pytest.approx(30.0)


def test_cancelled_run_without_end_time_still_reports_reason():
    events = [event("run.cancelled", 30, termination_reason="cancelled")]
    analysis = analyze_run(run_row(status="cancelled", completed_at=None), events)
    assert analysis.stop_reason == "cancelled"
    assert analysis.net_seconds is None
    assert analysis.duration_known is False


# --- stop reason precedence -----------------------------------------------


def test_specific_terminal_event_wins_over_run_finished():
    events = [
        event("run.limit_reached", 50, termination_reason="max_steps", message="达到步数上限"),
        event("run.finished", 50, status="limit_reached", termination_reason="max_steps"),
    ]
    reason, detail, event_type, termination = resolve_stop_reason(events, "limit_reached", None)
    assert reason == "max_steps"
    assert event_type == "run.limit_reached"
    assert detail == "达到步数上限"
    assert termination == "max_steps"


def test_run_finished_alone_yields_termination_reason():
    events = [event("run.finished", 50, status="completed", termination_reason="completed")]
    reason, _, event_type, _ = resolve_stop_reason(events, "completed", None)
    assert reason == "completed"
    assert event_type == "run.completed"


def test_missing_terminal_events_fall_back_to_unknown():
    reason, _, event_type, _ = resolve_stop_reason([], "running", None)
    assert reason == "unknown"
    assert event_type is None


def test_terminal_status_without_events_uses_status():
    reason, _, event_type, _ = resolve_stop_reason([], "failed", "boom")
    assert reason == "failed"
    assert event_type is None


# --- unknown gaps ---------------------------------------------------------


def test_long_gap_is_reported_as_unknown_and_not_subtracted():
    events = [
        event("run.started", 0, prompt="任务"),
        event("tool.completed", 300, tool_call_id="t1"),
        event("run.finished", 320, status="completed", termination_reason="completed"),
    ]
    analysis = analyze_run(run_row(completed_at=stamp(320)), events)
    assert len(analysis.gaps) == 1
    assert analysis.gaps[0]["seconds"] == pytest.approx(300.0)
    assert analysis.gaps[0]["unknown"] is True
    # The gap is evidence-free time, so it stays inside net duration.
    assert analysis.net_seconds == pytest.approx(320.0)


def test_gap_threshold_is_configurable():
    events = [event("run.started", 0), event("tool.completed", 30)]
    assert detect_gaps(events, threshold_seconds=120) == []
    assert len(detect_gaps(events, threshold_seconds=10)) == 1


def test_trailing_gap_uses_window_end_for_unfinished_run():
    events = [event("run.started", 0)]
    gaps = detect_gaps(
        events, threshold_seconds=60, window_end=BASE + timedelta(seconds=600)
    )
    assert len(gaps) == 1
    assert gaps[0]["before_event"] is None


# --- aggregation and sorting ----------------------------------------------


def test_summary_counts_stop_reasons_and_unknown_durations():
    known = analyze_run(run_row(id="r1"), [event("run.finished", 100, status="completed")])
    unknown = analyze_run(
        run_row(id="r2", status="running", completed_at=None), [event("run.started", 0)]
    )
    cancelled = analyze_run(
        run_row(id="r3", status="cancelled", completed_at=stamp(50)),
        [event("run.cancelled", 50, termination_reason="cancelled")],
    )
    summary = summarize([known, unknown, cancelled])
    assert summary["runs"] == 3
    assert summary["duration_known"] == 2
    assert summary["duration_unknown"] == 1
    assert summary["stop_reasons"]["completed"] == 1
    assert summary["stop_reasons"]["cancelled"] == 1
    assert summary["stop_reasons"]["unknown"] == 1
    assert summary["net_seconds"]["count"] == 2


def test_sort_keeps_unknown_durations_last_in_both_directions():
    rows = [
        {"run_id": "a", "net_seconds": 30.0},
        {"run_id": "b", "net_seconds": None},
        {"run_id": "c", "net_seconds": 10.0},
    ]
    descending = sort_analyses(rows, sort="net", order="desc")
    assert [item["run_id"] for item in descending] == ["a", "c", "b"]
    ascending = sort_analyses(rows, sort="net", order="asc")
    assert [item["run_id"] for item in ascending] == ["c", "a", "b"]


def test_sort_rejects_unknown_field():
    with pytest.raises(ValueError):
        sort_analyses([], sort="bogus")


# --- integration with the durable store -----------------------------------


def publish(store, kind, *, session="s1", run="r1", sequence=1, **payload):
    event = AgentEvent(
        type=kind, session_id=session, run_id=run, sequence=sequence, payload=payload
    )
    asyncio.run(store.publish(event))
    return event


@pytest.fixture
def records(tmp_path):
    store = SQLiteSessionStore(tmp_path / ".bot/state.db")
    store.create_session(tmp_path, session_id="s1")
    store.create_session(tmp_path, session_id="s2")
    yield store, WebQueries(store, tmp_path)
    store.close()


def test_analysis_runs_spans_sessions_and_filters(records):
    store, queries = records
    store.start_run("s1", "r1")
    publish(store, EventType.RUN_STARTED, run="r1", prompt="检查配置")
    publish(store, EventType.APPROVAL_REQUESTED, run="r1", approval_id="a", name="run_command")
    publish(store, EventType.APPROVAL_RESOLVED, run="r1", approval_id="a", approved=True)
    publish(
        store, EventType.RUN_FINISHED, run="r1", status="completed", termination_reason="completed"
    )
    store.finish_run("r1", "completed")

    store.start_run("s2", "r2")
    publish(store, EventType.RUN_STARTED, session="s2", run="r2", prompt="部署服务")
    publish(store, EventType.RUN_CANCELLED, session="s2", run="r2", termination_reason="cancelled")
    store.finish_run("r2", "cancelled")

    page = queries.analysis_runs()
    assert page["total"] == 2
    by_id = {item["run_id"]: item for item in page["runs"]}
    assert by_id["r1"]["stop_reason"] == "completed"
    assert by_id["r2"]["stop_reason"] == "cancelled"
    assert by_id["r1"]["prompt"] == "检查配置"

    filtered = queries.analysis_runs(stop_reason="cancelled")
    assert [item["run_id"] for item in filtered["runs"]] == ["r2"]

    searched = queries.analysis_runs(search="部署")
    assert [item["run_id"] for item in searched["runs"]] == ["r2"]

    scoped = queries.analysis_runs(session_id="s1")
    assert [item["run_id"] for item in scoped["runs"]] == ["r1"]


def test_analysis_run_detail_exposes_approval_intervals(records):
    store, queries = records
    store.start_run("s1", "r1")
    publish(store, EventType.RUN_STARTED, run="r1", prompt="检查配置")
    publish(store, EventType.APPROVAL_REQUESTED, run="r1", approval_id="a", name="run_command")
    publish(store, EventType.APPROVAL_RESOLVED, run="r1", approval_id="a", approved=True)
    publish(
        store, EventType.RUN_FINISHED, run="r1", status="completed", termination_reason="completed"
    )
    store.finish_run("r1", "completed")

    detail = queries.analysis_run("r1")
    assert detail["approval_seconds"] > 0
    assert len(detail["approval_intervals"]) == 1
    assert detail["approval_waits"][0]["approved"] is True
    assert detail["approval_waits"][0]["tool_name"] == "run_command"
    assert detail["net_seconds"] <= detail["total_seconds"]


# --- endpoint path --------------------------------------------------------


def test_summarize_accepts_serialized_run_dicts(records):
    """Regression: the endpoint aggregates ``as_dict()`` rows, not dataclasses.

    ``queries.analysis_runs`` serializes each run before the endpoint calls
    ``summarize``; a dataclass-only implementation raised AttributeError and
    turned ``/api/analysis/runs`` into a 500.
    """
    store, queries = records
    store.start_run("s1", "r1")
    publish(store, EventType.RUN_STARTED, run="r1", prompt="检查配置")
    publish(
        store, EventType.RUN_FINISHED, run="r1", status="completed", termination_reason="completed"
    )
    store.finish_run("r1", "completed")

    store.start_run("s2", "r2")
    publish(store, EventType.RUN_STARTED, session="s2", run="r2", prompt="部署服务")
    publish(store, EventType.RUN_CANCELLED, session="s2", run="r2", termination_reason="cancelled")
    store.finish_run("r2", "cancelled")

    page = queries.analysis_runs()
    assert all(isinstance(item, dict) for item in page["runs"])

    summary = summarize(page["runs"])
    assert summary["runs"] == 2
    assert summary["stop_reasons"] == {"cancelled": 1, "completed": 1}
    assert summary["duration_known"] == 2
    assert summary["duration_unknown"] == 0
    assert summary["net_seconds"]["count"] == 2


def test_summarize_handles_mixed_dict_and_dataclass(records):
    """Both shapes must aggregate identically so callers cannot drift apart."""
    store, queries = records
    store.start_run("s1", "r1")
    publish(store, EventType.RUN_STARTED, run="r1", prompt="检查配置")
    publish(
        store, EventType.RUN_FINISHED, run="r1", status="completed", termination_reason="completed"
    )
    store.finish_run("r1", "completed")

    detail = queries.analysis_run("r1")
    from_dict = summarize([detail])
    events = queries.events("s1", run_id="r1")["events"]
    from_object = summarize([analyze_run(queries.run("r1"), events)])
    assert from_dict["stop_reasons"] == from_object["stop_reasons"]
    assert from_dict["net_seconds"] == from_object["net_seconds"]


def test_summarize_counts_unknown_duration_runs(records):
    """A run with no end time must be counted as unknown, never as zero."""
    store, queries = records
    store.start_run("s1", "r1")
    publish(store, EventType.RUN_STARTED, run="r1", prompt="长时间运行的任务")

    page = queries.analysis_runs()
    summary = summarize(page["runs"])
    assert summary["duration_unknown"] == 1
    assert summary["duration_known"] == 0
    assert summary["net_seconds"]["count"] == 0
    assert summary["net_seconds"]["sum"] is None
    assert summary["stop_reasons"] == {"unknown": 1}
