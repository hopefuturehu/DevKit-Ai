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
    parse_timestamp,
    resolve_stop_reason,
    summarize,
)
from bot.web.queries import WebQueries
from bot.web.server import sort_analyses
from bot.web.sources import collect_runs, inspect_database

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


# --- regression: historical approval events keyed only by tool_call_id -----
#
# The real workspace database contains runs whose approval events carry only
# ``tool_call_id`` and no ``approval_id``. Pairing on ``approval_id`` alone
# silently reported zero approval waiting for those runs. These cases are
# distilled from the real event structure (payload keys and ordering), with the
# identifiers replaced.


def test_historical_events_pair_on_tool_call_id():
    """Events without ``approval_id`` must still pair, via ``tool_call_id``."""
    waits, unpaired, duplicates = build_approval_waits(
        [
            event("approval.requested", 0, tool_call_id="call-1", name="run_command"),
            event("approval.resolved", 30, tool_call_id="call-1", approved=True),
        ]
    )
    assert len(waits) == 1
    assert waits[0].paired is True
    assert waits[0].key_kind == "tool_call_id"
    assert waits[0].seconds == 30
    assert unpaired == 0
    assert duplicates == 0


def test_approval_id_is_preferred_when_both_identifiers_exist():
    """When both keys are present the explicit ``approval_id`` wins."""
    waits, _, _ = build_approval_waits(
        [
            event("approval.requested", 0, approval_id="a1", tool_call_id="call-1"),
            event("approval.resolved", 20, approval_id="a1", tool_call_id="call-1"),
        ]
    )
    assert waits[0].key_kind == "approval_id"
    assert waits[0].seconds == 20


def test_disjoint_identifier_kinds_are_reported_as_unpaired():
    """A request keyed by ``approval_id`` and a result keyed by ``tool_call_id``
    never share a value, so the wait is unknown rather than guessed."""
    waits, unpaired, _ = build_approval_waits(
        [
            event("approval.requested", 0, approval_id="a1"),
            event("approval.resolved", 30, tool_call_id="call-1"),
        ]
    )
    assert unpaired == 1
    assert waits[0].paired is False
    assert "不同的标识字段" in waits[0].reason


def test_duplicate_approval_events_with_tool_call_id_are_deduplicated():
    """Repeated request/resolution pairs count once and report the duplicates."""
    waits, unpaired, duplicates = build_approval_waits(
        [
            event("approval.requested", 0, tool_call_id="call-1"),
            event("approval.requested", 1, tool_call_id="call-1"),
            event("approval.resolved", 30, tool_call_id="call-1"),
            event("approval.resolved", 31, tool_call_id="call-1"),
        ]
    )
    assert len(waits) == 1
    assert waits[0].seconds == 30
    assert duplicates == 2
    assert unpaired == 0


def test_orphan_resolution_without_request_is_not_a_wait():
    """A resolution with no request carries no measurable interval."""
    waits, unpaired, duplicates = build_approval_waits(
        [event("approval.resolved", 30, tool_call_id="call-9")]
    )
    assert waits == []
    assert unpaired == 0
    assert duplicates == 1


def test_overlapping_approval_intervals_are_merged_once():
    """Two overlapping waits must not subtract the same second twice."""
    waits, _, _ = build_approval_waits(
        [
            event("approval.requested", 0, tool_call_id="call-1"),
            event("approval.resolved", 60, tool_call_id="call-1"),
            event("approval.requested", 30, tool_call_id="call-2"),
            event("approval.resolved", 120, tool_call_id="call-2"),
        ]
    )
    spans = [
        (parse_timestamp(w.requested_at), parse_timestamp(w.resolved_at))
        for w in waits
        if w.paired
    ]
    merged = merge_intervals(spans)
    assert len(merged) == 1
    assert (merged[0][1] - merged[0][0]).total_seconds() == 120


def test_unpaired_approval_never_subtracts_from_net_duration():
    """An unpaired request must leave net duration untouched and be counted."""
    analysis = analyze_run(
        run_row(),
        [
            event("run.started", 0, prompt="等待审批"),
            event("approval.requested", 10, tool_call_id="call-1"),
            event("run.completed", 100),
        ],
    )
    assert analysis.approval_seconds == 0
    assert analysis.net_seconds == 100
    assert analysis.unpaired_approvals == 1


def test_approval_wait_starting_before_run_is_measured_from_request():
    """A request recorded before the run start still yields its full wait.

    The contract measures the union of request->resolution intervals; it does not
    clip them to the run window. Only ``net_seconds`` is clamped at zero, so a
    wait longer than the run must not produce a negative net duration.
    """
    analysis = analyze_run(
        run_row(started_at=stamp(0), completed_at=stamp(100)),
        [
            event("run.started", 0, prompt="边界"),
            event("approval.requested", -50, tool_call_id="call-1"),
            event("approval.resolved", 40, tool_call_id="call-1"),
            event("run.completed", 100),
        ],
    )
    assert analysis.approval_seconds == 90
    assert analysis.net_seconds == 10


def test_approval_wait_longer_than_run_clamps_net_to_zero():
    """A wait exceeding the run must clamp net at zero and say so."""
    analysis = analyze_run(
        run_row(started_at=stamp(0), completed_at=stamp(100)),
        [
            event("run.started", 0, prompt="边界"),
            event("approval.requested", 10, tool_call_id="call-1"),
            event("approval.resolved", 500, tool_call_id="call-1"),
            event("run.completed", 100),
        ],
    )
    assert analysis.approval_seconds == 490
    assert analysis.net_seconds == 0
    assert any("净耗时按 0 计" in note for note in analysis.notes)


# --- regression: stop reason for legacy terminal events --------------------


def test_limit_reached_status_beats_legacy_run_failed_event():
    """``runs.status=limit_reached`` with a legacy ``run.failed`` event must not
    be reported as a plain failure, and the conflict must stay visible."""
    reason, detail, event_type, _ = resolve_stop_reason(
        [event("run.failed", 50, error="达到最大步骤数 30")],
        "limit_reached",
        "达到最大步骤数 30",
    )
    assert reason == "limit_reached"
    assert event_type == "run.failed"
    assert detail == "达到最大步骤数 30"


def test_cancelled_status_beats_legacy_run_failed_event():
    reason, _, event_type, _ = resolve_stop_reason(
        [event("run.failed", 50, error="运行已取消")], "cancelled", "运行已取消"
    )
    assert reason == "cancelled"
    assert event_type == "run.failed"


def test_genuine_failure_still_reports_failed():
    """A real failure must not be reclassified by the compatibility rule."""
    reason, detail, event_type, _ = resolve_stop_reason(
        [event("run.failed", 50, error="boom")], "failed", "boom"
    )
    assert reason == "failed"
    assert event_type == "run.failed"
    assert detail == "boom"


def test_specific_terminal_event_beats_status_conflict():
    """An explicit ``run.limit_reached`` event is stronger than the runs row."""
    reason, _, event_type, _ = resolve_stop_reason(
        [
            event("run.limit_reached", 50, termination_reason="max_steps"),
            event("run.failed", 50, error="达到步数上限"),
        ],
        "limit_reached",
        None,
    )
    assert reason == "max_steps"
    assert event_type == "run.limit_reached"


# --- regression: sort the whole set before paginating ----------------------


def test_sort_is_stable_for_equal_values():
    """Equal durations must keep a deterministic order so paging cannot drop or
    duplicate a run."""
    rows = [
        {"run_id": "z", "net_seconds": 7.0},
        {"run_id": "a", "net_seconds": 7.0},
        {"run_id": "m", "net_seconds": 7.0},
    ]
    assert [r["run_id"] for r in sort_analyses(rows, sort="net", order="desc")] == ["z", "m", "a"]
    assert [r["run_id"] for r in sort_analyses(rows, sort="net", order="asc")] == ["a", "m", "z"]


def test_sort_keeps_unknown_last_in_both_directions():
    rows = [
        {"run_id": "a", "net_seconds": 10.0},
        {"run_id": "b", "net_seconds": None},
        {"run_id": "c", "net_seconds": 5.0},
    ]
    assert [r["run_id"] for r in sort_analyses(rows, sort="net", order="desc")] == ["a", "c", "b"]
    assert [r["run_id"] for r in sort_analyses(rows, sort="net", order="asc")] == ["c", "a", "b"]


def test_analysis_all_runs_sorts_before_paging(records):
    """A long run that started early must rank first, not fall outside page one."""
    store, queries = records
    # r1 starts first and runs long; r2 starts later and is short.
    store.start_run("s1", "r1")
    publish(store, EventType.RUN_STARTED, run="r1", prompt="长任务")
    publish(store, EventType.RUN_FINISHED, run="r1", status="completed")
    store.finish_run("r1", "completed")
    store.start_run("s2", "r2")
    publish(store, EventType.RUN_STARTED, session="s2", run="r2", prompt="短任务")
    publish(store, EventType.RUN_FINISHED, session="s2", run="r2", status="completed")
    store.finish_run("r2", "completed")

    items = queries.analysis_all_runs()
    ordered = sort_analyses(items, sort="net", order="desc")
    # Page one of size 1 must contain the top-ranked run, not the newest one.
    page = ordered[0:1]
    assert len(page) == 1
    assert page[0]["run_id"] == ordered[0]["run_id"]


def test_paging_covers_every_run_without_duplicates(records):
    store, queries = records
    for index in range(5):
        session = f"page-{index}"
        run = f"page-run-{index}"
        store.create_session(store.path.parent, session_id=session)
        store.start_run(session, run)
        publish(store, EventType.RUN_STARTED, session=session, run=run, prompt=f"任务{index}")
        publish(store, EventType.RUN_FINISHED, session=session, run=run, status="completed")
        store.finish_run(run, "completed")

    items = queries.analysis_all_runs()
    ordered = sort_analyses(items, sort="net", order="desc")
    seen: list[str] = []
    offset = 0
    while offset < len(ordered):
        seen.extend(item["run_id"] for item in ordered[offset : offset + 2])
        offset += 2
    assert len(seen) == len(ordered)
    assert len(set(seen)) == len(seen)


# --- regression: multi-source import, de-duplication and isolation ---------


def _make_source_db(path, runs, *, events=None, corrupt=False):
    """Create a minimal history database shaped like the real one."""
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE runs (id TEXT PRIMARY KEY, session_id TEXT, status TEXT,
            started_at TEXT, completed_at TEXT, error TEXT,
            input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL)"""
    )
    connection.execute(
        """CREATE TABLE events (id TEXT, session_id TEXT, run_id TEXT, sequence INTEGER,
            type TEXT, timestamp TEXT, payload_json TEXT, schema_version INTEGER)"""
    )
    for run in runs:
        connection.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?)",
            (
                run["id"],
                run.get("session_id", "s1"),
                run.get("status", "completed"),
                run.get("started_at", stamp(0)),
                run.get("completed_at", stamp(100)),
                run.get("error"),
                0,
                0,
                None,
            ),
        )
    for item in events or []:
        connection.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
            (
                item.get("id", "e1"),
                item.get("session_id", "s1"),
                item["run_id"],
                item.get("sequence", 1),
                item["type"],
                item.get("timestamp", stamp(0)),
                json.dumps(item.get("payload", {})),
                1,
            ),
        )
    connection.commit()
    if corrupt:
        # Truncate the file so SQLite reports a malformed image on read.
        connection.close()
        data = path.read_bytes()
        path.write_bytes(data[: len(data) // 3])
        return
    connection.close()


def test_collect_runs_deduplicates_same_run_across_sources(tmp_path):
    """The same run id in two databases counts once, and both sources are kept."""
    _make_source_db(
        tmp_path / ".bot" / "state.db",
        [{"id": "shared"}],
        events=[{"run_id": "shared", "type": "run.completed"}],
    )
    _make_source_db(
        tmp_path / ".bot" / "benchmarks" / "eval-a" / "state.db",
        [{"id": "shared"}],
        events=[{"run_id": "shared", "type": "run.completed"}],
    )
    deduped, problems, summary = collect_runs(tmp_path)
    assert problems == []
    assert summary["raw_records"] == 2
    assert summary["deduped_records"] == 1
    assert summary["excluded_duplicates"] == 1
    entry = deduped[0]
    assert entry.chosen.source_kind == "main"
    assert len(entry.duplicates) == 1


def test_collect_runs_reports_conflicting_copies(tmp_path):
    """Copies that disagree are flagged instead of being silently merged."""
    _make_source_db(
        tmp_path / ".bot" / "state.db",
        [{"id": "shared", "started_at": stamp(0), "completed_at": stamp(100)}],
        events=[{"run_id": "shared", "type": "run.completed"}],
    )
    _make_source_db(
        tmp_path / ".bot" / "benchmarks" / "eval-a" / "state.db",
        [{"id": "shared", "started_at": stamp(500), "completed_at": stamp(900)}],
        events=[{"run_id": "shared", "type": "run.completed"}],
    )
    deduped, _, summary = collect_runs(tmp_path)
    assert summary["conflicts"] == 1
    assert deduped[0].conflict is True
    assert deduped[0].conflict_reason


def test_corrupt_database_does_not_block_other_sources(tmp_path):
    """One unreadable database must not take the healthy sources down with it."""
    _make_source_db(
        tmp_path / ".bot" / "state.db",
        [{"id": "good"}],
        events=[{"run_id": "good", "type": "run.completed"}],
    )
    _make_source_db(
        tmp_path / ".bot" / "benchmarks" / "broken" / "state.db",
        [{"id": "bad"}],
        events=[{"run_id": "bad", "type": "run.completed"}],
        corrupt=True,
    )
    deduped, problems, summary = collect_runs(tmp_path)
    assert len(problems) == 1
    assert summary["sources_failed"] == 1
    assert summary["sources_loaded"] == 1
    assert [entry.run_id for entry in deduped] == ["good"]


def test_schema_incompatible_database_is_reported(tmp_path):
    """A database without the expected columns is skipped with a clear reason."""
    import sqlite3

    path = tmp_path / ".bot" / "benchmarks" / "old" / "state.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE runs (id TEXT)")
    connection.commit()
    connection.close()

    copies, problem = inspect_database(path, "benchmark")
    assert copies == []
    assert problem is not None
    assert "缺少字段" in problem.reason


def test_source_databases_are_never_modified(tmp_path):
    """Discovery must open every database read-only and leave it byte-identical."""
    import hashlib

    path = tmp_path / ".bot" / "state.db"
    _make_source_db(path, [{"id": "r1"}], events=[{"run_id": "r1", "type": "run.completed"}])
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    collect_runs(tmp_path)
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    assert before == after


def test_analysis_all_runs_filters_by_source_kind(tmp_path):
    """Main and benchmark runs must be distinguishable and filterable."""
    _make_source_db(
        tmp_path / ".bot" / "state.db",
        [{"id": "main-run"}],
        events=[{"run_id": "main-run", "type": "run.completed"}],
    )
    _make_source_db(
        tmp_path / ".bot" / "benchmarks" / "eval-a" / "state.db",
        [{"id": "bench-run"}],
        events=[{"run_id": "bench-run", "type": "run.completed"}],
    )
    store = SQLiteSessionStore(tmp_path / ".bot" / "state.db")
    queries = WebQueries(store, tmp_path)
    try:
        everything = queries.analysis_all_runs()
        assert {item["run_id"] for item in everything} == {"main-run", "bench-run"}
        benchmarks = queries.analysis_all_runs(source_kind="benchmark")
        assert [item["run_id"] for item in benchmarks] == ["bench-run"]
        assert benchmarks[0]["source_kind"] == "benchmark"
    finally:
        store.close()


def test_run_without_end_time_is_unknown_not_zero(tmp_path):
    """A historical run with no ``completed_at`` must report unknown duration."""
    _make_source_db(
        tmp_path / ".bot" / "state.db",
        [{"id": "open-run", "completed_at": None, "status": "running"}],
        events=[{"run_id": "open-run", "type": "run.started", "payload": {"prompt": "未结束"}}],
    )
    store = SQLiteSessionStore(tmp_path / ".bot" / "state.db")
    queries = WebQueries(store, tmp_path)
    try:
        items = queries.analysis_all_runs()
        assert len(items) == 1
        assert items[0]["total_seconds"] is None
        assert items[0]["net_seconds"] is None
        summary = summarize(items)
        assert summary["duration_unknown"] == 1
    finally:
        store.close()


def test_analysis_run_any_finds_run_outside_main_database(tmp_path):
    """A benchmark run must be inspectable even though it is not in the main db."""
    _make_source_db(
        tmp_path / ".bot" / "state.db",
        [{"id": "main-run"}],
        events=[{"run_id": "main-run", "type": "run.completed"}],
    )
    _make_source_db(
        tmp_path / ".bot" / "benchmarks" / "eval-a" / "state.db",
        [{"id": "bench-run"}],
        events=[{"run_id": "bench-run", "type": "run.completed"}],
    )
    store = SQLiteSessionStore(tmp_path / ".bot" / "state.db")
    queries = WebQueries(store, tmp_path)
    try:
        detail = queries.analysis_run_any("bench-run")
        assert detail["run_id"] == "bench-run"
        assert detail["source_kind"] == "benchmark"
    finally:
        store.close()

