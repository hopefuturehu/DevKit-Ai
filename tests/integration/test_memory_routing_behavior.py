from __future__ import annotations

import json

import pytest

from bot.evals.memory_routing_behavior import (
    _final_answer_correct,
    _final_answer_exact_format,
    memory_routing_scenario,
    run_memory_routing_behavior_suite,
)


def test_memory_routing_eval_separates_correctness_from_exact_format() -> None:
    scenario = memory_routing_scenario("irrelevant")
    response = "7 + 5 = 12\n\n[ROUTER_DONE=irrelevant:12]"

    assert _final_answer_correct(scenario, response) is True
    assert _final_answer_exact_format(scenario, response) is False


@pytest.mark.asyncio
async def test_scripted_memory_routing_suite_covers_paired_tradeoff_matrix(
    tmp_path,
) -> None:
    results, summary = await run_memory_routing_behavior_suite(
        workspace=tmp_path / "memory-routing-suite"
    )

    assert summary["acceptance"] == {
        "paired_matrix_complete": True,
        "router_quality_passed": True,
        "hypotheses_passed": True,
    }
    assert all(summary["hypotheses"].values())
    by_key = {(result.variant, result.scenario.name): result for result in results}
    assert len(by_key) == 8

    irrelevant_eager = by_key[("eager", "irrelevant")].summary
    irrelevant_routed = by_key[("on_demand", "irrelevant")].summary
    assert irrelevant_eager["metrics"]["automatic_memory_requests"] == 1
    assert irrelevant_routed["metrics"]["automatic_memory_requests"] == 0
    assert (
        irrelevant_routed["metrics"]["input_tokens"] < irrelevant_eager["metrics"]["input_tokens"]
    )

    implicit = by_key[("on_demand", "implicit_relevant")].summary
    assert implicit["observed"]["router_decisions"] == ["suggest_search"]
    assert implicit["observed"]["named_tool_choices"] == []
    assert implicit["observed"]["requested_tool_names"] == ["search_memory"]

    explicit = by_key[("on_demand", "explicit_history")].summary
    assert explicit["observed"]["router_decisions"] == ["require_search"]
    assert explicit["observed"]["named_tool_choices"] == ["search_memory"]

    attribution = by_key[("on_demand", "attribution_conflict")].summary
    assert attribution["observed"]["router_decisions"] == ["require_evidence"]
    assert attribution["observed"]["named_tool_choices"] == [
        "search_memory",
        "load_memory_evidence",
    ]
    assert attribution["observed"]["requested_tool_names"] == [
        "search_memory",
        "load_memory_evidence",
    ]
    assert attribution["observed"]["final_text"] == "[ROUTER_ATTRIBUTION=denied]"

    artifact = tmp_path / "memory-routing-suite" / "summary.json"
    assert json.loads(artifact.read_text(encoding="utf-8"))["quality"]["router_passed"] is True
