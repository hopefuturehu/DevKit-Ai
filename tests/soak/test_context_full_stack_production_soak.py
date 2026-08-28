from __future__ import annotations

import os
from pathlib import Path

import pytest

from bot.evals.context_full_stack_soak import (
    SOAK_CONTEXT_FULL_STACK_PROFILE,
    run_context_full_stack_soak,
)


@pytest.mark.skipif(
    os.getenv("RUN_CONTEXT_FULL_STACK_SOAK") != "1",
    reason="设置 RUN_CONTEXT_FULL_STACK_SOAK=1 才运行全栈长任务评测",
)
@pytest.mark.asyncio
async def test_context_full_stack_at_soak_scale(tmp_path: Path) -> None:
    result = await run_context_full_stack_soak(
        profile=SOAK_CONTEXT_FULL_STACK_PROFILE,
        workspace=tmp_path / "full-stack-soak",
    )

    assert result.summary["quality"]["passed"] is True
    assert result.summary["metrics"]["logical_turns"] == 120
    assert result.summary["metrics"]["compactions"] >= 6
    assert result.summary["metrics"]["runtime_rebuilds"] == 1
    assert result.summary["metrics"]["session_forks"] == 1
