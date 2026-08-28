from __future__ import annotations

import json

from bot.evals.context_blob_scale import (
    FAST_CONTEXT_BLOB_SCALE_PROFILE,
    run_context_blob_scale_benchmark,
)


def test_context_blob_scale_fast_profile_covers_storage_query_and_access(tmp_path) -> None:
    profile = FAST_CONTEXT_BLOB_SCALE_PROFILE.with_overrides(
        blob_count=6,
        blob_sizes=(1024, 16 * 1024),
        read_repeats=1,
        query_repeats=1,
        concurrency=2,
    )

    result = run_context_blob_scale_benchmark(profile=profile, workspace=tmp_path / "blob-scale")

    assert result.summary["quality"]["passed"] is True
    assert result.summary["storage"]["unique_blobs"] == 6
    assert result.summary["storage"]["attempted_puts"] == 7
    assert result.summary["storage"]["duplicate_bytes_avoided"] == 1024
    assert result.summary["quality"]["gates"]["unauthorized_session_is_blocked"] is True
    assert result.summary["quality"]["gates"]["fork_inherits_referenced_blob"] is True
    assert result.summary["latency"]["concurrent_query"]["count"] == 6
    assert result.summary["resources"]["peak_python_bytes_during_largest_query"] > 0

    artifacts = result.workspace / "artifacts"
    assert (artifacts / "operations.csv").is_file()
    persisted = json.loads((artifacts / "summary.json").read_text(encoding="utf-8"))
    assert persisted["workload_sha256"] == result.summary["workload_sha256"]
