import importlib.util
import json
import sys
from pathlib import Path

import pytest

from bot.config.models import AppConfig

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SPEC = importlib.util.spec_from_file_location(
    "context_length_task", SCRIPTS / "run_context_length_task.py"
)
BENCH = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(SCRIPTS))
try:
    SPEC.loader.exec_module(BENCH)
finally:
    sys.path.pop(0)


def configs(monkeypatch):
    monkeypatch.setattr(BENCH, "load_config", lambda _: AppConfig())
    return {label: cfg.model_dump() for label, cfg in BENCH.configurations(Path.cwd()).items()}


def test_input_caps_are_not_silently_limited_to_128k(monkeypatch):
    values = configs(monkeypatch)
    result = BENCH.verify_configs(values)
    assert [v["hard_input_limit"] for v in result["budgets"].values()] == [128000, 256000, 512000]
    assert [v["target_input_limit"] for v in result["budgets"].values()] == [102400, 204800, 409600]
    values["512k"]["model"]["context_window_tokens"] = 131072
    with pytest.raises(ValueError, match="capped"):
        BENCH.verify_configs(values)


def test_rejects_second_experimental_variable(monkeypatch):
    values = configs(monkeypatch)
    values["256k"]["context"]["compaction_low_water_tokens"] = 80000
    with pytest.raises(ValueError, match="differ beyond"):
        BENCH.verify_configs(values)


def test_report_uses_weighted_usage_and_keeps_missing_usage(monkeypatch, tmp_path):
    monkeypatch.setattr(BENCH, "verify", lambda _: {})
    (tmp_path / "manifest.json").write_text(
        json.dumps({"order": ["128k"], "pricing": BENCH.PRICING})
    )
    trial = tmp_path / "jobs/128k/trial"
    (trial / "agent").mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps({"verifier_result": {"rewards": {"reward": 0}}}))
    events = []
    for step, (inp, hit) in enumerate([(10000, 0), (90000, 81000)], 1):
        events.append(
            {
                "id": str(step),
                "type": "model.usage",
                "timestamp": "2026-09-14T00:00:00Z",
                "payload": {
                    "step": step,
                    "provider_metadata": {
                        "raw_usage": {
                            "prompt_tokens": inp,
                            "prompt_cache_hit_tokens": hit,
                            "prompt_cache_miss_tokens": inp - hit,
                            "completion_tokens": 1,
                        }
                    },
                },
            }
        )
    events.append(
        {
            "id": "3",
            "type": "model.request.retry",
            "timestamp": "2026-09-14T00:00:01Z",
            "payload": {"step": 3},
        }
    )
    (trial / "agent/events.jsonl").write_text("\n".join(map(json.dumps, events)))
    result = BENCH.report(tmp_path)["runs"]["128k"]
    assert result["totals"]["cache_hit_rate"] == pytest.approx(0.81)
    assert result["main_excluding_first"]["cache_hit_rate"] == pytest.approx(0.90)
    assert result["totals"]["usage_missing"] == 1
    assert result["totals"]["cost_is_lower_bound"]
    assert result["main_input_peak"] == 90000
    assert result["main_by_input_length"]["128000-256000"]["cache_hit_rate"] is None
    assert result["verifier"]["rewards"]["reward"] == 0
