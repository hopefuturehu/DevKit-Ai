import pytest

from bot.evals.context_strategy_audit import aggregate, analyze_events, request_rows

PRICING = {
    "usd_per_million_peak": {"hit": 0.044, "miss": 1.32, "output": 3.96},
    "off_peak_multiplier": 0.5,
}
RAW = {
    "prompt_tokens": 100,
    "prompt_cache_hit_tokens": 80,
    "prompt_cache_miss_tokens": 20,
    "completion_tokens": 10,
}


def event(kind, payload, second=0):
    return {
        "id": f"{kind}-{second}",
        "type": kind,
        "timestamp": f"2026-09-10T12:00:{second:02d}Z",
        "payload": payload,
    }


def test_audit_counts_actual_summary_usage_once_and_keeps_missing_usage():
    events = [
        event("model.usage", {"provider_metadata": {"raw_usage": RAW}}),
        event("model.usage", {"phase": "compaction", "input_tokens": 1000}),
        event(
            "context.compaction.request.started", {"compaction_id": "node", "phase": "b_leaf"}, 1
        ),
        event(
            "context.compaction.request.completed",
            {"compaction_id": "node", "phase": "b_leaf", "raw_usage": RAW},
            2,
        ),
        event(
            "context.compaction.request.failed",
            {"compaction_id": "cancelled", "phase": "b_leaf", "input_tokens": 60000},
            3,
        ),
    ]
    totals = aggregate(request_rows(events), PRICING)
    assert totals["requests"] == 3 and totals["usage_missing"] == 1
    assert totals["input"] == 200 and totals["output"] == 20
    assert totals["cache_hit_rate"] == 0.8
    assert totals["normalized_peak_cost_usd"] == pytest.approx(
        2 * (80 * 0.044 + 20 * 1.32 + 10 * 3.96) / 1e6
    )
    assert totals["timestamp_priced_cost_usd"] == totals["normalized_peak_cost_usd"] / 2
    assert totals["cost_is_lower_bound"]


def test_audit_rejects_inconsistent_cache_buckets():
    with pytest.raises(ValueError, match="cache buckets"):
        aggregate(
            [{"timestamp": "2026-09-10T12:00:00Z", "raw_usage": {**RAW, "prompt_tokens": 200}}],
            PRICING,
        )


def test_audit_requires_completed_tool_roundtrip_after_publication():
    events = [
        event("context.compaction.completed", {"strategy": "b"}),
        event(
            "model.response", {"step": 9, "finish_reason": "tool_calls", "tool_call_count": 2}, 1
        ),
        event("tool.result", {"tool_call_id": "first"}, 2),
    ]
    assert not analyze_events(events, PRICING)["switches"][0]["first_continuation_completed"]
    events.append(event("tool.result", {"tool_call_id": "second"}, 3))
    assert analyze_events(events, PRICING)["switches"][0]["first_continuation_completed"]


def test_audit_inflight_summary_has_unknown_cost():
    rows = request_rows(
        [
            event(
                "context.compaction.request.started",
                {"compaction_id": "inflight", "phase": "b_leaf"},
            )
        ]
    )
    assert rows[0]["status"] == "no_terminal_event"
    assert aggregate(rows, PRICING)["usage_missing"] == 1


def test_audit_cancelled_main_stream_keeps_subtotal_and_marks_uncertainty():
    report = analyze_events(
        [
            event("model.usage", {"provider_metadata": {"raw_usage": RAW}}),
            event("run.cancelled", {"termination_reason": "cancelled"}, 1),
        ],
        PRICING,
    )
    assert report["totals"]["input"] == 100
    assert report["totals"]["cost_is_lower_bound"]
