import hashlib

import pytest

from bot.evals.context_strategy_audit import (
    aggregate,
    analyze_events,
    request_rows,
    verify_manifest,
)

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


def test_audit_exposes_clock_gap_without_silently_replacing_reported_duration():
    started = event(
        "context.compaction.request.started", {"compaction_id": "c", "phase": "generate"}
    )
    ended = event(
        "context.compaction.request.completed",
        {"compaction_id": "c", "phase": "generate", "duration_ms": 30000, "raw_usage": RAW},
    )
    ended["timestamp"] = "2026-09-10T12:05:30Z"
    report = analyze_events([started, ended], PRICING)
    discrepancy = report["clock_discrepancies"][0]
    assert discrepancy["duration_ms"] == 30000
    assert discrepancy["utc_duration_ms"] == 330000
    assert discrepancy["clock_gap_ms"] == 300000


def test_audit_main_transport_retry_is_an_unpriced_attempt():
    report = analyze_events(
        [
            event("model.request.retry", {"step": 9, "failed_attempt": 1}),
            event("model.usage", {"step": 9, "provider_metadata": {"raw_usage": RAW}}, 1),
        ],
        PRICING,
    )
    assert report["totals"]["requests"] == 2
    assert report["totals"]["usage_missing"] == 1
    assert report["totals"]["input"] == 100
    assert report["totals"]["cost_is_lower_bound"]


@pytest.mark.parametrize("finalizer_step", [89, 90])
def test_audit_runtime_stream_failure_is_not_hidden_by_successful_finalizer(finalizer_step):
    report = analyze_events(
        [
            event("assistant.delta", {"step": 90, "text": "partial"}),
            event("assistant.delta", {"step": 90, "text": " output"}, 1),
            event("run.finalizing", {"reason_code": "runtime_error"}, 2),
            event(
                "assistant.delta",
                {"step": finalizer_step, "phase": "finalizing", "text": "done"},
                3,
            ),
            event(
                "model.usage",
                {
                    "step": finalizer_step,
                    "phase": "finalizing",
                    "provider_metadata": {"raw_usage": RAW},
                },
                4,
            ),
        ],
        PRICING,
    )
    assert report["totals"]["requests"] == 2
    assert report["totals"]["usage_missing"] == 1
    assert report["totals"]["input"] == 100
    assert report["totals"]["cost_is_lower_bound"]
    missing = [r for r in report["requests"] if not r["raw_usage"]]
    assert report["totals"]["interruption_or_retry_may_hide_usage"]
    assert [(r["phase"], r["step"]) for r in missing] == [("main", 90)]


def test_audit_streamed_retry_is_counted_once_and_completed_stream_is_priced():
    report = analyze_events(
        [
            event("assistant.delta", {"step": 9, "text": "partial"}),
            event("model.request.retry", {"step": 9, "failed_attempt": 1}, 1),
            event("assistant.delta", {"step": 9, "text": "retried"}, 2),
            event("model.usage", {"step": 9, "provider_metadata": {"raw_usage": RAW}}, 3),
        ],
        PRICING,
    )
    assert report["totals"]["requests"] == 2
    assert report["totals"]["usage_missing"] == 1
    assert report["totals"]["input"] == 100
    assert all(r["status"] != "stream_without_terminal_usage" for r in report["requests"])


@pytest.mark.parametrize("tamper", [None, "asset", "task", "config_diff", "strategy"])
def test_manifest_audit_checks_files_and_comparison_controls(tmp_path, tamper):
    def asset(name, content):
        path = tmp_path / name
        path.write_text(content)
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    configs = {
        strategy: asset(
            f"{strategy}.toml", f'[context]\ncompaction_strategy="{strategy}"\nlimit=40000\n'
        )
        for strategy in ("current", "a", "b")
    }
    task = asset("instruction.md", "frozen task")
    manifest = {
        "wheel": asset("package.whl", "frozen wheel"),
        "tokenizer": asset("tokenizer.json", "frozen vocabulary"),
        "configs": configs,
        "task": str(tmp_path),
        "task_files": {"instruction.md": task["sha256"]},
    }
    if tamper == "asset":
        (tmp_path / "package.whl").write_text("changed")
    elif tamper == "task":
        (tmp_path / "instruction.md").write_text("changed")
    elif tamper in ("config_diff", "strategy"):
        configs["b"] = asset(
            "b.toml",
            '[context]\ncompaction_strategy="a"\nlimit=40000\n'
            if tamper == "strategy"
            else '[context]\ncompaction_strategy="b"\nlimit=50000\n',
        )
    if tamper:
        with pytest.raises(ValueError):
            verify_manifest(manifest)
    else:
        assert verify_manifest(manifest) == {
            "asset_hashes": 5,
            "task_hashes": 1,
            "only_strategy_differs": True,
        }
