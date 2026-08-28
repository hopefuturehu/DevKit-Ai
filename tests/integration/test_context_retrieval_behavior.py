from __future__ import annotations

import pytest

from bot.evals.context_retrieval_behavior import run_context_retrieval_behavior_suite


@pytest.mark.asyncio
async def test_scripted_retrieval_behavior_suite_covers_decision_matrix(tmp_path) -> None:
    results, summary = await run_context_retrieval_behavior_suite(workspace=tmp_path / "suite")

    assert summary["quality"]["passed"] is True
    by_name = {result.scenario.name: result.summary for result in results}
    assert by_name["irrelevant"]["metrics"]["reference_tool_calls"] == 0
    assert by_name["preview"]["metrics"]["reference_tool_calls"] == 0
    assert by_name["middle"]["metrics"]["query_calls"] == 1
    assert by_name["no_match_retry"]["metrics"]["reference_tool_calls"] == 2
    assert by_name["no_match_retry"]["metrics"]["empty_query_results"] >= 1
    assert by_name["multi_match"]["metrics"]["query_calls"] == 1
    assert by_name["multi_blob"]["metrics"]["source_tool_calls"] == 2
    assert by_name["multi_blob"]["quality"]["gates"]["selects_correct_blob"] is True
    assert (tmp_path / "suite" / "cases.csv").is_file()
