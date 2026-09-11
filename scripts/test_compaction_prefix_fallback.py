"""Compare three compaction paths at the reconstructed A32 failure checkpoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from replay_compaction_tool_calls import ROOT, reconstruct, save, sha

from bot.compaction.service import ContextCompactor
from bot.compaction.strategies import StrategyCompactor, StrategyFrame
from bot.config import load_config, resolve_model_api_key
from bot.core.context import ContextPlanner
from bot.core.events import EventBus, MemoryEventSink
from bot.core.models import ChatMessage, ModelRequest
from bot.evals.context_strategy_audit import aggregate, request_rows
from bot.providers import OpenAICompatibleProvider
from bot.sessions import SQLiteSessionStore

PRICING = {
    "usd_per_million_peak": {"hit": 0.044, "miss": 1.32, "output": 3.96},
    "off_peak_multiplier": 0.5,
    "note": "Frozen 2026-09-10 comparison rates; estimates, not an invoice.",
}


class RecordedProvider(OpenAICompatibleProvider):
    def __init__(self, output: Path, **kwargs):
        super().__init__(**kwargs)
        self.output, self.requests = output, 0

    async def stream(self, request):
        self.requests += 1
        number = self.requests
        save(self.output / f"request-{number}.json", self._payload(request))
        async for event in super().stream(request):
            with (self.output / f"response-{number}.jsonl").open("a") as stream:
                stream.write(event.model_dump_json() + "\n")
            yield event


async def trial(output: Path, mode: str, *, api_key: str | None) -> dict:
    first, historical, provenance, context = reconstruct(include_context=True)
    config = context["config"].model_copy(deep=True)
    config.context.compaction_strategy = "a" if mode == "old_a" else "a_fallback"
    provider = RecordedProvider(
        output,
        base_url=config.model.base_url,
        api_key=api_key or "offline-unused",
        timeout_seconds=90,
    )
    assert provider.estimate_input_tokens(first).tokens == 3914
    assert provider.estimate_input_tokens(historical).tokens == 73262
    request = historical.model_copy(deep=True)
    request.messages.pop()  # Remove the old A suffix; each strategy constructs its own.
    request.max_output_tokens = config.model.max_output_tokens
    assert request.max_output_tokens == 32768
    history = context["history"]
    positions = {id(e.message): e.position for e in history}
    mapping = tuple(positions.get(id(m)) for m in historical.messages[:-1])
    assert sorted(p for p in mapping if p is not None) == list(range(1, 71))
    output.mkdir(parents=True)
    store = SQLiteSessionStore(output / "state.db")
    session = store.create_session(Path("/app"))
    store.start_run(session, "replay")
    with sqlite3.connect((ROOT / provenance["source_db"]).as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute(
            "SELECT message_json FROM messages WHERE position <= 70 ORDER BY position"
        ).fetchall()
    for (raw,) in rows:
        store.append_message(session, "replay", ChatMessage.model_validate_json(raw))
    sink = MemoryEventSink()
    compactor = ContextCompactor(
        config=config,
        provider=provider,
        store=store,
        event_bus=EventBus([store, sink]),
    )
    runner = context["runner"]
    runner.config, runner.store, runner.context_compactor = config, store, compactor

    def project(record):
        items = runner._build_context_items(
            base_items=context["base"],
            memory_items=[],
            compaction_items=runner._compaction_context_items(
                {"compaction": record}, run_id="replay"
            ),
            conversation=[e for e in history if e.position > record["covered_end_position"]],
            runtime_notes=context["notes"],
        )
        return ModelRequest(
            model=request.model,
            messages=[i.message for i in sorted(items, key=ContextPlanner._render_order)],
            tools=request.tools,
            max_output_tokens=32768,
            temperature=request.temperature,
            thinking=request.thinking,
        )

    engine = StrategyCompactor(compactor, provider, session, "replay")
    engine.frame = StrategyFrame(
        request,
        project,
        evidence=history,
        prefix_request=request if mode != "isolated" else None,
        prefix_positions=mapping,
        prefix_skip_reason="evaluation_direct_isolated",
        remaining_cost_usd=0.5,
    )
    entries = store.load_positioned_messages(session)
    boundary, reserve = engine.select_boundary(engine.frame, entries, 40, [1])
    assert boundary == 40, (boundary, reserve)
    record = {
        "mode": mode,
        "started_at": datetime.now(UTC).isoformat(),
        "source": provenance,
        "covered_range": [1, boundary],
        "main_max_output_tokens": 32768,
        "isolated_max_output_tokens": 8192,
        "live": api_key is not None,
    }
    save(output / "input.json", request.model_dump(mode="json"))
    save(output / "provenance.json", record)
    try:
        if api_key is not None:
            before = compactor._digest(entries)
            began = monotonic()
            result = await engine.compact(40, [1])
            record.update(
                result=result.model_dump(mode="json"),
                metrics=engine.last_metrics,
                duration_seconds=monotonic() - began,
            )
            assert compactor._digest(store.load_positioned_messages(session)) == before
            events = [e.model_dump(mode="json") for e in sink.events]
            (output / "events.jsonl").write_text(
                "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
            )
            rows = request_rows(events)
            record["requests"] = rows
            record["usage"] = aggregate(rows, PRICING)
            active = compactor.projection(session)["compaction"]
            if active:
                (output / "summary.md").write_text(active["summary_text"] + "\n")
                record["summary_sha256"] = sha(output / "summary.md")
            assert result.request_count <= (1 if mode == "isolated" else 2)
            assert not any(e["type"] == "tool.requested" for e in events)
        record["ended_at"] = datetime.now(UTC).isoformat()
        save(output / "result.json", record)
        return record
    finally:
        await engine.close()
        store.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, choices=range(1, 4), default=3)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output directory")
    args.output.mkdir(parents=True)
    local = load_config(ROOT)
    api_key = resolve_model_api_key(local.model, workspace=ROOT) if args.live else None
    order = []
    for repeat in range(1, args.repeats + 1):
        modes = ["old_a", "prefix_fallback", "isolated"]
        random.Random(20260911 + repeat).shuffle(modes)
        order.extend((repeat, mode) for mode in modes)
    manifest = {
        "revision": (
            await asyncio.to_thread(
                subprocess.check_output, ["git", "rev-parse", "HEAD"], text=True
            )
        ).strip(),
        "script_sha256": sha(Path(__file__)),
        "order": order,
        "live": args.live,
        "pricing": PRICING,
        "max_reserved_usd_per_trial": 0.5,
        "limitations": [
            "Reconstructed checkpoint, not the original HTTP request bytes.",
            "No task tools execute here; continuation is tested in a separate Harbor run.",
            "No artificial prewarming. Repeats may benefit from cross-trial provider cache.",
            "Main limit is 32K. Old A and isolated summary use 8K; prefix inherits main 32K.",
            "One checkpoint and three repeats do not establish a general success rate.",
        ],
    }
    save(args.output / "manifest.json", manifest)
    results = []
    for repeat, mode in order:
        print(f"starting repeat={repeat} mode={mode}", flush=True)
        record = await trial(args.output / f"{repeat}-{mode}", mode, api_key=api_key)
        record["repeat"] = repeat
        results.append(record)
        save(args.output / "results.json", results)
        print(
            json.dumps(
                {k: record.get(k) for k in ("repeat", "mode", "result", "usage")},
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
