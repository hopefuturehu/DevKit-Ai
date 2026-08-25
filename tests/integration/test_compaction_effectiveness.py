from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot.config.models import AppConfig
from bot.evals.compaction_effectiveness import (
    ScriptedCompactionProvider,
    run_compaction_effectiveness,
    synthetic_replay_entries,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expect_success", "expected_requests"),
    [
        ("success", True, 1),
        ("length", True, 2),
        ("format", True, 2),
        ("rate-limit", True, 2),
        ("context-overflow", True, 2),
        ("authentication", False, 1),
    ],
)
async def test_compaction_effectiveness_replays_failure_matrix(
    tmp_path: Path,
    scenario: str,
    expect_success: bool,
    expected_requests: int,
) -> None:
    workspace = tmp_path / scenario
    config = AppConfig.model_validate(
        {
            "model": {
                "base_url": "https://unused",
                "name": "agent-model",
                "context_window_tokens": 32_000,
            },
            "context": {
                "compaction_model": "summary-model",
                "compaction_summary_target_tokens": 2_000,
                "compaction_summary_tokens": 3_000,
                "compaction_max_output_tokens": 4_000,
                "compaction_max_input_tokens": 20_000,
                "compaction_source_refs": "range",
                "compaction_transport_retry_backoff_seconds": 0,
            },
        }
    )
    provider = ScriptedCompactionProvider(scenario)  # type: ignore[arg-type]

    result = await run_compaction_effectiveness(
        config=config,
        provider=provider,
        entries=synthetic_replay_entries(turns=6, tool_output_chars=1_000),
        workspace=workspace,
        scenario=scenario,
        expect_success=expect_success,
    )

    assert result.summary["quality"]["passed"] is True
    assert result.summary["requests"]["total"] == expected_requests
    assert result.summary["quality"]["fact_recall"] == 1
    assert (result.artifacts / "summary.json").is_file()
    requests = [
        json.loads(line)
        for line in (result.artifacts / "requests.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert requests
    assert all("raw_messages" not in json.dumps(row) for row in requests)
