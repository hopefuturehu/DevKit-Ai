import copy
import importlib.util
from pathlib import Path

import pytest

from bot.evals.context_strategy_audit import aggregate

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/report_context_strategy_repeats.py"
SPEC = importlib.util.spec_from_file_location("context_strategy_repeats", SCRIPT)
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)
PRICING = {
    "usd_per_million_peak": {"hit": 0.014, "miss": 0.44, "output": 1.32},
    "off_peak_multiplier": 0.5,
}


def info(input_tokens=100, hit=90, *, success=True):
    rows = [
        {
            "timestamp": "2026-09-13T00:00:00Z",
            "phase": "main",
            "status": "completed",
            "raw_usage": {
                "prompt_tokens": input_tokens,
                "prompt_cache_hit_tokens": hit,
                "prompt_cache_miss_tokens": input_tokens - hit,
                "completion_tokens": 10,
            },
        }
    ]
    return {
        "official_test_summary": {"tests": 3, "passed": 3 if success else 0},
        "verifier": {"rewards": {"reward": float(success)}},
        "exception": None,
        "result": {"status": "completed", "termination_reason": None, "steps": 10},
        "requests": rows,
        "totals": aggregate(rows, PRICING),
        "summary_usage": aggregate([], PRICING),
        "switches": [],
        "agent_seconds": 60,
        "wall_seconds": 120,
        "publications": 0,
        "compaction_triggers": 0,
        "compaction_failures": [],
        "model_output_limit_events": [],
        "stagnation": {"dominant_command": {"count": 1}},
        "events_sha256": "events",
        "database": {},
        "main_output_peak": 10,
        "captured_source": None,
    }


def report(index):
    manifest = {
        "revision": "frozen",
        "task_files": {"task": "hash"},
        "model": "deepseek-v4-flash",
        "main_max_output_tokens": 32768,
        "summary_max_output_tokens": 8192,
        "max_steps": 240,
        "worker_wall_seconds": 1740,
        "max_cost_usd_conservative_per_run": 10,
        "pricing": PRICING,
        "concurrency": 1,
        "model_host": "api.deepseek.com",
        "wheel": {"sha256": "wheel"},
        "tokenizer": {"sha256": "tokenizer"},
        "configs": {"a": {"sha256": "config"}},
        "repetition_index": index,
    }
    return {
        "manifest": manifest,
        "complete": True,
        "manifest_sha256": str(index),
        "strategies": {"a": info()},
        "pricing": PRICING,
    }


def test_weighted_cache_and_cost_include_failed_trials():
    first, second = info(), info(900, 90, success=False)
    result = REPORT.group([first, second], PRICING)
    assert result["official_successes"] == 1 and result["trials"] == 2
    assert result["totals"]["cache_hit_rate"] == pytest.approx(0.18)
    assert result["totals"]["normalized_peak_cost_usd"] == pytest.approx(
        sum(i["totals"]["normalized_peak_cost_usd"] for i in (first, second))
    )
    assert result["cost_usd"]["count"] == 2
    assert result["successful_run_cost_usd"]["count"] == 1
    assert result["observed_total_cost_per_success_usd"] == pytest.approx(
        result["totals"]["normalized_peak_cost_usd"]
    )


def test_unknown_usage_stays_a_lower_bound_in_combined_report():
    trial = info()
    trial["totals"]["cost_is_lower_bound"] = True
    result = REPORT.group([trial], PRICING)
    assert result["totals"]["cost_is_lower_bound"]
    assert result["cost_distribution_contains_lower_bounds"]


def test_pilot_is_excluded_from_follow_up_and_missing_strategy_label_is_current():
    first, second = report(1), report(2)
    first["strategies"]["a"]["verifier"]["rewards"]["reward"] = 0
    second["strategies"]["a"]["switches"] = [
        {
            "publication": {},
            "next_main_usage": None,
            "first_continuation_completed": False,
        }
    ]
    result = REPORT.combine([first, second])["strategies"]["a"]
    assert result["all_trials"]["official_successes"] == 1
    assert result["all_trials"]["trials"] == 2
    assert result["follow_up_only"]["official_successes"] == 1
    assert result["follow_up_only"]["trials"] == 1
    assert result["follow_up_only"]["publication_paths"] == {"current": 1}


@pytest.mark.parametrize("tamper", ["config", "model", "duplicate", "incomplete"])
def test_rejects_incomparable_or_duplicate_reports(tamper):
    first, second = report(1), copy.deepcopy(report(2))
    if tamper == "config":
        second["manifest"]["configs"]["a"]["sha256"] = "changed"
    elif tamper == "model":
        second["manifest"]["model"] = "deepseek-v4-pro"
    elif tamper == "duplicate":
        second["manifest"]["repetition_index"] = 1
    else:
        second["complete"] = False
    with pytest.raises(ValueError):
        REPORT.combine([first, second])


def test_environmental_replacement_preserves_excluded_success_and_uses_new_result():
    first, second = report(1), report(2)
    replacement = report(2)
    replacement["manifest_sha256"] = "replacement"
    replacement["manifest"].update(
        replacement_reason="host sleep", replacement_for_manifest_sha256="2"
    )
    replacement["strategies"]["a"] = info(900, 90, success=False)
    result = REPORT.combine([first, second], replacements=[replacement])
    group = result["strategies"]["a"]
    assert group["all_trials"]["trials"] == 2
    assert group["all_trials"]["official_successes"] == 1
    assert group["follow_up_only"]["official_successes"] == 0
    assert group["all_trials"]["totals"]["cache_hit_rate"] == pytest.approx(0.18)
    assert result["excluded_runs"][0]["official_success"]
    assert result["runs"][1]["manifest_sha256"] == "replacement"


@pytest.mark.parametrize("tamper", ["parent", "reason", "model", "duplicate"])
def test_replacement_requires_provenance_and_same_controls(tamper):
    original = report(2)
    replacement = report(2)
    replacement["manifest"].update(
        replacement_reason="host sleep", replacement_for_manifest_sha256="2"
    )
    if tamper == "parent":
        replacement["manifest"]["replacement_for_manifest_sha256"] = "wrong"
    elif tamper == "reason":
        replacement["manifest"]["replacement_reason"] = ""
    elif tamper == "model":
        replacement["manifest"]["model"] = "deepseek-v4-pro"
    replacements = [replacement] * (2 if tamper == "duplicate" else 1)
    with pytest.raises(ValueError):
        REPORT.combine([original], replacements=replacements)
