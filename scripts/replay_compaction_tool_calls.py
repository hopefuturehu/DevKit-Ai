"""Reconstruct the first A32 summary input and observe it without executing tools."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from bot.compaction.handoff import SUMMARY_INSTRUCTION, protocol_closed
from bot.config import load_config, resolve_model_api_key
from bot.config.models import AppConfig
from bot.core.agent import AgentRunner
from bot.core.context import ContextAssembler, ContextPlanner, PositionedMessage, TokenEstimator
from bot.core.models import ChatMessage, ModelEventKind, ModelRequest, Role
from bot.core.termination import ProgressController
from bot.execution import EnvironmentCapabilities
from bot.providers import OpenAICompatibleProvider
from bot.skills import SkillCatalog
from bot.tools import ToolRegistry, register_builtin_tools
from bot.tools.kunpeng import register_kunpeng_tools

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "artifacts/context-strategy-path-20260910/a-output-32k"


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reconstruct():
    agent = next((SOURCE / "jobs/a").glob("*/agent"))
    db_path = agent / "trace/state.db"
    before_sha = sha(db_path)
    events = [json.loads(line) for line in (agent / "trace/events.jsonl").read_text().splitlines()]
    cutoff = next(e for e in events if e["type"] == "context.compaction.started")
    prior = [e for e in events if e["sequence"] < cutoff["sequence"]]
    session, run_id = cutoff["session_id"], cutoff["run_id"]
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute(
            "SELECT position, message_json FROM messages WHERE position <= 70 ORDER BY position"
        ).fetchall()
    assert [p for p, _ in rows] == list(range(1, 71))
    assert sha(db_path) == before_sha
    config = AppConfig.model_validate(tomllib.loads((SOURCE / "a.toml").read_text()))
    # Use pure AgentRunner render helpers only: no runtime, execution target, or
    # writable session store is started. Historical paths remain data strings.
    runner = object.__new__(AgentRunner)
    runner.config = config
    runner._token_estimator = TokenEstimator()
    runner.context_compactor = object()
    runner.memory_store = runner.subagent_controller = None
    runner._activated_tools = {}
    plans = [e["payload"] for e in prior if e["type"] == "plan.updated"]
    runner.store = SimpleNamespace(
        load_plan=lambda _: plans[-1] if plans else None,
        put_context_blob=lambda **kw: "blob:" + hashlib.sha256(kw["content"].encode()).hexdigest(),
    )
    registry = ToolRegistry()
    register_builtin_tools(registry)
    register_kunpeng_tools(registry)
    tools, tool_note = runner._select_tool_definitions(session, registry.definitions())
    assert tool_note is None
    catalog = SkillCatalog(Path("/installed-agent/no-skills"))
    context = ContextAssembler(workspace=Path("/app"), skill_catalog=catalog)
    assert not context.project_instruction_files()
    environment = EnvironmentCapabilities(
        operating_system="linux", architecture="x86_64", executables={"ksys": None, "devkit": None}
    )
    base = context.ledger_items(environment)
    history = []
    for position, raw in rows:
        message = ChatMessage.model_validate_json(raw)
        if message.role != Role.TOOL:
            message = runner._externalize_message(message, session_id=session, run_id=run_id)
        history.append(PositionedMessage(position, message, run_id=run_id))
    assert protocol_closed([e.message for e in history])
    assert all(not e.message.reasoning_content for e in history)
    notes = []
    controller = ProgressController(config.agent.progress)
    for event in prior:
        payload = event["payload"]
        if event["type"] == "run.progress" and payload["reason_code"] == "strong_progress":
            notes = [
                n for n in notes if n.id not in {"progress-stall-warning", "progress-recovery"}
            ]
        elif event["type"] == "run.stall_warning":
            runner._replace_runtime_note(
                notes,
                note_id="progress-stall-warning",
                priority=875,
                content=controller.warning_guidance(SimpleNamespace(**payload)),
            )
        elif event["type"] == "run.recovery_started":
            notes = [n for n in notes if n.id != "progress-stall-warning"]
            runner._replace_runtime_note(
                notes,
                note_id="progress-recovery",
                priority=900,
                content=controller.recovery_guidance(SimpleNamespace(**payload)),
            )
    runner._refresh_plan_note(notes, session)

    def assemble(conversation, runtime_notes):
        items = runner._build_context_items(
            base_items=base,
            memory_items=[],
            compaction_items=[],
            conversation=conversation,
            runtime_notes=runtime_notes,
        )
        return ModelRequest(
            model=config.model.name,
            messages=[i.message for i in sorted(items, key=ContextPlanner._render_order)],
            tools=tools,
            temperature=config.model.temperature,
            thinking=config.model.thinking,
            max_output_tokens=config.context.compaction_max_output_tokens,
        )

    first = assemble(history[:1], [])
    request = assemble(history, notes)
    request.messages.append(
        ChatMessage(
            role=Role.USER,
            content=SUMMARY_INSTRUCTION
            + "\n只输出交接摘要，不调用工具。Skill 正文由运行器恢复，不复制整份手册。",
        )
    )
    return (
        first,
        request,
        {
            "source_db": str(db_path.relative_to(ROOT)),
            "source_db_sha256": before_sha,
            "source_events_sha256": sha(agent / "trace/events.jsonl"),
            "config_sha256": sha(SOURCE / "a.toml"),
            "cutoff_sequence": cutoff["sequence"],
            "history_positions": [1, 70],
            "summary_covered_range": [1, 40],
            "runtime_note_ids": [n.id for n in notes],
            "tool_names": [t.name for t in tools],
            "environment": environment.model_dump(),
            "limitations": [
                "Original HTTP payload was not persisted; this is a reconstructed replay.",
                "Environment: Linux x86_64, ksys/devkit absent, no AGENTS.md, empty Skill catalog.",
                "No published compaction or live managed process at this boundary.",
                "Original two tool names/arguments are lost; these responses are new observations.",
            ],
        },
    )


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if (args.output / "manifest.json").exists():
        parser.error("Use a new output directory")
    local = load_config(ROOT)
    provider = OpenAICompatibleProvider(
        base_url=local.model.base_url,
        api_key=resolve_model_api_key(local.model, workspace=ROOT)
        if args.live
        else "offline-unused",
        timeout_seconds=90,
    )
    first, request, manifest = reconstruct()
    first_count = provider.estimate_input_tokens(first)
    count = provider.estimate_input_tokens(request)
    assert first_count is not None and count is not None
    manifest.update(
        created_at=datetime.now(UTC).isoformat(),
        live=args.live,
        original_first_input=3914,
        reconstructed_first_input=first_count.tokens,
        original_summary_input=73262,
        reconstructed_summary_input=count.tokens,
        estimate=count.model_dump(),
        model=request.model,
        thinking=request.thinking,
        max_output_tokens=request.max_output_tokens,
        repeats=3,
        max_cost_reserved_usd=0.5,
        script_sha256=sha(Path(__file__)),
    )
    save(args.output / "manifest.json", manifest)
    save(args.output / "request.json", provider._payload(request))
    print(json.dumps(manifest, ensure_ascii=False), flush=True)
    if not args.live:
        return
    # Require both observed token-count checkpoints before paying for a replay.
    assert first_count.tokens == 3914 and count.tokens == 73262
    reserve = 3 * (count.budget_tokens * 1.32 + request.max_output_tokens * 3.96) / 1_000_000
    assert reserve <= 0.5
    records = []
    for attempt in range(1, 4):
        record = {
            "attempt": attempt,
            "started_at": datetime.now(UTC).isoformat(),
            "text": "",
            "reasoning": "",
            "calls": {},
            "usage": None,
            "status": "started",
        }
        records.append(record)
        save(args.output / "results.json", records)
        try:
            async with asyncio.timeout(100):
                async for event in provider.stream(request):
                    with (args.output / f"events-{attempt}.jsonl").open("a") as stream:
                        stream.write(json.dumps(event.model_dump(), ensure_ascii=False) + "\n")
                    if event.kind == ModelEventKind.TEXT_DELTA:
                        record["text"] += event.text or ""
                    elif event.kind == ModelEventKind.REASONING_DELTA:
                        record["reasoning"] += event.text or ""
                    elif event.kind == ModelEventKind.TOOL_CALL_DELTA:
                        call = record["calls"].setdefault(
                            event.tool_index or 0, {"id": "", "name": "", "arguments": ""}
                        )
                        call["id"] = event.tool_call_id or call["id"]
                        call["name"] += event.tool_name or ""
                        call["arguments"] += event.arguments_delta or ""
                    elif event.kind == ModelEventKind.USAGE:
                        record["usage"] = event.provider_metadata
                    elif event.kind == ModelEventKind.FINISH:
                        record["finish_reason"] = event.finish_reason
                        record["finish_metadata"] = event.provider_metadata
            record["status"] = "completed"
        except Exception as exc:
            record["status"] = "error"
            record["error"] = str(exc).replace(provider.api_key, "<redacted>")
        finally:
            record["ended_at"] = datetime.now(UTC).isoformat()
            save(args.output / "results.json", records)
            print(json.dumps(record, ensure_ascii=False), flush=True)
        # Independent repeats of the same frozen input: do not append results,
        # tool errors, or corrective instructions between attempts.
        if attempt < 3:
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
