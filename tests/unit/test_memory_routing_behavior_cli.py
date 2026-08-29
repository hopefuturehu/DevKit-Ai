from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[2] / "scripts" / "run_memory_routing_behavior.py"
_SPEC = importlib.util.spec_from_file_location("run_memory_routing_behavior", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
run_memory_routing_behavior = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(run_memory_routing_behavior)


def test_live_memory_routing_requires_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUN_MEMORY_ROUTING_LIVE", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_memory_routing_behavior.py", "--provider", "live", "--repeat", "3"],
    )

    with pytest.raises(SystemExit, match="RUN_MEMORY_ROUTING_LIVE=1"):
        run_memory_routing_behavior.main()


@pytest.mark.parametrize(
    ("extra_args", "expected"),
    [
        ([], "至少需要 --repeat 3"),
        (["--repeat", "3", "--variants", "on_demand"], "必须同时包含"),
        (
            ["--repeat", "3", "--scenarios", "irrelevant", "explicit_history"],
            "必须包含四个",
        ),
    ],
)
def test_live_memory_routing_validates_comparison_shape_before_network(
    monkeypatch: pytest.MonkeyPatch,
    extra_args: list[str],
    expected: str,
) -> None:
    monkeypatch.setenv("RUN_MEMORY_ROUTING_LIVE", "1")
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_memory_routing_behavior.py", "--provider", "live", *extra_args],
    )

    with pytest.raises(SystemExit, match=expected):
        run_memory_routing_behavior.main()


def test_live_aggregate_uses_paired_medians_and_requires_router_quality() -> None:
    def case(passed: bool, tokens: int, requests: int = 1) -> dict:
        return {
            "quality": {
                "passed": passed,
                "gates": {"final_answer_correct": passed},
                "diagnostics": {
                    "exact_output_format": False,
                    "minimal_memory_tool_path": True,
                },
            },
            "metrics": {
                "input_tokens": tokens,
                "model_requests": requests,
                "cost_usd": tokens / 1_000_000,
                "model_latency_seconds": 1.0,
                "end_to_end_seconds": 1.1,
            },
        }

    attempts = []
    for attempt in range(3):
        cases = {}
        for scenario in run_memory_routing_behavior.MEMORY_ROUTING_SCENARIOS:
            cases[f"eager/{scenario.name}"] = case(
                scenario.name != "attribution_conflict",
                1_000 + attempt,
            )
            cases[f"on_demand/{scenario.name}"] = case(True, 700 + attempt, 2)
        attempts.append(
            {
                "cases": cases,
                "summary": {
                    "hypotheses": {
                        "irrelevant_avoids_memory_replay": True,
                        "implicit_relevant_recalled": True,
                        "explicit_history_forces_search": True,
                        "attribution_uses_original_evidence": True,
                    },
                    "paired_deltas_on_demand_minus_eager": {
                        scenario.name: {
                            "input_tokens": -300,
                            "model_requests": 1,
                            "cost_usd": -0.0003,
                            "model_latency_seconds": 1,
                            "end_to_end_seconds": 1,
                        }
                        for scenario in run_memory_routing_behavior.MEMORY_ROUTING_SCENARIOS
                    },
                },
            }
        )

    aggregate = run_memory_routing_behavior._aggregate(
        provider_kind="live",
        variants=run_memory_routing_behavior.MEMORY_ROUTING_VARIANTS,
        scenarios=run_memory_routing_behavior.MEMORY_ROUTING_SCENARIOS,
        attempts=attempts,
        expected_attempts=3,
        total_cost=0.01,
    )

    assert aggregate["acceptance"]["passed"] is True
    assert aggregate["case_pass_rates"]["eager/attribution_conflict"] == 0
    assert aggregate["case_pass_rates"]["on_demand/attribution_conflict"] == 1
    assert aggregate["median_case_metrics"]["on_demand/irrelevant"]["input_tokens"] == 701
    assert (
        aggregate["median_paired_deltas_on_demand_minus_eager"]["irrelevant"]["input_tokens"]
        == -300
    )
    assert aggregate["case_gate_rates"]["on_demand/irrelevant"]["final_answer_correct"] == 1
    assert aggregate["case_diagnostic_rates"]["on_demand/irrelevant"]["exact_output_format"] == 0
