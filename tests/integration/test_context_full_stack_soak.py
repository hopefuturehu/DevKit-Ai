from __future__ import annotations

import pytest

from bot.evals.context_full_stack_soak import (
    FAST_CONTEXT_FULL_STACK_PROFILE,
    run_context_full_stack_soak,
)


@pytest.mark.asyncio
async def test_context_full_stack_fast_profile_crosses_lifecycle_boundaries(tmp_path) -> None:
    result = await run_context_full_stack_soak(
        profile=FAST_CONTEXT_FULL_STACK_PROFILE,
        workspace=tmp_path / "full-stack",
    )

    assert result.summary["quality"]["passed"] is True
    assert result.summary["metrics"]["logical_turns"] == 24
    assert result.summary["metrics"]["runtime_rebuilds"] == 1
    assert result.summary["metrics"]["session_forks"] == 1
    assert result.summary["metrics"]["compactions"] >= 2
    assert result.summary["metrics"]["externalized_source_results"] == 24
    assert result.summary["quality"]["gates"]["tool_schema_shedding_observed"] is True
    assert result.summary["quality"]["gates"]["child_requires_explicit_blob_grant"] is True
    assert (result.workspace / "artifacts" / "turns.csv").is_file()
