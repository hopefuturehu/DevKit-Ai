"""Request-level usage ledger, including failed calls and cache-aware prices."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

from bot.core.context import TokenEstimator
from bot.core.models import ModelEventKind
from bot.providers import ModelProvider, ProviderError, ProviderErrorKind

PRICE_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"
PRICE_CHECKED = "2026-09-09"


def flash_prices(at: datetime) -> dict[str, float]:
    at = at.astimezone(UTC)
    peak = at.weekday() < 5 and (1 <= at.hour < 4 or 6 <= at.hour < 10)
    scale = 2 if peak else 1
    return {"hit": 0.007 * scale, "miss": 0.22 * scale, "output": 0.66 * scale}


def usage_cost(usage: dict, prices: dict) -> float | None:
    fields = (
        "prompt_tokens",
        "completion_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
    )
    if not all(isinstance(usage.get(key), int) and usage[key] >= 0 for key in fields):
        return None
    if (
        usage["prompt_cache_hit_tokens"] + usage["prompt_cache_miss_tokens"]
        != usage["prompt_tokens"]
    ):
        return None
    return (
        usage["prompt_cache_hit_tokens"] * prices["hit"]
        + usage["prompt_cache_miss_tokens"] * prices["miss"]
        + usage["completion_tokens"] * prices["output"]
    ) / 1_000_000


class ExperimentLedger:
    def __init__(self, path: Path, *, max_cost_usd: float):
        self.path = path
        self.max_cost_usd = max_cost_usd
        self.rows = (
            [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        )

    @property
    def reserved_cost(self):
        return sum(row.get("budget_charge_usd", 0) for row in self.rows)

    def append(self, row):
        self.rows.append(row)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()


class RecordingHandoffProvider(ModelProvider):
    def __init__(self, underlying, ledger: ExperimentLedger, output: Path, *, identity: dict):
        self.underlying = underlying
        self.ledger = ledger
        self.output = output
        self.identity = identity
        self.phase = "setup"
        self.logical_turn = 0
        self.rows: list[dict] = []
        self.estimator = TokenEstimator()
        self.started_clock = monotonic()
        self.max_requests = 64
        self.max_wall_seconds = 1800

    def capabilities(self, model):
        return self.underlying.capabilities(model)

    async def stream(self, original):
        if len(self.rows) >= self.max_requests:
            raise ProviderError("segment request budget", kind=ProviderErrorKind.PAYMENT)
        if monotonic() - self.started_clock >= self.max_wall_seconds:
            raise ProviderError("segment wall-time budget", kind=ProviderErrorKind.PAYMENT)
        # All L1 branches use the same explicit mode. Avoid inventing reasoning
        # for synthetic checkpoints; this does not fix the default production mode.
        request = original.model_copy(deep=True, update={"thinking": "disabled"})
        if request.model != "deepseek-v4-flash":
            raise ValueError("The frozen price adapter applies only to deepseek-v4-flash")
        estimate = self.estimator.request(request.messages, request.tools)
        if estimate > 120_000:
            raise ProviderError("experiment input budget", kind=ProviderErrorKind.CONTEXT_LENGTH)
        # Conservative peak-price reserve with headroom for estimator error.
        reserve = (
            max(estimate * 2, 131072) * 0.44 + (request.max_output_tokens or 8192) * 1.32
        ) / 1_000_000
        if self.ledger.reserved_cost + reserve > self.ledger.max_cost_usd:
            raise ProviderError("experiment total cost budget", kind=ProviderErrorKind.PAYMENT)
        request_id = uuid4().hex
        started = datetime.now(UTC)
        start_clock = monotonic()
        last_usage = {}
        metadata = {}
        finish = None
        error = None
        texts = []
        reasoning_chars = 0
        tool_names = set()
        payload = request.model_dump(mode="json")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.output.mkdir(parents=True, exist_ok=True)
        # Local experimental artifacts only: never commit raw historical prompts.
        (self.output / f"{request_id}.request.json").write_text(encoded)
        try:
            async for event in self.underlying.stream(request):
                if event.kind == ModelEventKind.USAGE:
                    last_usage = event.provider_metadata.get("raw_usage", {})
                    metadata.update(event.provider_metadata)
                elif event.kind == ModelEventKind.FINISH:
                    finish = event.finish_reason
                    metadata.update(event.provider_metadata)
                elif event.kind == ModelEventKind.TEXT_DELTA:
                    texts.append(event.text or "")
                elif event.kind == ModelEventKind.REASONING_DELTA:
                    reasoning_chars += len(event.text or "")
                elif event.kind == ModelEventKind.TOOL_CALL_DELTA and event.tool_name:
                    tool_names.add(event.tool_name)
                yield event
        except BaseException as exc:
            error = {
                "type": type(exc).__name__,
                "kind": str(getattr(exc, "kind", "")),
                "status_code": getattr(exc, "status_code", None),
                "message": str(exc)[:500],
            }
            raise
        finally:
            ended = datetime.now(UTC)
            cost_start = usage_cost(last_usage, flash_prices(started))
            cost_end = usage_cost(last_usage, flash_prices(ended))
            normalized = usage_cost(last_usage, {"hit": 0.014, "miss": 0.44, "output": 1.32})
            row = {
                **self.identity,
                "request_id": request_id,
                "phase": self.phase,
                "logical_turn": self.logical_turn,
                "started_at": started.isoformat(),
                "ended_at": ended.isoformat(),
                "latency_seconds": monotonic() - start_clock,
                "model": request.model,
                "response_model": metadata.get("model"),
                "provider_request_id": metadata.get("request_id"),
                "provider_response_id": metadata.get("response_id"),
                "thinking": "disabled",
                "estimated_input_tokens": estimate,
                "raw_usage": last_usage,
                "usage_complete": normalized is not None,
                "cost_usd_at_start_rate": cost_start,
                "cost_usd_at_end_rate": cost_end,
                "normalized_peak_cost_usd": normalized,
                "price_boundary_crossed": flash_prices(started) != flash_prices(ended),
                "budget_charge_usd": normalized if normalized is not None else reserve,
                "finish_reason": finish,
                "error": error,
                "output_text": "".join(texts),
                "reasoning_chars": reasoning_chars,
                "requested_tools": sorted(tool_names),
                "request_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
                "system_sha256": hashlib.sha256(
                    json.dumps(
                        [m.model_dump(mode="json") for m in request.messages if m.role == "system"],
                        sort_keys=True,
                    ).encode()
                ).hexdigest(),
                "tools_sha256": hashlib.sha256(
                    json.dumps(
                        [t.model_dump(mode="json") for t in request.tools], sort_keys=True
                    ).encode()
                ).hexdigest(),
            }
            self.rows.append(row)
            self.ledger.append(row)


def aggregate_requests(rows: list[dict]) -> dict:
    measured = [row for row in rows if row["usage_complete"]]
    input_tokens = sum(row["raw_usage"]["prompt_tokens"] for row in measured)
    hits = sum(row["raw_usage"]["prompt_cache_hit_tokens"] for row in measured)
    return {
        "requests": len(rows),
        "usage_complete_requests": len(measured),
        "input_tokens": input_tokens,
        "cache_hit_tokens": hits,
        "cache_hit_ratio": hits / input_tokens if input_tokens else None,
        "output_tokens": sum(row["raw_usage"]["completion_tokens"] for row in measured),
        "normalized_peak_cost_usd": sum(row["normalized_peak_cost_usd"] for row in measured),
        "cost_usd_at_start_rate": sum(row["cost_usd_at_start_rate"] for row in measured),
        "latency_seconds": sum(row["latency_seconds"] for row in rows),
        "errors": sum(row["error"] is not None for row in rows),
        "unknown_cost_requests": len(rows) - len(measured),
    }
