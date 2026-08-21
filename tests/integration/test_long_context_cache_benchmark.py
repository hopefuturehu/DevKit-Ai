from __future__ import annotations

from pathlib import Path

import pytest

from bot.evals.context_cache import (
    FAST_CONTEXT_CACHE_PROFILE,
    run_context_cache_benchmark,
)


@pytest.mark.asyncio
async def test_fast_suite_measures_growth_compaction_and_counterfactual(
    tmp_path: Path,
) -> None:
    current = await run_context_cache_benchmark(
        profile=FAST_CONTEXT_CACHE_PROFILE,
        workspace=tmp_path / "current",
    )
    no_compaction = await run_context_cache_benchmark(
        profile=FAST_CONTEXT_CACHE_PROFILE,
        workspace=tmp_path / "no-compaction",
        variant="no-compaction",
    )

    assert current.summary["quality"]["passed"] is True
    assert no_compaction.summary["quality"]["passed"] is True
    assert current.summary["workload"]["sha256"] == no_compaction.summary["workload"]["sha256"]
    assert current.summary["compaction"]["completed"] >= 3
    assert current.summary["compaction"]["rebuild_from_raw"] >= 1
    assert current.summary["compaction"]["incremental_update"] >= 1
    assert no_compaction.summary["compaction"]["completed"] == 0

    current_metrics = current.summary["metrics"]
    baseline_metrics = no_compaction.summary["metrics"]
    assert 0 < current_metrics["weighted_cache_hit_ratio"] < 1
    assert current_metrics["cache_read_tokens"] <= current_metrics["ideal_reusable_tokens"]
    # The append-only baseline gets a prettier raw hit rate but sends a much
    # larger prompt. This is why the suite's primary score is cost per turn.
    assert (
        baseline_metrics["weighted_cache_hit_ratio"] > current_metrics["weighted_cache_hit_ratio"]
    )
    assert current_metrics["cost_per_logical_turn"] < baseline_metrics["cost_per_logical_turn"]
    assert len(current.epochs) == current.summary["compaction"]["completed"] + 1

    for workspace in (tmp_path / "current", tmp_path / "no-compaction"):
        artifacts = workspace / "artifacts"
        assert (artifacts / "summary.json").is_file()
        assert (artifacts / "requests.jsonl").is_file()
        assert (artifacts / "epochs.csv").is_file()
