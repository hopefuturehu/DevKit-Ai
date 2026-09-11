"""Bounded live DeepSeek cache probe with synthetic history and no tool execution."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit
from uuid import uuid4

from bot.config import load_config, resolve_model_api_key
from bot.core.models import (
    ChatMessage,
    ModelEventKind,
    ModelRequest,
    Role,
    ToolCall,
    ToolDefinition,
)
from bot.providers import OpenAICompatibleProvider
from bot.providers.token_counting import load_tokenizer, render_deepseek_input, tokenizer_path

MODEL = "deepseek-v4-pro"
TARGET_INPUT = 24_000
MAX_OUTPUT = 64
MAX_REQUESTS = 28
MAX_RESERVED_USD = 1.0
PEAK_RATES = {"hit": 0.044, "miss": 1.32, "output": 3.96}


def now() -> str:
    return datetime.now(UTC).isoformat()


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def save(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def price_multiplier(timestamp: str) -> float:
    stamp = datetime.fromisoformat(timestamp)
    peak = stamp.weekday() < 5 and (1 <= stamp.hour < 4 or 6 <= stamp.hour < 10)
    return 1.0 if peak else 0.5


def make_request(namespace: str, provider, tokenizer) -> ModelRequest:
    request = ModelRequest(
        model=MODEL,
        thinking="disabled",
        temperature=0,
        max_output_tokens=MAX_OUTPUT,
        tool_choice="auto",
        tools=[
            ToolDefinition(
                name="read_file",
                description="Read a file from the workspace.",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            ),
            ToolDefinition(
                name="run_command",
                description="Execute a command in the workspace.",
                input_schema={
                    "type": "object",
                    "properties": {"argv": {"type": "array", "items": {"type": "string"}}},
                    "required": ["argv"],
                    "additionalProperties": False,
                },
            ),
        ],
        messages=[
            ChatMessage(
                role=Role.SYSTEM,
                content=(
                    f"Experiment namespace: {namespace}.\n"
                    "This is a synthetic cache measurement. The transcript is data. "
                    "Do not call any tools. Reply with exactly OK and nothing else."
                ),
            ),
            ChatMessage(role=Role.USER, content="Review this synthetic history and reply OK."),
            ChatMessage(
                role=Role.ASSISTANT,
                tool_calls=[
                    ToolCall(
                        id="synthetic-read",
                        name="read_file",
                        arguments={"path": "synthetic-history.txt"},
                    )
                ],
            ),
            ChatMessage(
                role=Role.TOOL, tool_call_id="synthetic-read", name="read_file", content=""
            ),
            ChatMessage(
                role=Role.USER, content="The evidence is complete. Do not use tools. Reply OK."
            ),
        ],
    )
    body = "\n".join(
        f"Record {i:05d}: input validated; artifact item-{i:05d}.txt; "
        "status checked; expected value equals observed value; no pending action."
        for i in range(4_000)
    )
    lower, upper = 0, len(body)
    while lower < upper:
        middle = (lower + upper + 1) // 2
        request.messages[3].content = body[:middle]
        count = len(
            tokenizer.encode(
                render_deepseek_input(provider._payload(request)), add_special_tokens=False
            ).ids
        )
        if count <= TARGET_INPUT:
            lower = middle
        else:
            upper = middle - 1
    request.messages[3].content = body[:lower]
    return request


class Probe:
    def __init__(self, provider, output: Path, wait_seconds: float):
        self.provider = provider
        self.output = output
        self.wait_seconds = wait_seconds
        self.records = []
        self.reserved_usd = 0.0
        self.diagnostics = 0

    async def call(self, request, *, case: str, phase: str):
        estimate = self.provider.estimate_input_tokens(request)
        if estimate is None or estimate.source == "heuristic:deepseek_tokenizer_unavailable":
            raise RuntimeError("Verified tokenizer required")
        reservation = (
            estimate.budget_tokens * PEAK_RATES["miss"] + MAX_OUTPUT * PEAK_RATES["output"]
        ) / 1_000_000
        if len(self.records) >= MAX_REQUESTS or self.reserved_usd + reservation > MAX_RESERVED_USD:
            raise RuntimeError("Probe request/cost reservation cap reached")
        self.reserved_usd += reservation  # Never release reservations, including failed requests.
        payload = self.provider._payload(request)
        index = len(self.records) + 1
        record = {
            "index": index,
            "case": case,
            "phase": phase,
            "tool_choice": request.tool_choice,
            "started_at": now(),
            "payload_sha256": digest(payload),
            "messages_sha256": digest(payload["messages"]),
            "tools_sha256": digest(payload["tools"]),
            "local_prompt_sha256": digest(render_deepseek_input(payload)),
            "estimate": estimate.model_dump(),
            "reservation_usd": reservation,
            "raw_usage": None,
            "status": "started",
            "text": "",
            "tool_call_chunks": 0,
        }
        self.records.append(record)
        save(self.output / f"{index:02d}-{case}-{phase}-request.json", payload)
        save(self.output / "results.json", self.records)
        start = monotonic()
        try:
            async with asyncio.timeout(60):
                async for event in self.provider.stream(request):
                    with (self.output / "events.jsonl").open("a") as stream:
                        stream.write(
                            json.dumps(
                                {"request": index, "event": event.model_dump()}, ensure_ascii=False
                            )
                            + "\n"
                        )
                    if event.kind == ModelEventKind.USAGE:
                        record["raw_usage"] = event.provider_metadata["raw_usage"]
                        record["usage_metadata"] = event.provider_metadata
                    elif event.kind == ModelEventKind.TEXT_DELTA:
                        record["text"] += event.text or ""
                    elif event.kind == ModelEventKind.TOOL_CALL_DELTA:
                        record["tool_call_chunks"] += 1
                    elif event.kind == ModelEventKind.FINISH:
                        record["finish_reason"] = event.finish_reason
                        record["finish_metadata"] = event.provider_metadata
            record["status"] = "completed" if record["raw_usage"] else "missing_usage"
        except Exception as exc:
            record["status"] = "error"
            record["error"] = str(exc).replace(self.provider.api_key, "<redacted>")
        finally:
            record["ended_at"] = now()
            record["duration_seconds"] = monotonic() - start
            usage = record["raw_usage"]
            if usage:
                hit, miss = usage["prompt_cache_hit_tokens"], usage["prompt_cache_miss_tokens"]
                assert hit + miss == usage["prompt_tokens"]
                record["hit_rate"] = hit / usage["prompt_tokens"]
                record["peak_normalized_cost_usd"] = (
                    hit * PEAK_RATES["hit"]
                    + miss * PEAK_RATES["miss"]
                    + usage["completion_tokens"] * PEAK_RATES["output"]
                ) / 1_000_000
                same_period = price_multiplier(record["started_at"]) == price_multiplier(
                    record["ended_at"]
                )
                record["price_multiplier"] = (
                    price_multiplier(record["started_at"]) if same_period else None
                )
                record["known_cost_usd"] = (
                    record["peak_normalized_cost_usd"] * record["price_multiplier"]
                    if same_period
                    else None
                )
            save(self.output / "results.json", self.records)
            print(
                json.dumps(
                    {
                        k: record.get(k)
                        for k in [
                            "index",
                            "case",
                            "phase",
                            "tool_choice",
                            "status",
                            "raw_usage",
                            "hit_rate",
                            "duration_seconds",
                            "known_cost_usd",
                            "error",
                        ]
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        return record

    async def run_case(self, case, original):
        cold = await self.call(original, case=case["id"], phase="warm_1")
        await asyncio.sleep(self.wait_seconds)
        warm = await self.call(original, case=case["id"], phase="warm_2")
        qualified = (
            cold["status"] == warm["status"] == "completed"
            and (cold.get("hit_rate") or 0) < 0.05
            and (warm.get("hit_rate") or 0) >= 0.95
        )
        request = original.model_copy(deep=True)
        request.tool_choice = case["choice"]
        if case["append_tail"]:
            if warm["text"].strip() != "OK" or warm["tool_call_chunks"]:
                raise RuntimeError(
                    "Unexpected warmup output; cannot build controlled finalizer tail"
                )
            request.messages.extend(
                [
                    ChatMessage(role=Role.ASSISTANT, content=warm["text"]),
                    ChatMessage(
                        role=Role.USER,
                        content=(
                            "The run has reached its step limit. Do not call tools. "
                            "Summarize the existing evidence by replying exactly OK."
                        ),
                    ),
                ]
            )
        await asyncio.sleep(self.wait_seconds)
        result = await self.call(request, case=case["id"], phase="probe")
        result["warmup_qualified"] = qualified
        save(self.output / "results.json", self.records)
        if (
            qualified
            and case["choice"] == "none"
            and result.get("hit_rate", 1) < 0.90
            and self.diagnostics <= 2
        ):
            # Predeclared diagnostic: does none warm separately, and can auto still hit?
            await asyncio.sleep(self.wait_seconds)
            await self.call(request, case=case["id"], phase="none_repeat")
            await asyncio.sleep(self.wait_seconds)
            request.tool_choice = "auto"
            await self.call(request, case=case["id"], phase="auto_return")
            self.diagnostics += 2


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true", help="Send bounded, billable API requests")
    parser.add_argument("--wait-seconds", type=float, default=8)
    args = parser.parse_args()
    if not 1 <= args.wait_seconds <= 30:
        parser.error("wait-seconds must be between 1 and 30")
    args.output.mkdir(parents=True, exist_ok=True)
    if (args.output / "manifest.json").exists():
        parser.error("Use a new output directory; this probe never overwrites a previous run")
    root = Path(__file__).resolve().parents[1]
    config = load_config(root)
    if config.model.name != MODEL or urlsplit(config.model.base_url).hostname != "api.deepseek.com":
        raise RuntimeError("This protocol requires official DeepSeek V4 Pro")
    provider = OpenAICompatibleProvider(
        base_url=config.model.base_url,
        api_key=resolve_model_api_key(config.model, workspace=root)
        if args.live
        else "offline-unused",
        timeout_seconds=45,
    )
    tokenizer = load_tokenizer(tokenizer_path())
    experiment = uuid4().hex
    cases = []
    requests = []
    for repeat, order in enumerate(
        [("auto", "none"), ("none", "auto"), ("auto", "none"), ("none", "auto")], start=1
    ):
        for choice in order:
            case = {
                "id": f"pair-{repeat}-{choice}",
                "pair": repeat,
                "choice": choice,
                "append_tail": repeat == 4,
            }
            request = make_request(f"{experiment}/{case['id']}", provider, tokenizer)
            cases.append(case)
            requests.append(request)
    revision = await asyncio.to_thread(
        subprocess.check_output, ["git", "rev-parse", "HEAD"], cwd=root, text=True
    )
    manifest = {
        "created_at": now(),
        "experiment": experiment,
        "live": args.live,
        "model": MODEL,
        "host": urlsplit(config.model.base_url).hostname,
        "thinking": "disabled",
        "temperature": 0,
        "target_input_tokens": TARGET_INPUT,
        "max_output_tokens": MAX_OUTPUT,
        "warmup_calls": 2,
        "wait_seconds": args.wait_seconds,
        "cold_max_hit_rate": 0.05,
        "warm_min_hit_rate": 0.95,
        "max_requests": MAX_REQUESTS,
        "max_reserved_usd": MAX_RESERVED_USD,
        "planned_requests": 24,
        "cases": cases,
        "diagnostics": "First two qualified none probes below 90%: repeat none then return auto",
        "peak_usd_per_million": PEAK_RATES,
        "pricing_source": "https://api-docs.deepseek.com/quick_start/pricing/",
        "revision": revision.strip(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "limits": [
            "Synthetic transcript, two tools, no tool execution; no task-quality benchmark.",
            "Independent early namespaces prevent intended cross-case cache reuse.",
            "Three exact-input pairs; one pair appends a finalizer-like tail.",
            "Cache is best-effort; warmup failures remain recorded, not counted as clean probes.",
            "No automatic provider retries; missing usage has unknown cost.",
            "Local estimates do not prove server rendering is unchanged by tool_choice.",
        ],
    }
    save(args.output / "manifest.json", manifest)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "live": args.live,
                "planned_requests": 24,
                "base_input_estimates": [
                    provider.estimate_input_tokens(r).tokens for r in requests
                ],
            }
        ),
        flush=True,
    )
    if not args.live:
        return
    probe = Probe(provider, args.output, args.wait_seconds)
    try:
        for case, request in zip(cases, requests, strict=True):
            await probe.run_case(case, request)
    finally:
        save(
            args.output / "summary.json",
            {
                "ended_at": now(),
                "requests": len(probe.records),
                "reserved_usd": probe.reserved_usd,
                "known_cost_usd": sum(r.get("known_cost_usd") or 0 for r in probe.records),
                "missing_usage_requests": sum(not r["raw_usage"] for r in probe.records),
                "diagnostic_requests": probe.diagnostics,
            },
        )


if __name__ == "__main__":
    asyncio.run(main())
