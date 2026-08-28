from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[2] / "scripts" / "run_context_efficiency_benchmark.py"
_SPEC = importlib.util.spec_from_file_location("run_context_efficiency_benchmark", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
run_context_efficiency_benchmark = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(run_context_efficiency_benchmark)


def test_live_context_efficiency_requires_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUN_CONTEXT_EFFICIENCY_LIVE", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_context_efficiency_benchmark.py", "--provider", "live", "--repeat", "3"],
    )

    with pytest.raises(SystemExit, match="RUN_CONTEXT_EFFICIENCY_LIVE=1"):
        run_context_efficiency_benchmark.main()


@pytest.mark.parametrize(
    ("extra_args", "expected"),
    [
        ([], "至少需要 --repeat 3"),
        (["--repeat", "3", "--variants", "current"], "必须同时包含 raw 和 current"),
    ],
)
def test_live_context_efficiency_validates_comparison_shape_before_network(
    monkeypatch: pytest.MonkeyPatch,
    extra_args: list[str],
    expected: str,
) -> None:
    monkeypatch.setenv("RUN_CONTEXT_EFFICIENCY_LIVE", "1")
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_context_efficiency_benchmark.py", "--provider", "live", *extra_args],
    )

    with pytest.raises(SystemExit, match=expected):
        run_context_efficiency_benchmark.main()


def test_live_aggregate_reports_cost_and_latency_effects() -> None:
    def summary(input_tokens: int, cost: float, latency: float) -> dict:
        return {
            "metrics": {
                "input_tokens": input_tokens,
                "cost_usd": cost,
                "model_latency_seconds": latency,
                "model_request_latency_p95_seconds": latency / 2,
            },
            "quality": {"passed": True},
        }

    attempts = [
        {
            "comparison": {"acceptance": {"passed": True}},
            "variants": {
                "raw": summary(1_000, 0.10, 10.0),
                "current": summary(600, 0.06, 8.0),
            },
        },
        {
            "comparison": {"acceptance": {"passed": True}},
            "variants": {
                "raw": summary(1_100, 0.11, 12.0),
                "current": summary(650, 0.065, 9.0),
            },
        },
        {
            "comparison": {"acceptance": {"passed": True}},
            "variants": {
                "raw": summary(900, 0.09, 11.0),
                "current": summary(550, 0.055, 7.0),
            },
        },
    ]

    aggregate = run_context_efficiency_benchmark._aggregate(
        "live",
        ("raw", "current"),
        attempts,
        total_cost=0.48,
        expected_attempts=3,
    )

    assert aggregate["acceptance"]["passed"] is True
    assert aggregate["median_cost_usd"] == {"raw": 0.10, "current": 0.06}
    assert aggregate["median_model_latency_seconds"] == {"raw": 11.0, "current": 8.0}
    assert aggregate["observed_effects"]["current_input_tokens_saved_vs_raw"] == 400
    assert aggregate["observed_effects"]["current_cost_usd_saved_vs_raw"] == pytest.approx(0.04)
    assert (
        aggregate["observed_effects"]["current_model_latency_delta_vs_raw_seconds"]
        == -3.0
    )
