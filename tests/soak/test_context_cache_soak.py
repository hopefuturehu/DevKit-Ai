from __future__ import annotations

import os
from pathlib import Path

import pytest

from bot.evals.context_cache import (
    SOAK_CONTEXT_CACHE_PROFILE,
    run_context_cache_benchmark,
)


@pytest.mark.skipif(
    os.getenv("RUN_CONTEXT_CACHE_SOAK") != "1",
    reason="设置 RUN_CONTEXT_CACHE_SOAK=1 才运行生产规模上下文缓存评测",
)
@pytest.mark.asyncio
async def test_production_window_repeatedly_grows_and_compacts(tmp_path: Path) -> None:
    result = await run_context_cache_benchmark(
        profile=SOAK_CONTEXT_CACHE_PROFILE,
        workspace=tmp_path / "soak",
    )

    assert result.summary["quality"]["passed"] is True
    assert result.summary["compaction"]["completed"] >= 6
    assert result.summary["compaction"]["rebuild_from_raw"] >= 1
    assert result.summary["compaction"]["incremental_update"] >= 5
    assert len(result.epochs) >= 7
    assert result.summary["metrics"]["cache_read_tokens"] > 0
    assert (
        result.summary["metrics"]["cache_adjusted_cost_units"]
        < result.summary["metrics"]["no_cache_cost_units"]
    )
