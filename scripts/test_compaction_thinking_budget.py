"""Replay the four distinct 2026-09-16 isolated fallbacks under three output policies.

Only --live sends requests. Production state is read-only; private responses and
disposable telemetry databases stay in a new, ignored artifacts directory.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import sqlite3
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

from bot.compaction.service import ContextCompactor
from bot.compaction.strategies import StrategyCompactor
from bot.config import load_config, resolve_model_api_key
from bot.core.events import EventBus, MemoryEventSink
from bot.core.models import ModelEventKind, ModelRequest
from bot.providers import OpenAICompatibleProvider
from bot.sessions import SQLiteSessionStore

ROOT = Path(__file__).resolve().parents[1]
INCIDENT = ROOT / "docs/data/compaction-incident-20260916.json"
BODY_BUDGET = 8192
MODES = ("current", "thinking_off", "thinking_reserve_8k")
PRICING = {
    "source": "https://api-docs.deepseek.com/quick_start/pricing/",
    "checked_at": "2026-09-17",
    "usd_per_million_peak": {"hit": 0.006, "miss": 0.3, "output": 1.2},
    "off_peak_multiplier": 0.5,
    "note": "Flash legacy alias now routes to V4.1 Flash; estimates, not an invoice.",
}


def save(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def variant(request: ModelRequest, mode: str) -> ModelRequest:
    result = request.model_copy(deep=True)
    if mode == "thinking_off":
        result.thinking = "disabled"
    elif mode == "thinking_reserve_8k":
        result.thinking = "enabled"
        # DeepSeek has ONE shared cap, not separate hard reasoning/text caps.
        result.max_output_tokens = BODY_BUDGET + 8192
    elif mode != "current":
        raise ValueError(mode)
    return result


def checkpoints(db_path: Path) -> list[dict]:
    incident = json.loads(INCIDENT.read_text())
    unique = {r["request_blob_ref"]: r for r in incident["requests"] if r["phase"] == "a_isolated"}
    result = []
    with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        for ref, historical in unique.items():
            row = db.execute("SELECT content FROM context_blobs WHERE id = ?", (ref,)).fetchone()
            if row is None:
                raise ValueError(f"Missing frozen request: {ref}")
            raw = row[0].encode() if isinstance(row[0], str) else row[0]
            if "blob:" + digest(raw) != ref:
                raise ValueError(f"Corrupt frozen request: {ref}")
            request = ModelRequest.model_validate_json(raw)
            payload = json.loads(request.messages[1].content)
            if (
                len(request.messages) != 2
                or request.tools
                or request.thinking is not None
                or request.max_output_tokens != BODY_BUDGET
                or request.model != "deepseek-v4-flash"
                or payload["kind"] != "isolated_handoff"
            ):
                raise ValueError("Frozen incident assumptions changed")
            result.append(
                {
                    "request": request,
                    "request_ref": ref,
                    "covered_end": payload["covered_range"][1],
                    "historical_input_tokens": historical["input_tokens"],
                }
            )
    if len(result) != 4:
        raise ValueError("Expected all four distinct isolated requests")
    return result


def peak_multiplier(started_at: str) -> float:
    when = datetime.fromisoformat(started_at).astimezone(UTC)
    return 1.0 if when.weekday() < 5 and (1 <= when.hour < 4 or 6 <= when.hour < 10) else 0.5


def token_split(usage: dict, response: dict, thinking: str | None) -> dict:
    completion = usage.get("completion_tokens")
    reported = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    reasoning = reported
    source = "api_usage" if reported is not None else "unknown"
    if (
        reported is None
        and thinking == "disabled"
        and completion is not None
        and not response["reasoning"]
        and not response["tool_calls"]
    ):
        # DeepSeek omits reasoning_tokens in non-thinking responses. Require both
        # the explicit disabled request and the captured absence of reasoning.
        reasoning, source = 0, "disabled_and_no_reasoning_delta"
    return {
        "reported_reasoning_tokens": reported,
        "reasoning_tokens": reasoning,
        "body_tokens": completion - reasoning
        if completion is not None and reasoning is not None
        else None,
        "token_split_source": source,
    }


class RecordedProvider(OpenAICompatibleProvider):
    response_metadata: dict

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.response_metadata = {}

    async def stream(self, request):
        async for event in super().stream(request):
            if event.kind == ModelEventKind.USAGE:
                self.response_metadata = event.provider_metadata
            yield event


async def trial(output, checkpoint, repeat, mode, config, api_key) -> dict:
    output.mkdir()
    request = variant(checkpoint["request"], mode)
    provider = RecordedProvider(base_url=config.model.base_url, api_key=api_key, timeout_seconds=90)
    save(output / "request.json", provider._payload(request))
    store = SQLiteSessionStore(output / "state.db")
    session = store.create_session(output)
    store.start_run(session, "replay")
    sink = MemoryEventSink()
    compactor = ContextCompactor(
        config=config, provider=provider, store=store, event_bus=EventBus([store, sink])
    )
    engine = StrategyCompactor(compactor, provider, session, "replay")
    record = {
        "id": output.name,
        "checkpoint": checkpoint["covered_end"],
        "request_ref": checkpoint["request_ref"],
        "repeat": repeat,
        "mode": mode,
        "started_at": datetime.now(UTC).isoformat(),
        "max_tokens": request.max_output_tokens,
        "thinking": request.thinking,
        "candidate_pass": False,
    }
    try:
        try:
            await engine.summarize(
                request,
                phase="a_isolated",
                start=1,
                end=checkpoint["covered_end"],
                limit=engine.request_input_limit(request),
                attempts=1,
                operation_id=output.name,
            )
            record["candidate_pass"] = True
        except Exception as exc:
            record["error_class"] = compactor._error_class(exc).value
            record["error"] = str(exc) or type(exc).__name__
        events = [event.model_dump(mode="json") for event in sink.events]
        save(output / "events.json", events)
        terminal = [
            e["payload"]
            for e in events
            if e["type"]
            in {"context.compaction.request.failed", "context.compaction.request.completed"}
        ]
        if len(terminal) != 1 or engine.requests != 1:
            raise RuntimeError("Replay did not make exactly one attempt")
        telemetry = terminal[0]
        usage = telemetry["raw_usage"]
        with sqlite3.connect(output / "state.db") as db:
            raw = db.execute(
                "SELECT content FROM context_blobs WHERE id = ?", (telemetry["response_ref"],)
            ).fetchone()[0]
        response = json.loads(raw)
        (output / "summary.md").write_text(response["text"])
        completion = usage.get("completion_tokens")
        split = token_split(usage, response, request.thinking)
        body_tokens = split["body_tokens"]
        record.update(
            ended_at=datetime.now(UTC).isoformat(),
            finish_reason=telemetry["finish_reason"],
            duration_seconds=telemetry["duration_ms"] / 1000,
            input_tokens=usage.get("prompt_tokens"),
            cached_input_tokens=usage.get("prompt_cache_hit_tokens"),
            output_tokens=completion,
            **split,
            body_chars=len(response["text"]),
            summary_sha256=digest(response["text"].encode()),
            body_budget_pass=record["candidate_pass"]
            and body_tokens is not None
            and body_tokens <= BODY_BUDGET,
            provider_model=provider.response_metadata.get("model"),
            response_id=provider.response_metadata.get("response_id"),
            planned_input_tokens=telemetry["planned_input_tokens"],
            input_limit=telemetry["input_limit"],
        )
        if usage:
            rates = PRICING["usd_per_million_peak"]
            record["estimated_cost_usd"] = (
                peak_multiplier(record["started_at"])
                * (
                    usage["prompt_cache_hit_tokens"] * rates["hit"]
                    + usage["prompt_cache_miss_tokens"] * rates["miss"]
                    + completion * rates["output"]
                )
                / 1_000_000
            )
        else:
            record["estimated_cost_usd"] = None
        save(output / "result.json", record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        return record
    finally:
        await engine.close()
        store.close()


def aggregate(records: list[dict]) -> dict:
    result = {}
    for mode in MODES:
        rows = [r for r in records if r["mode"] == mode]
        result[mode] = {
            "attempts": len(rows),
            "candidate_pass": sum(r["candidate_pass"] for r in rows),
            "body_budget_pass": sum(r["body_budget_pass"] for r in rows),
            "finish_reasons": dict(Counter(r["finish_reason"] for r in rows)),
            "missing_usage": sum(r["output_tokens"] is None for r in rows),
            "estimated_cost_usd": sum(r["estimated_cost_usd"] or 0 for r in rows),
        }
        for field in (
            "duration_seconds",
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "body_tokens",
        ):
            present = [r[field] for r in rows if r[field] is not None]
            result[mode]["mean_" + field] = mean(present) if present else None
    return result


def analyze_saved(output: Path) -> dict:
    """Recompute metrics offline, preserving the original live results and script."""
    result = json.loads((output / "results.json").read_text())
    for record in result["trials"]:
        directory = output / record["id"]
        events = json.loads((directory / "events.json").read_text())
        terminal = [
            e["payload"]
            for e in events
            if e["type"]
            in {"context.compaction.request.failed", "context.compaction.request.completed"}
        ][-1]
        with sqlite3.connect((directory / "state.db").as_uri() + "?mode=ro", uri=True) as db:
            raw = db.execute(
                "SELECT content FROM context_blobs WHERE id = ?", (terminal["response_ref"],)
            ).fetchone()[0]
        response = json.loads(raw)
        record.update(token_split(terminal["raw_usage"], response, record["thinking"]))
        record["body_budget_pass"] = (
            record["candidate_pass"]
            and record["body_tokens"] is not None
            and record["body_tokens"] <= BODY_BUDGET
        )
        record["reasoning_chars"] = len(response["reasoning"])
    result["aggregate"] = aggregate(result["trials"])
    result["analysis"] = {
        "source_results_sha256": digest((output / "results.json").read_bytes()),
        "script_sha256": digest(Path(__file__).read_bytes()),
        "note": "Offline accounting: disabled responses omit reasoning_tokens; no new requests.",
    }
    save(output / "analyzed-results.json", result)
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-db", type=Path, default=ROOT / ".bot/state.db")
    parser.add_argument("--repeats", type=int, choices=range(1, 4), default=2)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--analyze-only", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / "artifacts"):
        parser.error("Output must be under this repository's ignored artifacts/")
    if args.analyze_only:
        print(json.dumps(analyze_saved(output)["aggregate"], ensure_ascii=False, indent=2))
        return
    if output.exists():
        parser.error("Use a new directory under this repository's ignored artifacts/")
    config = load_config(ROOT)
    frozen = checkpoints(args.source_db)
    provider = OpenAICompatibleProvider(base_url=config.model.base_url, api_key="offline-unused")
    if not provider._is_official_deepseek_endpoint():
        parser.error("Replay is restricted to the original official DeepSeek provider")
    plans = []
    for repeat in range(1, args.repeats + 1):
        block = [(c, mode) for c in frozen for mode in MODES]
        random.Random(20260917 + repeat).shuffle(block)
        for c, mode in block:
            request = variant(c["request"], mode)
            original, modified = provider._payload(c["request"]), provider._payload(request)
            assert {k: v for k, v in original.items() if k not in {"thinking", "max_tokens"}} == {
                k: v for k, v in modified.items() if k not in {"thinking", "max_tokens"}
            }
            estimate = provider.estimate_input_tokens(request)
            if estimate is None:
                raise ValueError("An exact input estimator is required for preflight")
            cfg = config.context
            limit = min(
                cfg.max_input_tokens,
                config.model.context_window_tokens
                - request.max_output_tokens
                - cfg.protocol_reserve_tokens
                - cfg.safety_margin_tokens,
            )
            if estimate.budget_tokens > limit:
                raise ValueError(f"Request exceeds window: {estimate.budget_tokens} > {limit}")
            plans.append(
                {
                    "checkpoint": c["covered_end"],
                    "mode": mode,
                    "repeat": repeat,
                    "planned_input_tokens": estimate.budget_tokens,
                    "input_limit": limit,
                    "max_tokens": request.max_output_tokens,
                    "request_ref": c["request_ref"],
                }
            )
    worst = sum(p["planned_input_tokens"] * 0.3 + p["max_tokens"] * 1.2 for p in plans) / 1e6
    if worst > 2:
        raise ValueError(f"Worst-case planned peak spend exceeds $2: {worst}")
    output.mkdir(parents=True)
    manifest = {
        "live": args.live,
        "created_at": datetime.now(UTC).isoformat(),
        "pricing": PRICING,
        "git_revision": (
            await asyncio.to_thread(
                subprocess.check_output, ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            )
        ).strip(),
        "script_sha256": digest(Path(__file__).read_bytes()),
        "source_db": str(args.source_db),
        "source_access": "SQLite mode=ro",
        "repeats": args.repeats,
        "concurrency": 3,
        "body_budget_tokens": BODY_BUDGET,
        "timeout_seconds": config.context.compaction_request_timeout_seconds,
        "worst_case_planned_usd_peak": worst,
        "plans": plans,
        "scope": "Actual StrategyCompactor.summarize validation; no publication/continuation test",
        "limitations": [
            "Four related checkpoints in one failure-selected session",
            "Shared provider cap cannot guarantee independent text/reasoning quotas",
            "Body <=8192 is an additional metric, not a current runtime hard cap",
            "Cross-trial cache warming affects timing and cost",
            "Missing usage means actual charge is unknown, not zero",
        ],
    }
    save(output / "manifest.json", manifest)
    (output / "replay-script.py").write_bytes(Path(__file__).read_bytes())
    print(
        json.dumps(
            {
                "planned_trials": len(plans),
                "worst_case_usd": worst,
                "live": args.live,
                "output": str(output),
            }
        ),
        flush=True,
    )
    if not args.live:
        return
    key = resolve_model_api_key(config.model, workspace=ROOT)
    semaphore = asyncio.Semaphore(3)

    async def run(plan):
        async with semaphore:
            checkpoint = next(c for c in frozen if c["covered_end"] == plan["checkpoint"])
            name = f"p{plan['checkpoint']}-r{plan['repeat']}-{plan['mode']}"
            return await trial(output / name, checkpoint, plan["repeat"], plan["mode"], config, key)

    records = await asyncio.gather(*(run(p) for p in plans))
    result = {"manifest": manifest, "aggregate": aggregate(records), "trials": records}
    save(output / "results.json", result)
    print(json.dumps(result["aggregate"], ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
