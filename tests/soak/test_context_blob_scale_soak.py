from __future__ import annotations

import os
from pathlib import Path

import pytest

from bot.evals.context_blob_scale import (
    SOAK_CONTEXT_BLOB_SCALE_PROFILE,
    run_context_blob_scale_benchmark,
)


@pytest.mark.skipif(
    os.getenv("RUN_CONTEXT_BLOB_SCALE_SOAK") != "1",
    reason="设置 RUN_CONTEXT_BLOB_SCALE_SOAK=1 才运行 blob 规模评测",
)
def test_context_blob_store_at_soak_scale(tmp_path: Path) -> None:
    result = run_context_blob_scale_benchmark(
        profile=SOAK_CONTEXT_BLOB_SCALE_PROFILE,
        workspace=tmp_path / "blob-scale-soak",
    )

    assert result.summary["quality"]["passed"] is True
    assert result.summary["storage"]["unique_blobs"] == 120
    assert result.summary["storage"]["logical_blob_bytes"] > 100 * 1024 * 1024
    assert result.summary["latency"]["query"]["p95_seconds"] > 0
    assert result.summary["resources"]["peak_python_bytes_during_largest_query"] > 0
