import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts/summarize_public_benchmarks.py"
SPEC = importlib.util.spec_from_file_location("public_benchmark_summary", SCRIPT)
summary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(summary)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def write_events(path, *events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events))


def event(kind, payload):
    return {"type": kind, "timestamp": "2026-09-08T18:00:00Z", "payload": payload}


def test_compaction_detailed_usage_is_counted_once_with_agent_usage(tmp_path):
    path = tmp_path / "events.jsonl"
    raw = {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "prompt_cache_hit_tokens": 20,
        "prompt_cache_miss_tokens": 80,
    }
    normal = event("model.usage", {"provider_metadata": {"response_id": "one", "raw_usage": raw}})
    write_events(
        path,
        normal,
        normal,
        event(
            "context.compaction.request.completed",
            {"compaction_id": "c", "request_sequence": 1, "raw_usage": raw},
        ),
        event(
            "model.usage",
            {
                "phase": "compaction",
                "compaction_id": "c",
                "input_tokens": 200,
                "output_tokens": 20,
                "compaction_usage": {"input_tokens": 100, "output_tokens": 10},
            },
        ),
    )

    result = summary.usage_metrics(path)

    assert result["input_tokens"] == 200
    assert result["output_tokens"] == 20
    assert result["model_responses_with_usage"] == 2
    assert result["estimated_cost_usd"] == pytest.approx((20 * 0.014 + 80 * 0.44 + 10 * 1.32) / 1e6)


def test_old_compaction_aggregate_preserves_tokens_with_unknown_price(tmp_path):
    path = tmp_path / "events.jsonl"
    write_events(
        path,
        event(
            "model.usage",
            {
                "phase": "compaction",
                "compaction_id": "c",
                "input_tokens": 9900,
                "output_tokens": 900,
                "compaction_usage": {"input_tokens": 100, "output_tokens": 10},
            },
        ),
    )

    result = summary.usage_metrics(path)

    assert result["input_tokens"] == 100
    assert result["output_tokens"] == 10
    assert result["estimated_cost_usd"] is None


@pytest.mark.parametrize("kind", ["terminalbench", "swebench"])
def test_timeout_retains_model_activity_without_agent_result(tmp_path, kind):
    if kind == "terminalbench":
        trial = tmp_path / "terminalbench/example-baseline-1/trial"
        write_json(
            trial / "result.json",
            {
                "finished_at": "2026-09-08T18:01:00Z",
                "exception_info": {"exception_type": "AgentTimeoutError"},
            },
        )
        events_path = trial / "agent/events.jsonl"
    else:
        trial = tmp_path / "cases/swebench/example/1"
        write_json(trial / "result.json", {"verdict": "error", "error": "Worker timed out"})
        events_path = trial / "prediction.events.jsonl"
    write_events(events_path, event("run.started", {}), event("model.response", {"step": 1}))

    suite = {"terminalbench": [], "swebench": []}
    suite[kind] = [{"id": "terminal-bench/example" if kind == "terminalbench" else "example"}]
    result = summary.summarize(tmp_path, suite)

    attempt = result["tasks"][0]["attempts"][0]
    assert attempt["verdict"] == "error"
    assert attempt["model_response_observed"] is True
    assert attempt["agent_result_available"] is False


def test_running_terminal_is_visible_with_an_incomplete_event_line(tmp_path):
    events = tmp_path / "terminalbench/example-baseline-1/trial/agent/events.jsonl"
    write_events(
        events,
        event("run.started", {}),
        event("assistant.reasoning.delta", {"step": 1, "text": "fragment"}),
    )
    with events.open("a") as stream:
        stream.write('{"type":')

    result = summary.summarize(
        tmp_path, {"terminalbench": [{"id": "terminal-bench/example"}], "swebench": []}
    )

    assert result["baseline_counts"] == {"running": 1}
    assert result["tasks"][0]["attempts"][0]["model_response_observed"] is True


def test_later_diagnostics_do_not_become_the_frozen_first_baseline(tmp_path):
    for attempt in (2, 10):
        trial = tmp_path / f"terminalbench/example-baseline-{attempt}/trial"
        write_json(
            trial / "result.json",
            {"finished_at": "2026-09-08T18:01:00Z", "verifier_result": {"rewards": {"reward": 1}}},
        )

    result = summary.summarize(
        tmp_path, {"terminalbench": [{"id": "terminal-bench/example"}], "swebench": []}
    )

    assert result["baseline_counts"] == {"not_run": 1}
    assert [a["attempt"] for a in result["tasks"][0]["attempts"]] == ["2", "10"]


@pytest.mark.parametrize(
    "statuses,reward,expected",
    [
        ([], 0, "invalid_verifier"),
        ([], 1, "invalid_verifier"),
        (["skipped"], 0, "invalid_verifier"),
        (["failed"], 0, "fail"),
    ],
)
def test_terminal_scoring_separates_collection_failure_from_failed_tests(
    tmp_path, statuses, reward, expected
):
    trial = tmp_path / "terminalbench/example-baseline-1/trial"
    write_json(
        trial / "result.json",
        {"finished_at": "2026-09-08T18:01:00Z", "verifier_result": {"rewards": {"reward": reward}}},
    )
    write_json(trial / "agent/result.json", {"status": "completed"})
    write_json(
        trial / "verifier/ctrf.json",
        {
            "results": {
                "summary": {"tests": len(statuses)},
                "tests": [{"name": "actual_assertion", "status": status} for status in statuses],
            }
        },
    )

    report = summary.summarize(
        tmp_path, {"terminalbench": [{"id": "terminal-bench/example"}], "swebench": []}
    )

    assert report["baseline_counts"] == {"pass" if reward else "fail": 1}
    assert report["baseline_scoring_counts"] == {expected: 1}
    assert report["tasks"][0]["attempts"][0]["verifier_health"]["status"] == (
        "valid" if expected == "fail" else "invalid"
    )


def test_missing_structured_report_is_unknown_instead_of_zero_tests(tmp_path):
    trial = tmp_path / "terminalbench/example-baseline-1/trial"
    write_json(
        trial / "result.json",
        {"finished_at": "2026-09-08T18:01:00Z", "verifier_result": {"rewards": {"reward": 1}}},
    )

    report = summary.summarize(
        tmp_path, {"terminalbench": [{"id": "terminal-bench/example"}], "swebench": []}
    )

    assert report["baseline_scoring_counts"] == {"pass": 1}
    assert report["tasks"][0]["attempts"][0]["verifier_health"]["status"] == "unknown"


def test_timeout_with_passing_artifact_keeps_late_terminal_event_separate(tmp_path):
    trial = tmp_path / "terminalbench/example-baseline-1/trial"
    write_json(
        trial / "result.json",
        {
            "finished_at": "2026-09-08T18:02:00Z",
            "agent_execution": {"finished_at": "2026-09-08T17:59:50Z"},
            "exception_info": {"exception_type": "AgentTimeoutError"},
            "verifier_result": {"rewards": {"reward": 1}},
        },
    )
    write_json(
        trial / "verifier/ctrf.json",
        {
            "results": {"tests": [{"name": "actual_assertion", "status": "passed"}]},
        },
    )
    write_events(
        trial / "agent/events.jsonl",
        event("model.response", {"step": 65}),
        event(
            "run.finished",
            {
                "status": "limit_reached",
                "steps": 65,
                "termination_reason": "model_output_limit",
                "input_tokens": 100,
                "output_tokens": 10,
            },
        ),
    )

    report = summary.summarize(
        tmp_path,
        {
            "terminalbench": [{"id": "terminal-bench/example"}],
            "swebench": [],
        },
    )

    attempt = report["tasks"][0]["attempts"][0]
    assert report["baseline_counts"] == {"error": 1}
    assert attempt["artifact_verdict"] == "pass"
    assert attempt["agent_result_available"] is False
    assert attempt["agent_metadata_source"] == "events.run.finished"
    assert attempt["run_status"] == "limit_reached"
    assert attempt["steps"] == 65
    assert attempt["terminal_event_after_official_stop"] is True
    # Terminal cumulative totals cannot stand in for request-level usage evidence.
    assert attempt["usage"]["input_tokens"] == 0
    assert attempt["usage"]["estimated_cost_usd"] is None


def test_partial_model_activity_does_not_invent_a_terminal_event(tmp_path):
    trial = tmp_path / "terminalbench/example-baseline-1/trial"
    write_events(trial / "agent/events.jsonl", event("model.response", {"step": 1}))

    report = summary.summarize(
        tmp_path,
        {
            "terminalbench": [{"id": "terminal-bench/example"}],
            "swebench": [],
        },
    )

    attempt = report["tasks"][0]["attempts"][0]
    assert attempt["run_status"] is None
    assert attempt["agent_metadata_source"] is None
    assert attempt["terminal_event_after_official_stop"] is None
    assert attempt["artifact_verdict"] == "pending"


def test_agent_result_file_retains_precedence_over_event_fallback(tmp_path):
    trial = tmp_path / "terminalbench/example-baseline-1/trial"
    write_json(trial / "agent/result.json", {"status": "completed", "steps": 3})
    write_events(
        trial / "agent/events.jsonl",
        event("run.finished", {"status": "failed", "steps": 2}),
    )

    report = summary.summarize(
        tmp_path,
        {
            "terminalbench": [{"id": "terminal-bench/example"}],
            "swebench": [],
        },
    )

    attempt = report["tasks"][0]["attempts"][0]
    assert attempt["agent_metadata_source"] == "agent_result"
    assert attempt["run_status"] == "completed"
    assert attempt["steps"] == 3
