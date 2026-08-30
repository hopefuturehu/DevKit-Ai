import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bot.evals.terminalbench_archive import archive_job, find_latest_job


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _make_job(tmp_path: Path, *, secret_value: str | None = None) -> Path:
    job = tmp_path / "jobs" / "2026-07-30__12-00-00"
    wheel = tmp_path / "agent.whl"
    config = tmp_path / "bot-config.toml"
    wheel.write_bytes(b"wheel")
    config.write_text("model = 'example'\n", encoding="utf-8")
    _write_json(
        job / "config.json",
        {
            "agents": [
                {
                    "kwargs": {
                        "package_path": str(wheel),
                        "config_path": str(config),
                    },
                    "env": {"TEST_MODEL_API_KEY": "${TEST_MODEL_API_KEY}"},
                }
            ]
        },
    )
    _write_json(
        job / "result.json",
        {
            "n_total_trials": 1,
            "finished_at": "2026-07-30T12:01:00+00:00",
            "stats": {
                "n_completed_trials": 1,
                "n_errored_trials": 0,
                "n_running_trials": 0,
                "n_pending_trials": 0,
                "n_cancelled_trials": 0,
            },
        },
    )
    trial = job / "example__abc"
    _write_json(
        trial / "result.json",
        {
            "task_name": "terminal-bench/example",
            "trial_name": "example__abc",
            "agent_result": {
                "n_input_tokens": 100,
                "n_output_tokens": 20,
                "cost_usd": 0.01,
                "metadata": {"status": "completed", "steps": 3},
            },
            "verifier_result": {"rewards": {"reward": 1.0}},
            "exception_info": None,
            "agent_execution": {
                "started_at": "2026-07-30T12:00:00+00:00",
                "finished_at": "2026-07-30T12:00:05+00:00",
            },
        },
    )
    _write_json(
        trial / "agent" / "trace" / "manifest.json",
        {
            "event_count": 10,
            "tool_count": 2,
            "reasoning_count": 3,
        },
    )
    (trial / "agent" / "trace" / "transcript.md").write_text(
        "# trace\n",
        encoding="utf-8",
    )
    events = [
        {
            "type": "model.usage",
            "sequence": 1,
            "payload": {"turn_usage": {"prompt_tokens": 120}},
        },
        {
            "type": "context.compaction.request.started",
            "sequence": 2,
            "payload": {},
        },
        {
            "type": "context.compaction.request.completed",
            "sequence": 3,
            "payload": {"input_tokens": 80, "output_tokens": 10, "cost_usd": 0.01},
        },
        {
            "type": "context.compaction.completed",
            "sequence": 4,
            "payload": {},
        },
        {"type": "model.response", "sequence": 5, "payload": {"step": 2}},
        {"type": "tool.completed", "sequence": 6, "payload": {}},
        {"type": "model.response", "sequence": 7, "payload": {"step": 3}},
    ]
    (trial / "agent" / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    (trial / "verifier").mkdir()
    (trial / "verifier" / "test-stdout.txt").write_text("ok\n", encoding="utf-8")
    (trial / "artifacts").mkdir()
    (trial / "artifacts" / "manifest.json").write_text("{}\n", encoding="utf-8")
    (trial / "artifacts" / "large.bin").write_bytes(b"large")
    if secret_value is not None:
        (trial / "agent" / "worker.log").write_text(secret_value, encoding="utf-8")
    return job


def test_archive_job_creates_timestamped_diagnostic_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_MODEL_API_KEY", "super-secret-value")
    job = _make_job(tmp_path)

    destination = archive_job(
        job,
        output_root=tmp_path / "reports",
        now=datetime(2026, 7, 30, 12, 34, 56, tzinfo=UTC),
    )

    assert destination.name == "20260730-123456+0000"
    summary = json.loads((destination / "summary.json").read_text(encoding="utf-8"))
    assert summary["aggregate"] == {
        "trials": 1,
        "passed": 1,
        "agent_failed": 0,
        "failed_verifier": 0,
        "exception": 0,
        "unscored": 0,
        "pass_rate": 1.0,
        "compacted_trials": 1,
        "compacted_passed": 1,
        "compacted_pass_rate": 1.0,
        "compactions_completed": 1,
        "compactions_failed": 0,
        "compaction_requests": 1,
        "compaction_input_tokens": 80,
        "compaction_output_tokens": 10,
        "compaction_cost_usd": 0.01,
        "mean_reward": 1.0,
        "input_tokens": 100,
        "output_tokens": 20,
        "cost_usd": 0.01,
        "archive_bytes": summary["aggregate"]["archive_bytes"],
    }
    assert summary["trials"][0]["tool_count"] == 2
    assert summary["trials"][0]["context"]["max_agent_prompt_tokens"] == 120
    assert summary["trials"][0]["context"]["model_steps"] == 2
    assert summary["trials"][0]["context"]["tool_completed"] == 1
    assert summary["trials"][0]["context"]["post_last_compaction_steps"] == 2
    assert summary["job"]["completed"] == 1
    assert summary["security"]["secret_hit_count"] == 0
    assert (destination / "SUMMARY.md").is_file()
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert {"SUMMARY.md", "summary.json"} <= {record["path"] for record in manifest["files"]}
    assert (destination / "inputs" / "agent-wheel-1.whl").read_bytes() == b"wheel"
    assert (
        destination / "snapshot" / "example__abc" / "agent" / "trace" / "transcript.md"
    ).is_file()
    assert not (destination / "snapshot" / "example__abc" / "artifacts" / "large.bin").exists()


def test_archive_job_refuses_plaintext_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "super-secret-value"
    monkeypatch.setenv("TEST_MODEL_API_KEY", secret)
    job = _make_job(tmp_path, secret_value=secret)

    with pytest.raises(RuntimeError, match="TEST_MODEL_API_KEY"):
        archive_job(
            job,
            output_root=tmp_path / "reports",
            now=datetime(2026, 7, 30, 12, 34, 56, tzinfo=UTC),
        )

    assert not (tmp_path / "reports").exists() or not any(
        path.name.startswith("20260730") for path in (tmp_path / "reports").iterdir()
    )


def test_archive_job_distinguishes_agent_failure_from_verifier_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_MODEL_API_KEY", "super-secret-value")
    job = _make_job(tmp_path)
    trial_result_path = job / "example__abc" / "result.json"
    trial_result = json.loads(trial_result_path.read_text(encoding="utf-8"))
    trial_result["agent_result"]["metadata"] = {
        "status": "failed",
        "steps": 0,
        "error": "UnicodeEncodeError",
    }
    trial_result["verifier_result"]["rewards"]["reward"] = 0.0
    _write_json(trial_result_path, trial_result)

    destination = archive_job(
        job,
        output_root=tmp_path / "reports",
        now=datetime(2026, 7, 30, 12, 34, 56, tzinfo=UTC),
    )

    summary = json.loads((destination / "summary.json").read_text(encoding="utf-8"))
    assert summary["aggregate"]["agent_failed"] == 1
    assert summary["aggregate"]["failed_verifier"] == 0
    assert summary["trials"][0]["agent_error"] == "UnicodeEncodeError"
    assert "Agent 内部失败" in (destination / "SUMMARY.md").read_text(encoding="utf-8")


def test_archive_job_recovers_steps_and_tools_from_events_after_agent_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_MODEL_API_KEY", "super-secret-value")
    job = _make_job(tmp_path)
    trial = job / "example__abc"
    result_path = trial / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["agent_result"] = None
    result["verifier_result"]["rewards"]["reward"] = 0.0
    result["exception_info"] = {
        "exception_type": "AgentTimeoutError",
        "exception_message": "Agent execution timed out after 1200.0 seconds",
    }
    _write_json(result_path, result)
    (trial / "agent" / "trace" / "manifest.json").unlink()

    destination = archive_job(
        job,
        output_root=tmp_path / "reports",
        now=datetime(2026, 7, 30, 12, 34, 56, tzinfo=UTC),
    )

    summary = json.loads((destination / "summary.json").read_text(encoding="utf-8"))
    archived_trial = summary["trials"][0]
    assert archived_trial["classification"] == "exception"
    assert archived_trial["steps"] == 2
    assert archived_trial["tool_count"] == 1
    assert archived_trial["trace_available"] is False


def test_archive_job_refuses_secret_embedded_in_harbor_config(tmp_path: Path) -> None:
    job = _make_job(tmp_path)
    config_path = job / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["agents"][0]["env"]["TEST_MODEL_API_KEY"] = "embedded-secret-value"
    _write_json(config_path, config)

    with pytest.raises(RuntimeError, match="TEST_MODEL_API_KEY"):
        archive_job(
            job,
            output_root=tmp_path / "reports",
            now=datetime(2026, 7, 30, 12, 34, 56, tzinfo=UTC),
        )


def test_find_latest_job_uses_result_modification_time(tmp_path: Path) -> None:
    first = _make_job(tmp_path)
    second = tmp_path / "jobs" / "older-name"
    _write_json(second / "result.json", {"stats": {}})
    os.utime(first / "result.json", ns=(1, 1))
    os.utime(second / "result.json", ns=(2, 2))

    assert find_latest_job(tmp_path / "jobs") == second
