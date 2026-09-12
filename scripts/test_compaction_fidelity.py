"""Paired live replay of frozen pre/post fidelity implementations; no task tools execute."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import random
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from replay_compaction_tool_calls import ROOT, reconstruct, save, sha
from test_compaction_prefix_fallback import PRICING, RecordedProvider

from bot.compaction.handoff import protocol_closed
from bot.config import load_config, resolve_model_api_key
from bot.core.context import ContextPlanner, PositionedMessage
from bot.core.events import EventBus, EventType, MemoryEventSink
from bot.core.models import ChatMessage
from bot.evals.context_strategy_audit import aggregate, request_rows
from bot.sessions import SQLiteSessionStore

REVISIONS = {"baseline": "04d7630", "improved": "1bcda03"}
CASES = {
    "initial": {"visible": 70, "through": 40, "parent_end": 0},
    "parent_error": {"visible": 56, "through": 56, "parent_end": 40},
    "later_correction": {"visible": 72, "through": 72, "parent_end": 40},
}
PARENT = ROOT / "artifacts/compaction-prefix-fallback-20260911/replay/1-isolated/summary.md"


def load_version(output: Path, arm: str):
    classes = []
    for filename, classname in (
        ("service", "ContextCompactor"),
        ("strategies", "StrategyCompactor"),
    ):
        source = subprocess.check_output(
            ["git", "show", f"{REVISIONS[arm]}:src/bot/compaction/{filename}.py"], cwd=ROOT
        )
        path = output / "frozen" / f"{arm}_{filename}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            assert path.read_bytes() == source
        else:
            path.write_bytes(source)
        name = f"bot.compaction._fidelity_{arm}_{filename}"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        classes.append(getattr(module, classname))
        if filename == "strategies":
            classes.append(module.StrategyFrame)
    return classes


def historical_context():
    first, historical, provenance, context = reconstruct(include_context=True)
    source = (ROOT / provenance["source_db"]).resolve()
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as db:
        raw = [
            (pos, ChatMessage.model_validate_json(value))
            for pos, value in db.execute(
                "SELECT position,message_json FROM messages WHERE position<=72 ORDER BY position"
            )
        ]
    assert [p for p, _ in raw] == list(range(1, 73))
    existing = {e.position: e for e in context["history"]}
    history = [
        existing.get(pos) or PositionedMessage(pos, message, run_id="replay")
        for pos, message in raw
    ]
    return first, historical, provenance, context, raw, history


def prepare(output: Path, repeats: int) -> dict:
    if (output / "manifest.json").exists():
        manifest = json.loads((output / "manifest.json").read_text())
        assert manifest["repeats"] == repeats
        assert manifest["script_sha256"] == sha(Path(__file__)), "frozen script changed"
        return manifest
    output.mkdir(parents=True, exist_ok=True)
    _, _, provenance, _, raw, _ = historical_context()
    old_compactor, _, _ = load_version(output, "baseline")
    load_version(output, "improved")
    seeds = {}
    for case, settings in CASES.items():
        folder = output / "seeds" / case
        folder.mkdir(parents=True)
        store = SQLiteSessionStore(folder / "state.db")
        try:
            session = store.create_session(Path("/app"))
            store.start_run(session, "seed")
            for _pos, message in raw[: settings["visible"]]:
                store.append_message(session, "seed", message)
            store.finish_run("seed", "completed")
            entries = store.load_positioned_messages(session)
            assert protocol_closed([e.message for e in entries])
            if settings["parent_end"]:
                covered = entries[: settings["parent_end"]]
                identifier = store.start_context_compaction(
                    session_id=session,
                    parent_id=None,
                    trigger="strategy:a_fallback",
                    model="deepseek-v4-pro",
                    covered_start_position=1,
                    covered_end_position=settings["parent_end"],
                    delta_start_position=1,
                    source_sha256=old_compactor._digest(covered),
                    anchor_positions=[1],
                    source_chars=sum(len(e.message.model_dump_json()) for e in covered),
                )
                store.complete_context_compaction(
                    identifier,
                    summary_text=PARENT.read_text().strip(),
                    summary_token_estimate=3000,
                    source_refs=[],
                    input_tokens=0,
                    output_tokens=0,
                    duration_ms=0,
                )
            seeds[case] = {"session_id": session, "path": str(folder / "state.db"), **settings}
        finally:
            store.close()
        seeds[case]["sha256"] = sha(folder / "state.db")
    order = []
    for case in CASES:
        for repeat in range(1, repeats + 1):
            arms = list(REVISIONS)
            random.Random(f"20260912:{case}:{repeat}").shuffle(arms)
            order.extend({"case": case, "repeat": repeat, "arm": arm} for arm in arms)
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "script_sha256": sha(Path(__file__)),
        "revisions": REVISIONS,
        "frozen_sources": {p.name: sha(p) for p in (output / "frozen").glob("*.py")},
        "source": provenance,
        "parent_summary": {"path": str(PARENT), "sha256": sha(PARENT)},
        "seeds": seeds,
        "order": order,
        "repeats": repeats,
        "main_output_tokens": 32768,
        "isolated_output_tokens": 8192,
        "summary_hard_tokens": 4000,
        "max_reserved_usd_per_trial": 0.5,
        "pricing": PRICING,
        "continuation_selection": (
            "Earliest repeat with both arms published in initial case, without inspecting quality."
        ),
        "limitations": [
            "Initial checkpoint is reconstructed, not original HTTP bytes or a process snapshot.",
            "Cross-round cases combine a real erroneous summary with real later messages; "
            "constructed replays, not original compactions.",
            "Cross-round requests omit old runtime notes to avoid future-state leakage.",
            "Parent snapshot IDs are fixed via seed database copies, not regenerated per arm.",
            "No task tools execute during summary replay; "
            "five repeats do not establish population accuracy.",
            "No artificial prewarming; interleaving cannot eliminate provider cache/order effects.",
            "Usage missing on failed requests remains unknown; prices are frozen comparison rates.",
        ],
    }
    save(output / "manifest.json", manifest)
    return manifest


async def trial(output: Path, manifest: dict, item: dict, api_key: str | None) -> dict:
    case, arm, repeat = item["case"], item["arm"], item["repeat"]
    folder = output / "replays" / f"{case}-{repeat}-{arm}"
    if (folder / "result.json").exists():
        result = json.loads((folder / "result.json").read_text())
        assert result["live"] == (api_key is not None)
        return result
    folder.mkdir(parents=True, exist_ok=False)
    seed = manifest["seeds"][case]
    assert sha(Path(seed["path"])) == seed["sha256"]
    assert sha(PARENT) == manifest["parent_summary"]["sha256"]
    with sqlite3.connect(Path(seed["path"]).resolve().as_uri() + "?mode=ro", uri=True) as src:
        with sqlite3.connect(folder / "state.db") as dest:
            src.backup(dest)
    first, historical, _, context, _, history = historical_context()
    history = history[: seed["visible"]]
    cfg = context["config"].model_copy(deep=True)
    cfg.context.compaction_strategy = "a_fallback"
    compactor_class, strategy_class, frame_class = load_version(output, arm)
    provider = RecordedProvider(
        folder, base_url=cfg.model.base_url, api_key=api_key or "offline-unused", timeout_seconds=90
    )
    assert provider.estimate_input_tokens(first).tokens == 3914
    assert provider.estimate_input_tokens(historical).tokens == 73262
    store = SQLiteSessionStore(folder / "state.db")
    store.start_run(seed["session_id"], "replay")
    sink = MemoryEventSink()
    compactor = compactor_class(
        config=cfg, provider=provider, store=store, event_bus=EventBus([store, sink])
    )
    runner = context["runner"]
    runner.config, runner.store, runner.context_compactor = cfg, store, compactor
    request = historical.model_copy(deep=True)
    request.messages.pop()
    request.max_output_tokens = 32768

    def project(record):
        items = runner._build_context_items(
            base_items=context["base"],
            memory_items=[],
            compaction_items=runner._compaction_context_items(
                {"compaction": record}, run_id="replay"
            ),
            conversation=[e for e in history if e.position > record["covered_end_position"]],
            runtime_notes=context["notes"] if case == "initial" else [],
        )
        return request.model_copy(
            update={
                "messages": [i.message for i in sorted(items, key=ContextPlanner._render_order)]
            }
        )

    if seed["parent_end"]:
        request = project(compactor.projection(seed["session_id"])["compaction"])
    by_message = {e.message.model_dump_json(): e.position for e in history}
    mapping = tuple(by_message.get(m.model_dump_json()) for m in request.messages)
    engine = strategy_class(compactor, provider, seed["session_id"], "replay")
    engine.frame = frame_class(
        request,
        project,
        evidence=history,
        prefix_request=request,
        prefix_positions=mapping,
        remaining_cost_usd=0.5,
    )
    entries = store.load_positioned_messages(seed["session_id"])
    boundary, reserve = engine.select_boundary(engine.frame, entries, seed["through"], [1])
    assert boundary == seed["through"], (case, boundary, reserve)
    result = {
        **item,
        "live": api_key is not None,
        "started_at": datetime.now(UTC).isoformat(),
        "covered_range": [1, boundary],
        "reserved_after_tokens": reserve,
    }
    save(folder / "input.json", request.model_dump(mode="json"))
    try:
        if api_key is not None:
            digest = compactor._digest(entries)
            began = monotonic()
            outcome = await engine.compact(boundary, [1])
            result.update(
                result=outcome.model_dump(mode="json"),
                metrics=engine.last_metrics,
                duration_seconds=monotonic() - began,
            )
            assert compactor._digest(store.load_positioned_messages(seed["session_id"])) == digest
            events = [e.model_dump(mode="json") for e in sink.events]
            (folder / "events.jsonl").write_text(
                "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
            )
            rows = request_rows(events)
            result["requests"], result["usage"] = rows, aggregate(rows, PRICING)
            if outcome.compacted:
                active = compactor.projection(seed["session_id"])["compaction"]
                (folder / "summary.md").write_text(active["summary_text"] + "\n")
                result["summary_sha256"] = sha(folder / "summary.md")
                save(folder / "continued-request.json", project(active).model_dump(mode="json"))
            assert outcome.request_count <= 2
            assert not any(e["type"] == EventType.TOOL_REQUESTED.value for e in events)
        result["ended_at"] = datetime.now(UTC).isoformat()
        save(folder / "result.json", result)
        return result
    finally:
        await engine.close()
        store.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=5)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    manifest = prepare(output, args.repeats)
    local = load_config(ROOT)
    api_key = resolve_model_api_key(local.model, workspace=ROOT) if args.live else None
    if args.live and not api_key:
        raise ValueError("model credentials unavailable")
    results = []
    for item in manifest["order"]:
        if (output / "STOP").exists():
            break
        print(json.dumps({"starting": item}, ensure_ascii=False), flush=True)
        result = await trial(output, manifest, item, api_key)
        results.append(result)
        save(output / "results.json", results)
        print(
            json.dumps(
                {**item, "result": result.get("result"), "usage": result.get("usage")},
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
