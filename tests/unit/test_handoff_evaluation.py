from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bot.core.models import (
    ChatMessage,
    ModelCapabilities,
    ModelEvent,
    ModelEventKind,
    ModelRequest,
    Role,
)
from bot.evals.handoff_fixtures import score_answer, synthetic_checkpoint
from bot.evals.handoff_recording import (
    ExperimentLedger,
    RecordingHandoffProvider,
    flash_prices,
    usage_cost,
)
from bot.providers import ModelProvider, ProviderError


def test_score_requires_values_not_only_markers_or_claimed_success():
    expected = {"revision": 2, "verified": False}
    assert score_answer('{"revision":2,"verified":false}', expected)["passed"]
    assert not score_answer('{"revision":1,"verified":false}', expected)["passed"]
    assert not score_answer('{"revision":2,"verified":true}', expected)["passed"]
    assert not score_answer('{"revision":2,"verified":0}', expected)["passed"]
    assert not score_answer("all facts preserved", expected)["passed"]


def test_fixture_latest_correction_and_oracle_are_separate():
    checkpoint = synthetic_checkpoint("correction", 5, 3, correction=True)
    assert checkpoint.probes[1]["expected"] == {"retry_limit": 11}
    assert checkpoint.probes[7]["expected"] == {"original_retry_limit": 7}
    assert "11" not in checkpoint.probes[1]["prompt"]
    assert "ledger-west-042" not in checkpoint.probes[6]["prompt"]


def test_price_accounts_cache_and_does_not_double_count_reasoning():
    usage = {
        "prompt_tokens": 1000,
        "prompt_cache_hit_tokens": 800,
        "prompt_cache_miss_tokens": 200,
        "completion_tokens": 100,
        "completion_tokens_details": {"reasoning_tokens": 50},
    }
    prices = {"hit": 1, "miss": 10, "output": 20}
    assert usage_cost(usage, prices) == pytest.approx(0.0048)
    assert usage_cost({}, prices) is None
    assert usage_cost({**usage, "prompt_tokens": 999}, prices) is None


def test_weekday_peak_boundaries_and_weekend():
    assert flash_prices(datetime(2026, 9, 9, 1, tzinfo=UTC))["miss"] == 0.44
    assert flash_prices(datetime(2026, 9, 9, 4, tzinfo=UTC))["miss"] == 0.22
    assert flash_prices(datetime(2026, 9, 12, 1, tzinfo=UTC))["miss"] == 0.22


class UsageProvider(ModelProvider):
    def __init__(self):
        self.calls = 0

    def capabilities(self, model):
        return ModelCapabilities()

    async def stream(self, request):
        self.calls += 1
        for tokens in (50, 100):
            yield ModelEvent(
                kind=ModelEventKind.USAGE,
                input_tokens=1000,
                output_tokens=tokens,
                provider_metadata={
                    "raw_usage": {
                        "prompt_tokens": 1000,
                        "prompt_cache_hit_tokens": 800,
                        "prompt_cache_miss_tokens": 200,
                        "completion_tokens": tokens,
                    }
                },
            )
        yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")


@pytest.mark.asyncio
async def test_usage_updates_and_phase_switch_do_not_reset_or_double_bill(tmp_path):
    underlying = UsageProvider()
    ledger = ExperimentLedger(tmp_path / "ledger.jsonl", max_cost_usd=1)
    provider = RecordingHandoffProvider(underlying, ledger, tmp_path / "requests", identity={})
    request = ModelRequest(
        model="deepseek-v4-flash", messages=[ChatMessage(role=Role.USER, content="check")]
    )
    for phase in ("handoff_D", "continuation"):
        provider.phase = phase
        async for _ in provider.stream(request):
            pass
    assert len(ledger.rows) == 2 and underlying.calls == 2
    assert all(row["raw_usage"]["completion_tokens"] == 100 for row in ledger.rows)
    assert ledger.reserved_cost == pytest.approx(2 * (800 * 0.014 + 200 * 0.44 + 100 * 1.32) / 1e6)
    restored = ExperimentLedger(tmp_path / "ledger.jsonl", max_cost_usd=1)
    assert restored.reserved_cost == ledger.reserved_cost
    provider.max_requests = 2
    with pytest.raises(ProviderError, match="request budget"):
        async for _ in provider.stream(request):
            pass
    assert underlying.calls == 2


@pytest.mark.asyncio
async def test_budget_denies_request_before_network(tmp_path):
    underlying = UsageProvider()
    ledger = ExperimentLedger(tmp_path / "ledger.jsonl", max_cost_usd=0.001)
    provider = RecordingHandoffProvider(underlying, ledger, tmp_path / "requests", identity={})
    request = ModelRequest(
        model="deepseek-v4-flash", messages=[ChatMessage(role=Role.USER, content="check")]
    )
    with pytest.raises(ProviderError, match="total cost budget"):
        async for _ in provider.stream(request):
            pass
    assert underlying.calls == 0 and ledger.rows == []
