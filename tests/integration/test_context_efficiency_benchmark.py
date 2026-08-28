from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot.evals.context_efficiency import (
    CONTEXT_EFFICIENCY_VARIANTS,
    FAST_CONTEXT_EFFICIENCY_PROFILE,
    run_context_efficiency_benchmark,
    run_context_efficiency_suite,
)


@pytest.mark.asyncio
async def test_composite_case_measures_context_savings_and_call_tradeoffs(
    tmp_path: Path,
) -> None:
    results, comparison = await run_context_efficiency_suite(
        profile=FAST_CONTEXT_EFFICIENCY_PROFILE,
        workspace=tmp_path,
    )
    by_variant = {result.variant: result for result in results}

    assert tuple(by_variant) == CONTEXT_EFFICIENCY_VARIANTS
    assert comparison["acceptance"]["passed"] is True
    assert comparison["break_even_turn"] <= 4
    assert len({result.summary["workload"]["sha256"] for result in results}) == 1
    assert all(result.summary["quality"]["passed"] for result in results)

    raw = by_variant["raw"].summary["metrics"]
    compact = by_variant["compact-inline"].summary["metrics"]
    persistent = by_variant["query-persistent"].summary["metrics"]
    ranged = by_variant["range-one-shot"].summary["metrics"]
    current = by_variant["current"].summary["metrics"]
    assert compact["input_tokens"] <= raw["input_tokens"] * 0.75
    assert current["input_tokens"] <= raw["input_tokens"] * 0.50
    assert current["input_tokens"] < compact["input_tokens"]
    assert current["reference_tool_calls"] == len(FAST_CONTEXT_EFFICIENCY_PROFILE.lookup_turns)
    assert ranged["reference_tool_calls"] >= current["reference_tool_calls"] * 3
    assert ranged["agent_requests"] > current["agent_requests"]
    assert current["replayed_reference_tokens"] == 0
    assert persistent["replayed_reference_tokens"] > 0
    assert current["input_tokens"] < persistent["input_tokens"]
    # Externalization saves tokens but does add lookup calls relative to raw.
    assert raw["reference_tool_calls"] == 0
    assert current["reference_tool_calls"] > raw["reference_tool_calls"]

    for variant in CONTEXT_EFFICIENCY_VARIANTS:
        artifacts = tmp_path / variant / "artifacts"
        summary = json.loads((artifacts / "summary.json").read_text(encoding="utf-8"))
        requests = [
            json.loads(line)
            for line in (artifacts / "requests.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert summary["variant"] == variant
        assert requests
        assert all("model_latency_seconds" in request for request in requests)
        assert (artifacts / "turns.csv").is_file()
    assert json.loads((tmp_path / "comparison.json").read_text(encoding="utf-8")) == comparison


@pytest.mark.asyncio
async def test_context_efficiency_case_is_repeatable_for_the_same_seed(
    tmp_path: Path,
) -> None:
    first = await run_context_efficiency_benchmark(
        profile=FAST_CONTEXT_EFFICIENCY_PROFILE,
        workspace=tmp_path / "first",
        variant="current",
    )
    second = await run_context_efficiency_benchmark(
        profile=FAST_CONTEXT_EFFICIENCY_PROFILE,
        workspace=tmp_path / "second",
        variant="current",
    )

    assert first.summary["workload"] == second.summary["workload"]
    assert first.summary["metrics"] == second.summary["metrics"]
    assert first.turns == second.turns
