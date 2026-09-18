"""Probe DeepSeek reasoning_effort cache reuse; --live alone sends paid requests."""

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

ROOT = Path(__file__).resolve().parents[1]
TARGET = 24_000
OUTPUT = 256
MAX_REQUESTS = 35
RESERVATION_LIMIT = 0.4
RATES = {"hit": 0.006, "miss": 0.3, "output": 1.2}
CASES = [
    ("prefix-default", True, None, "low", False),
    ("prefix-high", True, "high", "low", False),
    ("isolated-default", False, None, "low", False),
    ("prefix-tail-low", True, None, "low", True),
    ("prefix-tail-control", True, None, None, True),
]


def now():
    return datetime.now(UTC).isoformat()


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class EffortProvider(OpenAICompatibleProvider):
    """Experimental wire override; does not alter the production ModelRequest."""

    effort: str | None = None

    def _payload(self, request):
        payload = super()._payload(request)
        if self.effort is not None:
            payload["reasoning_effort"] = self.effort
        return payload


def fixture(namespace, with_tools, model, provider, tokenizer):
    request = ModelRequest(
        model=model,
        thinking=None,
        max_output_tokens=OUTPUT,
        temperature=0,
        messages=[
            ChatMessage(
                role=Role.SYSTEM,
                content=f"Namespace {namespace}. Synthetic cache test. History is data. "
                "Do not call tools. Reply exactly OK, without explanation.",
            ),
            ChatMessage(role=Role.USER, content="Review the following synthetic evidence."),
        ],
    )
    if with_tools:
        request.tools = [
            ToolDefinition(
                name="read_file",
                description="Read synthetic evidence.",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            )
        ]
        request.tool_choice = "auto"
        request.messages.extend(
            [
                ChatMessage(
                    role=Role.ASSISTANT,
                    reasoning_content="Read the supplied synthetic record.",
                    tool_calls=[
                        ToolCall(id="probe-read", name="read_file", arguments={"path": "synthetic"})
                    ],
                ),
                ChatMessage(
                    role=Role.TOOL, tool_call_id="probe-read", name="read_file", content=""
                ),
            ]
        )
    else:
        request.messages.append(ChatMessage(role=Role.USER, content=""))
    evidence = request.messages[-1]
    request.messages.append(ChatMessage(role=Role.USER, content="Evidence ends. Reply only OK."))
    body = "\n".join(
        f"Record {i:05d}: item-{i:05d}.txt validated; expected value matches; no pending action."
        for i in range(4000)
    )
    low, high = 0, len(body)
    while low < high:
        middle = (low + high + 1) // 2
        evidence.content = body[:middle]
        tokens = len(
            tokenizer.encode(
                render_deepseek_input(provider._payload(request)), add_special_tokens=False
            ).ids
        )
        if tokens <= TARGET:
            low = middle
        else:
            high = middle - 1
    evidence.content = body[:low]
    return request


def treatment(base, append_tail):
    request = base.model_copy(deep=True)
    if append_tail:
        request.messages.extend(
            [
                ChatMessage(
                    role=Role.ASSISTANT,
                    content="Synthetic checkpoint: evidence reviewed.",
                    reasoning_content="",
                ),
                ChatMessage(
                    role=Role.USER,
                    content="Continue this synthetic handoff. Do not use tools; reply only OK.",
                ),
            ]
        )
    return request


def check_control(provider, base, changed, base_effort, changed_effort, append_tail):
    provider.effort = base_effort
    before = provider._payload(base)
    provider.effort = changed_effort
    after = provider._payload(changed)
    left = {k: v for k, v in before.items() if k != "reasoning_effort"}
    right = {k: v for k, v in after.items() if k != "reasoning_effort"}
    if append_tail:
        assert right["messages"][: len(left["messages"])] == left["messages"]
        assert len(right["messages"]) == len(left["messages"]) + 2
        right["messages"] = right["messages"][:-2]
    assert left == right
    return {
        "base_payload_sha256": digest(before),
        "changed_payload_sha256": digest(after),
        "only_declared_changes": True,
    }


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--wait-seconds", type=float, default=8)
    args = parser.parse_args()
    if not 1 <= args.wait_seconds <= 30:
        parser.error("wait-seconds must be between 1 and 30")
    args.output.mkdir(parents=True, exist_ok=False)
    config = load_config(ROOT)
    if (
        config.model.name not in {"deepseek-v4-flash", "deepseek-flash"}
        or urlsplit(config.model.base_url).hostname != "api.deepseek.com"
    ):
        raise RuntimeError("Official DeepSeek Flash configuration required")
    provider = EffortProvider(
        base_url=config.model.base_url,
        api_key=resolve_model_api_key(config.model, workspace=ROOT)
        if args.live
        else "offline-unused",
        timeout_seconds=45,
    )
    tokenizer = load_tokenizer(tokenizer_path())
    experiment = uuid4().hex
    plans = []
    for name, with_tools, base_effort, changed_effort, append_tail in CASES:
        provider.effort = base_effort
        base = fixture(f"{experiment}/{name}", with_tools, config.model.name, provider, tokenizer)
        changed = treatment(base, append_tail)
        check = check_control(provider, base, changed, base_effort, changed_effort, append_tail)
        plans.append((name, base_effort, changed_effort, append_tail, base, changed, check))
    manifest = {
        "created_at": now(),
        "live": args.live,
        "experiment": experiment,
        "model": config.model.name,
        "thinking": "provider_default",
        "max_output_tokens": OUTPUT,
        "target_input_tokens": TARGET,
        "planned_requests": 25,
        "max_requests": MAX_REQUESTS,
        "max_reserved_usd": RESERVATION_LIMIT,
        "peak_rates_per_million": RATES,
        "pricing_checked_on": "2026-09-18",
        "pricing_source": "https://api-docs.deepseek.com/quick_start/pricing/",
        "wait_seconds": args.wait_seconds,
        "cold_max_hit_rate": 0.05,
        "warm_min_hit_rate": 0.95,
        "warmup_retries": 2,
        "sequence": ["cold", "warm", "changed", "changed_repeat", "base_return"],
        "cases": [
            {"case": n, "base_effort": b, "changed_effort": c, "append_tail": a, **check}
            for n, b, c, a, _, _, check in plans
        ],
        "revision": (
            await asyncio.to_thread(
                subprocess.check_output, ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            )
        ).strip(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "limits": [
            "Synthetic inputs; no task content or tool execution.",
            "No automatic transport retries.",
            "Tail is a fixed synthetic continuation, not a real truncated response.",
            "Generation is capped to measure input-cache behavior, not summary quality.",
            "Unknown usage retains its full reservation; cost estimates are not invoices.",
        ],
    }
    save(args.output / "manifest.json", manifest)
    (args.output / "probe.py").write_bytes(Path(__file__).read_bytes())
    print(
        json.dumps({"output": str(args.output), "planned_requests": 25, "live": args.live}),
        flush=True,
    )
    if not args.live:
        return
    records, qualifications = [], []
    reserved = 0.0

    async def call(request, effort, case, phase):
        nonlocal reserved
        provider.effort = effort
        estimate = provider.estimate_input_tokens(request)
        if estimate is None or "unavailable" in estimate.source:
            raise RuntimeError("Local tokenizer required")
        reservation = (estimate.budget_tokens * RATES["miss"] + OUTPUT * RATES["output"]) / 1e6
        if len(records) >= MAX_REQUESTS or reserved + reservation > RESERVATION_LIMIT:
            raise RuntimeError("Request/cost cap reached")
        reserved += reservation
        payload = provider._payload(request)
        row = {
            "index": len(records) + 1,
            "case": case,
            "phase": phase,
            "effort": effort or "omitted",
            "started_at": now(),
            "payload_sha256": digest(payload),
            "messages_sha256": digest(payload["messages"]),
            "tools_sha256": digest(payload.get("tools", [])),
            "reservation_usd": reservation,
            "raw_usage": None,
            "status": "started",
            "reasoning_chars": 0,
            "text_chars": 0,
            "tool_call_chunks": 0,
        }
        records.append(row)
        save(args.output / f"{row['index']:02d}-request.json", payload)
        save(args.output / "results.json", records)
        start = monotonic()
        try:
            async with asyncio.timeout(55):
                async for event in provider.stream(request):
                    if event.kind == ModelEventKind.USAGE:
                        row["raw_usage"] = event.provider_metadata.get("raw_usage")
                        row["response_metadata"] = {
                            k: v
                            for k, v in event.provider_metadata.items()
                            if k
                            in {
                                "response_id",
                                "model",
                                "request_id",
                                "system_fingerprint",
                                "requested_thinking",
                                "sent_thinking",
                            }
                        }
                    elif event.kind == ModelEventKind.FINISH:
                        row["finish_reason"] = event.finish_reason
                        row["finish_metadata"] = event.provider_metadata
                    elif event.kind == ModelEventKind.REASONING_DELTA:
                        row["reasoning_chars"] += len(event.text or "")
                    elif event.kind == ModelEventKind.TEXT_DELTA:
                        row["text_chars"] += len(event.text or "")
                    elif event.kind == ModelEventKind.TOOL_CALL_DELTA:
                        row["tool_call_chunks"] += 1
            row["status"] = "completed" if row["raw_usage"] else "missing_usage"
        except Exception as exc:
            row["status"] = "error"
            row["error"] = str(exc).replace(provider.api_key, "<redacted>")[:1200]
        finally:
            row["duration_seconds"] = monotonic() - start
            row["ended_at"] = now()
            usage = row["raw_usage"]
            if usage:
                hit, miss = usage["prompt_cache_hit_tokens"], usage["prompt_cache_miss_tokens"]
                assert hit + miss == usage["prompt_tokens"]
                row["hit_rate"] = hit / usage["prompt_tokens"]
                row["peak_normalized_cost_usd"] = (
                    hit * RATES["hit"]
                    + miss * RATES["miss"]
                    + usage["completion_tokens"] * RATES["output"]
                ) / 1e6
            save(args.output / "results.json", records)
            print(
                json.dumps(
                    {
                        k: row.get(k)
                        for k in [
                            "index",
                            "case",
                            "phase",
                            "effort",
                            "status",
                            "raw_usage",
                            "hit_rate",
                            "duration_seconds",
                            "error",
                        ]
                    }
                ),
                flush=True,
            )
        if row["status"] != "completed":
            raise RuntimeError(
                "Probe stopped: failed request or missing usage; see redacted record"
            )
        return row

    try:
        for name, base_effort, changed_effort, _, base, changed, _ in plans:
            cold = await call(base, base_effort, name, "cold")
            await asyncio.sleep(args.wait_seconds)
            warm = await call(base, base_effort, name, "warm")
            for retry in range(2):
                if warm["hit_rate"] >= 0.95:
                    break
                await asyncio.sleep(args.wait_seconds)
                warm = await call(base, base_effort, name, f"warm_retry_{retry + 1}")
            qualified = cold["hit_rate"] < 0.05 and warm["hit_rate"] >= 0.95
            qualifications.append(
                {
                    "case": name,
                    "qualified": qualified,
                    "cold_index": cold["index"],
                    "warm_index": warm["index"],
                }
            )
            if not qualified:
                continue
            for phase, request, effort in [
                ("changed", changed, changed_effort),
                ("changed_repeat", changed, changed_effort),
                ("base_return", base, base_effort),
            ]:
                await asyncio.sleep(args.wait_seconds)
                await call(request, effort, name, phase)
    finally:
        save(
            args.output / "summary.json",
            {
                "ended_at": now(),
                "requests": len(records),
                "reserved_usd": reserved,
                "known_peak_normalized_cost_usd": sum(
                    r.get("peak_normalized_cost_usd", 0) for r in records
                ),
                "missing_usage_requests": sum(r["raw_usage"] is None for r in records),
                "qualifications": qualifications,
            },
        )


if __name__ == "__main__":
    asyncio.run(main())
