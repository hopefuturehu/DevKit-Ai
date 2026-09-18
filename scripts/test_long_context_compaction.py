"""Frozen gaming-history compaction probes; only --live sends paid requests.

Uses production request construction, candidate checks and checkpoint publishing
in disposable databases. Path forcing and thinking overrides are experiment-only.
Never executes a model-requested tool or writes to the source database.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit
from uuid import uuid4

from bot.compaction.handoff import protocol_closed
from bot.compaction.service import ContextCompactor
from bot.compaction.strategies import StrategyCompactor, StrategyFrame
from bot.config import load_config, resolve_model_api_key
from bot.core.context import PositionedMessage
from bot.core.events import EventBus, MemoryEventSink
from bot.core.models import ChatMessage, ModelEventKind, ModelRequest, Role
from bot.providers import OpenAICompatibleProvider
from bot.providers.token_counting import load_tokenizer, render_deepseek_input, tokenizer_path
from bot.sessions import SQLiteSessionStore

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path("/Users/huyang/codespace/gaming/.bot/state.db")
SESSION = "a992ba7c14a94eb58cc1305c522b7071"
SOURCE_RUN = "85cab45ef30f4a39afa27218b73afc13"
TARGETS = (96 * 1024, 256 * 1024, 512 * 1024)
OUTPUT = 8192
MAX_CALLS = 60
MAX_RESERVATION = 10.0
RATES = {"hit": 0.006, "miss": 0.3, "output": 1.2}
ORIGINAL_WORDING = "直接输出完整摘要，不解释筛选过程，不调用工具，不继续原任务。"
NEUTRAL_WORDING = "输出完整摘要，不调用工具，不继续原任务。"
MODES = ("prefix_enabled", "isolated_enabled", "isolated_disabled")
# Retrieval checks, NOT semantic judges. Original user text is separately checked verbatim.
REQUIREMENT_PATTERNS = {
    "normal_mode_play": r"正常模式|普通模式|非测试模式",
    "honest_verification": r"未执行|未验证|待验证|待核实|不能算|不得.{0,8}通过|不.{0,8}视为通过",
    "editor_play_roundtrip": r"试玩.{0,40}(返回|回到|编辑器)|编辑器.{0,40}(试玩|往返)",
    "browser_automation": r"浏览器自动化|Playwright|playwright|E2E|e2e",
    "screenshots": r"截图",
    "build_type_test": r"类型检查|typecheck|tsc",
    "acceptance_report": r"验收报告|验收.{0,12}证据",
    "git_delivery": r"Git|git|提交",
}


def now():
    return datetime.now(UTC).isoformat()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def config_for_probe():
    return load_config(
        ROOT,
        overrides={
            "model": {"context_window_tokens": 1_000_000, "max_output_tokens": OUTPUT},
            "context": {
                "compaction_strategy": "a_fallback",
                # Freeze the enabled arm independently of production defaults.
                "compaction_isolated_thinking": "inherit",
                "max_input_tokens": 900_000,
                "compaction_max_output_tokens": OUTPUT,
                "compaction_low_water_tokens": 40_000,
                "compaction_request_timeout_seconds": 300,
            },
        },
    )


def source_history():
    with sqlite3.connect(SOURCE.resolve().as_uri() + "?mode=ro", uri=True) as db:
        entries = [
            PositionedMessage(p, ChatMessage.model_validate_json(raw))
            for p, raw in db.execute(
                "SELECT position,message_json FROM messages WHERE session_id=? ORDER BY position",
                (SESSION,),
            )
        ]
        raw = db.execute(
            "SELECT payload_json FROM events WHERE run_id=? AND "
            "type='context.compaction.request.completed' ORDER BY sequence LIMIT 1",
            (SOURCE_RUN,),
        ).fetchone()[0]
        ref = json.loads(raw)["request_ref"]
        raw = db.execute("SELECT content FROM context_blobs WHERE id=?", (ref,)).fetchone()[0]
        frozen = ModelRequest.model_validate_json(raw)
    assert [e.position for e in entries] == list(range(1, len(entries) + 1))
    assert protocol_closed([e.message for e in entries])
    # Freeze the original system/environment/skill layers and actual tool schema.
    index = next(i for i, m in enumerate(frozen.messages) if m.role == Role.USER and m.name is None)
    assert frozen.messages[index] == entries[0].message
    return frozen.model_copy(
        update={
            "messages": frozen.messages[:index],
            "thinking": "enabled",
            "max_output_tokens": OUTPUT,
        }
    ), entries


def exact_tokens(provider, request):
    tokenizer = load_tokenizer(tokenizer_path())
    return len(
        tokenizer.encode(
            render_deepseek_input(provider._payload(request)), add_special_tokens=False
        ).ids
    )


def build_fixtures(provider):
    template, entries = source_history()
    groups = ContextCompactor._atomic_groups(entries)
    boundaries = [g[-1].position for g in groups]
    fixtures = []
    for target in TARGETS:

        def request_at(end):
            return template.model_copy(
                update={"messages": template.messages + [e.message for e in entries[:end]]}
            )

        low, high = 1, len(boundaries) - 1
        while low < high:
            mid = (low + high + 1) // 2
            if exact_tokens(provider, request_at(boundaries[mid])) <= target:
                low = mid
            else:
                high = mid - 1
        index = min(
            (low, min(low + 1, len(boundaries) - 1)),
            key=lambda i: abs(exact_tokens(provider, request_at(boundaries[i])) - target),
        )
        end = boundaries[index]
        selected = entries[:end]
        tokens = exact_tokens(provider, request_at(end))
        assert abs(tokens / target - 1) < 0.05, (target, tokens)
        # Preserve approximately 4K native tokens, always at a complete tool boundary.
        through = end
        for boundary in reversed(boundaries[:index]):
            tail = [e.message for e in selected if e.position > boundary]
            tail_request = template.model_copy(update={"messages": template.messages + tail})
            tail_tokens = exact_tokens(provider, tail_request) - exact_tokens(provider, template)
            if tail_tokens > 4096:
                break
            through = boundary
        assert through < end
        fixture = {
            "target": target,
            "local_input_tokens": tokens,
            "end": end,
            "through": through,
            "source_sha256": digest([e.message.model_dump(mode="json") for e in selected]),
            "history_reasoning_messages": sum(bool(e.message.reasoning_content) for e in selected),
            "history_reasoning_tokens": sum(
                len(
                    load_tokenizer(tokenizer_path())
                    .encode(e.message.reasoning_content, add_special_tokens=False)
                    .ids
                )
                for e in selected
                if e.message.reasoning_content
            ),
            "template": template,
            "entries": selected,
        }
        fixtures.append(fixture)
    return fixtures


def base_request(fixture, experiment, repeat, thinking="enabled"):
    request = fixture["template"].model_copy(deep=True)
    request.messages[0].content = (
        f"Experiment {experiment}/{fixture['target']}/{repeat}.\n" + request.messages[0].content
    )
    request.messages.extend(e.message.model_copy(deep=True) for e in fixture["entries"])
    request.thinking = thinking
    return request


def frozen_fixtures(directory):
    """Use reviewed bytes, not a live database reread, for a paid experiment."""
    manifest = json.loads((directory / "manifest.json").read_text())
    review = json.loads((directory / "data-review.json").read_text())
    reviewed = {item["fixture"]: item for item in review["fixtures"]}
    fixtures = []
    assert review["same_configured_endpoint"]
    assert review["destination_host"] == "api.deepseek.com"
    for meta in manifest["fixtures"]:
        path = directory / f"fixture-{meta['target'] // 1024}k.json"
        raw = path.read_bytes()
        checked = reviewed[path.name]
        assert hashlib.sha256(raw).hexdigest() == checked["sha256"]
        assert not checked["contains_actual_configured_credentials"]
        assert not any(checked["pattern_match_counts"].values())
        data = json.loads(raw)
        assert digest(data["messages"]) == meta["source_sha256"]
        entries = [
            PositionedMessage(i, ChatMessage.model_validate(m))
            for i, m in enumerate(data["messages"], 1)
        ]
        assert len(entries) == meta["end"]
        assert protocol_closed([e.message for e in entries])
        fixtures.append(
            {**meta, "template": ModelRequest.model_validate(data["template"]), "entries": entries}
        )
    assert [f["target"] for f in fixtures] == list(TARGETS)
    return fixtures


def review_fixtures(directory, config):
    """Record credential-scan counts and endpoint provenance, never credential values."""
    gaming = load_config(SOURCE.parent.parent)
    keys = [
        resolve_model_api_key(config.model, workspace=ROOT),
        resolve_model_api_key(gaming.model, workspace=SOURCE.parent.parent),
    ]
    patterns = {
        "private_key": r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
        "bearer_credential": r'(?i)authorization["\s:=>]+bearer\s+[A-Za-z0-9._-]{16,}',
        "provider_key": r"\bsk-[A-Za-z0-9_-]{24,}",
        "github_token": r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}",
        "aws_key": r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
        "credential_assignment": (
            r"(?i)(?:api[_-]?key|secret|password|access[_-]?token)"
            r"\s*[=:]\s*[\"']?[A-Za-z0-9_./+-]{16,}"
        ),
    }
    rows = []
    for path in sorted(directory.glob("fixture-*.json")):
        raw = path.read_text()
        data = json.loads(raw)
        rows.append(
            {
                "fixture": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "messages": len(data["messages"]),
                "contains_actual_configured_credentials": any(k and k in raw for k in keys),
                "pattern_match_counts": {
                    name: len(re.findall(pat, raw)) for name, pat in patterns.items()
                },
            }
        )
    report = {
        "source_host": urlsplit(gaming.model.base_url).hostname,
        "destination_host": urlsplit(config.model.base_url).hostname,
        "source_model": gaming.model.name,
        "destination_model": config.model.name,
        "same_configured_endpoint": gaming.model.base_url.rstrip("/")
        == config.model.base_url.rstrip("/"),
        "same_credential": keys[0] == keys[1],
        "source_run": SOURCE_RUN,
        "fixtures": rows,
        "limits": "Pattern scan is not a proof that no sensitive information exists.",
    }
    save(directory / "data-review.json", report)
    return report


class Recorder(OpenAICompatibleProvider):
    def __init__(self, *, output, live, **kwargs):
        super().__init__(**kwargs)
        self.output, self.live = output, live
        self.calls = []
        self.reserved = 0.0
        self.label = ""

    async def stream(self, request):
        payload = self._payload(request)
        assert self.api_key not in json.dumps(payload, ensure_ascii=False)
        estimate = self.estimate_input_tokens(request)
        assert estimate is not None and not estimate.source.startswith("heuristic:")
        assert estimate.budget_tokens <= 900_000
        reserve = (estimate.budget_tokens * RATES["miss"] + OUTPUT * RATES["output"]) / 1e6
        if len(self.calls) >= MAX_CALLS or self.reserved + reserve > MAX_RESERVATION:
            raise RuntimeError("experiment_budget_exhausted")
        self.reserved += reserve
        directory = self.output / "requests" / f"{len(self.calls) + 1:03d}-{self.label}"
        directory.mkdir(parents=True)
        save(directory / "request.json", payload)
        row = {
            "index": len(self.calls) + 1,
            "label": self.label,
            "started_at": now(),
            "payload_sha256": digest(payload),
            "local_input_tokens": estimate.tokens,
            "budget_input_tokens": estimate.budget_tokens,
            "reserved_usd": reserve,
            "thinking": request.thinking,
            "max_output_tokens": request.max_output_tokens,
            "path": str(directory.relative_to(self.output)),
            "raw_usage": {},
        }
        self.calls.append(row)
        save(self.output / "calls.json", self.calls)
        if not self.live:
            raise ValueError("offline_capture_only")
        events = []
        began = monotonic()
        try:
            async for event in super().stream(request):
                events.append(event.model_dump(mode="json"))
                if event.kind == ModelEventKind.USAGE:
                    row["raw_usage"] = event.provider_metadata.get("raw_usage", {})
                    row["provider_model"] = event.provider_metadata.get("model")
                    row["response_id"] = event.provider_metadata.get("response_id")
                if event.kind == ModelEventKind.FINISH:
                    row["finish_reason"] = event.finish_reason
                    row["diagnostics"] = event.provider_metadata
                yield event
        except BaseException as exc:
            row["error_type"] = type(exc).__name__
            row["error"] = str(exc).replace(self.api_key, "<redacted>")[:2000]
            raise
        finally:
            row["ended_at"], row["duration_seconds"] = now(), monotonic() - began
            row["reasoning_chars"] = sum(
                len(e.get("text") or "") for e in events if e["kind"] == "reasoning_delta"
            )
            row["body_chars"] = sum(
                len(e.get("text") or "") for e in events if e["kind"] == "text_delta"
            )
            usage = row["raw_usage"]
            if usage:
                when = datetime.fromisoformat(row["started_at"])
                multiplier = (
                    1 if when.weekday() < 5 and (1 <= when.hour < 4 or 6 <= when.hour < 10) else 0.5
                )
                row["cost_estimate_usd"] = (
                    multiplier
                    * (
                        usage["prompt_cache_hit_tokens"] * RATES["hit"]
                        + usage["prompt_cache_miss_tokens"] * RATES["miss"]
                        + usage["completion_tokens"] * RATES["output"]
                    )
                    / 1e6
                )
            save(directory / "events.json", events)
            save(directory / "result.json", row)
            save(self.output / "calls.json", self.calls)
            print(
                json.dumps(
                    {
                        k: row.get(k)
                        for k in [
                            "index",
                            "label",
                            "duration_seconds",
                            "finish_reason",
                            "reasoning_chars",
                            "error_type",
                        ]
                    }
                ),
                flush=True,
            )


class ProbeStrategy(StrategyCompactor):
    """Force one path; all validation and publishing remain production code."""

    mode = "prefix_enabled"
    neutral = False
    requested = None

    async def summarize(self, request, **kwargs):
        if self.mode.startswith("prefix") and kwargs["phase"] != "a_prefix":
            raise ValueError("probe_fallback_suppressed_after_prefix_failure")
        request = request.model_copy(deep=True)
        if self.mode == "isolated_disabled" or (
            self.mode == "fallback_disabled" and kwargs["phase"] == "a_isolated"
        ):
            request.thinking = "disabled"
        if self.neutral:
            assert request.messages[-1].content.count(ORIGINAL_WORDING) == 1
            request.messages[-1].content = request.messages[-1].content.replace(
                ORIGINAL_WORDING, NEUTRAL_WORDING
            )
        self.requested = request.model_copy(deep=True)
        return await super().summarize(request, **kwargs)


def setup_trial(directory, fixture, base, provider, config, mode):
    directory.mkdir(parents=True)
    store = SQLiteSessionStore(directory / "state.db")
    session = store.create_session(directory)
    store.start_run(session, "probe")
    for entry in fixture["entries"]:
        assert store.append_message(session, "probe", entry.message) == entry.position
    sink = MemoryEventSink()
    compactor = ContextCompactor(
        config=config, provider=provider, store=store, event_bus=EventBus([store, sink])
    )
    engine = ProbeStrategy(compactor, provider, session, "probe")
    engine.mode, engine.neutral = mode, mode == "prefix_neutral"
    entries = store.load_positioned_messages(session)
    header_size = len(fixture["template"].messages)

    def project(record):
        return base.model_copy(
            update={
                "messages": base.messages[:header_size]
                + compactor.context_messages(session, record)
                + [e.message for e in entries if e.position > record["covered_end_position"]]
            }
        )

    engine.frame = StrategyFrame(
        request=base,
        project=project,
        evidence=entries,
        prefix_request=base.model_copy(deep=True)
        if mode.startswith(("prefix", "fallback"))
        else None,
        prefix_positions=(None,) * header_size + tuple(e.position for e in entries),
        prefix_skip_reason="experiment_forced_isolated" if mode.startswith("isolated") else None,
    )
    return engine, store, sink


async def trial(output, fixture, base, provider, config, mode, repeat):
    label = f"{fixture['target'] // 1024}k-r{repeat}-{mode}"
    directory = output / "trials" / label
    engine, store, sink = setup_trial(directory, fixture, base, provider, config, mode)
    provider.label = label
    before_calls = len(provider.calls)
    try:
        result = await engine.compact(fixture["through"], [1])
        events = [event.model_dump(mode="json") for event in sink.events]
        save(directory / "events.json", events)
        terminal = [
            e["payload"]
            for e in events
            if e["type"]
            in {"context.compaction.request.completed", "context.compaction.request.failed"}
        ]
        assert len(provider.calls) == before_calls + 1, result
        assert len(terminal) == 1, terminal
        meta = terminal[0]
        with sqlite3.connect(directory / "state.db") as db:
            response = json.loads(
                db.execute(
                    "SELECT content FROM context_blobs WHERE id=?", (meta["response_ref"],)
                ).fetchone()[0]
            )
        summary = response["text"]
        (directory / "summary.md").write_text(summary)
        save(directory / "effective-request.json", engine.requested.model_dump(mode="json"))
        usage = meta.get("raw_usage") or {}
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        if (
            reasoning is None
            and mode == "isolated_disabled"
            and usage
            and not response["reasoning"]
        ):
            reasoning = 0
        candidate = False
        try:
            engine.compactor._validate_candidate(
                summary,
                finish_reason=meta.get("finish_reason"),
                covered_start=1,
                covered_end=result.covered_end_position,
            )
            candidate = (
                meta.get("finish_reason") in {"stop", "end_turn", "stop_sequence"}
                and not response["tool_calls"]
            )
        except ValueError:
            pass
        projection = engine.compactor.projection(engine.session_id)
        anchors_ok, tail_ok, after_tokens = False, False, None
        if result.compacted:
            checkpoint = projection["compaction"]
            projected = engine.frame.project(checkpoint)
            anchors_ok = any(m == fixture["entries"][0].message for m in projected.messages)
            tail = [
                e.message for e in fixture["entries"] if e.position > result.covered_end_position
            ]
            tail_ok = projected.messages[-len(tail) :] == tail if tail else True
            after_tokens = engine.count(projected)
            assert anchors_ok and tail_ok and after_tokens <= 40_000
            assert checkpoint["covered_end_position"] == result.covered_end_position
        else:
            assert projection["cursor_position"] == 0 and projection["compaction"] is None
        row = {
            "id": label,
            "mode": mode,
            "repeat": repeat,
            "target_input_tokens": fixture["target"],
            "candidate_pass": candidate,
            "publish_pass": result.compacted,
            "anchor_verbatim": anchors_ok,
            "tail_verbatim": tail_ok,
            "after_budget_tokens": after_tokens,
            "covered_end": result.covered_end_position,
            "requested_covered_end": fixture["through"],
            "source_sha256": fixture["source_sha256"],
            "error": meta.get("error") or result.error,
            "finish_reason": meta.get("finish_reason"),
            "duration_seconds": meta.get("duration_ms", 0) / 1000,
            "within_90_seconds": meta.get("duration_ms", 0) <= 90_000 and result.compacted,
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": reasoning,
            "body_tokens": usage["completion_tokens"] - reasoning
            if usage and reasoning is not None
            else None,
            "summary_sha256": digest(summary),
            "request_sha256": provider.calls[-1]["payload_sha256"],
            "request_index": len(provider.calls),
            "requirement_keyword_hits": {
                key: bool(re.search(pattern, summary, re.S))
                for key, pattern in REQUIREMENT_PATTERNS.items()
            },
            "operation_result": result.model_dump(mode="json"),
        }
        save(directory / "result.json", row)
        return row
    finally:
        store.close()


async def warm(provider, base, label):
    results = []
    for attempt in range(2):
        provider.label = f"{label}-warm{attempt + 1}"
        try:
            async with asyncio.timeout(300):
                async for _ in provider.stream(base):
                    pass  # No tool executor exists in this harness.
        except Exception:
            pass
        row = provider.calls[-1]
        usage = row["raw_usage"]
        ratio = usage.get("prompt_cache_hit_tokens", 0) / max(usage.get("prompt_tokens", 0), 1)
        results.append(
            {
                "request_index": row["index"],
                "cache_hit_rate": ratio,
                "qualified": ratio >= 0.95 and bool(usage),
            }
        )
        await asyncio.sleep(8)
        if results[-1]["qualified"]:
            break
    return results


def check_pairs(output, results, fixtures):
    checks = []
    for fixture in fixtures:
        for repeat in range(1, 4):
            rows = {
                r["mode"]: r
                for r in results
                if r["target_input_tokens"] == fixture["target"] and r["repeat"] == repeat
            }

            def payload(mode, rows=rows):
                return json.loads(
                    (output / "trials" / rows[mode]["id"] / "effective-request.json").read_text()
                )

            on, off = payload("isolated_enabled"), payload("isolated_disabled")
            assert on.pop("thinking") == "enabled" and off.pop("thinking") == "disabled"
            assert on == off, "isolated pair changed more than thinking"
            assert len({r["covered_end"] for r in rows.values()}) == 1
            if "prefix_default" in rows:
                default, enabled, neutral = (
                    payload(m) for m in ("prefix_default", "prefix_enabled", "prefix_neutral")
                )
                assert default.pop("thinking") is None
                assert enabled.pop("thinking") == "enabled"
                assert default == enabled
                assert neutral.pop("thinking") == "enabled"
                neutral["messages"][-1]["content"] = neutral["messages"][-1]["content"].replace(
                    NEUTRAL_WORDING, ORIGINAL_WORDING
                )
                assert neutral == enabled
            checks.append(
                {
                    "target": fixture["target"],
                    "repeat": repeat,
                    "only_declared_variables_changed": True,
                    "same_coverage_boundary": True,
                }
            )
    return checks


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--fixtures-from", type=Path)
    args = parser.parse_args()
    if args.live and not args.fixtures_from:
        parser.error("--live requires --fixtures-from with a reviewed frozen preflight")
    args.output.mkdir(parents=True, exist_ok=False)
    config = config_for_probe()
    assert config.model.name == "deepseek-v4-flash"
    assert urlsplit(config.model.base_url).hostname == "api.deepseek.com"
    provider = Recorder(
        output=args.output,
        live=args.live,
        base_url=config.model.base_url,
        api_key=resolve_model_api_key(config.model, workspace=ROOT)
        if args.live
        else "offline-unused",
        timeout_seconds=300,
    )
    fixtures = (
        frozen_fixtures(args.fixtures_from) if args.fixtures_from else build_fixtures(provider)
    )
    experiment = uuid4().hex
    manifest = {
        "created_at": now(),
        "live": args.live,
        "experiment": experiment,
        "source_session": SESSION,
        "source_run": SOURCE_RUN,
        "fixtures_from": str(args.fixtures_from) if args.fixtures_from else None,
        "fixtures": [
            {k: v for k, v in f.items() if k not in {"template", "entries"}} for f in fixtures
        ],
        "summary_requests": 33,
        "main_matrix_requests": 27,
        "diagnostic_extra_requests": 6,
        "max_warmups": 24,
        "max_total_calls": MAX_CALLS,
        "max_reserved_usd": MAX_RESERVATION,
        "max_output_tokens": OUTPUT,
        "context_window_tokens": 1_000_000,
        "input_limit": 900_000,
        "low_water_tokens": 40_000,
        "timeout_seconds": 300,
        "rates_per_million_peak": RATES,
        "pricing_checked_at": "2026-09-18",
        "pricing_source": "https://api-docs.deepseek.com/quick_start/pricing/",
        "revision": (
            await asyncio.to_thread(
                subprocess.check_output, ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            )
        ).strip(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "neutral_wording_change": [ORIGINAL_WORDING, NEUTRAL_WORDING],
        "limitations": [
            "One nested real trajectory, three repeats, not independent task samples.",
            "Reconstructed prefix with frozen initial layers; not an original production request.",
            "8K output cap differs from uncapped historical gaming prefix requests.",
            "Forced individual paths; isolated success is not observed natural fallback success.",
            "Keyword recall is a retrieval diagnostic, not semantic fidelity scoring.",
            "Pinned V4 tokenizer estimates; actual API route and usage are recorded.",
        ],
    }
    save(args.output / "manifest.json", manifest)
    (args.output / "probe.py").write_bytes(Path(__file__).read_bytes())
    for fixture in fixtures:
        save(
            args.output / f"fixture-{fixture['target'] // 1024}k.json",
            {
                "template": fixture["template"].model_dump(mode="json"),
                "messages": [e.message.model_dump(mode="json") for e in fixture["entries"]],
            },
        )
    if not args.live:
        review_fixtures(args.output, config)
    print(json.dumps({"preflight": manifest["fixtures"], "live": args.live}), flush=True)
    results, warmups = [], []
    for fixture in fixtures:
        for repeat in range(1, 4):
            base = base_request(fixture, experiment, repeat)
            default = base.model_copy(update={"thinking": None})
            # 96K diagnostic order alternates; isolate order alternates at every length.
            prefix_order = (
                ["prefix_default", "prefix_enabled", "prefix_neutral"]
                if fixture == fixtures[0]
                else ["prefix_enabled"]
            )
            if repeat == 2:
                prefix_order.reverse()
            warmed = set()
            for mode in prefix_order:
                request = default if mode == "prefix_default" else base
                key = request.thinking
                if args.live and key not in warmed:
                    warmups.append(
                        {
                            "target": fixture["target"],
                            "repeat": repeat,
                            "thinking": key,
                            "attempts": await warm(
                                provider,
                                request,
                                f"{fixture['target'] // 1024}k-r{repeat}-{key or 'default'}",
                            ),
                        }
                    )
                    warmed.add(key)
                    save(args.output / "warmups.json", warmups)
                results.append(
                    await trial(args.output, fixture, request, provider, config, mode, repeat)
                )
                save(args.output / "results.json", results)
            isolated_order = ["isolated_enabled", "isolated_disabled"]
            if repeat % 2 == 0:
                isolated_order.reverse()
            for mode in isolated_order:
                results.append(
                    await trial(args.output, fixture, base, provider, config, mode, repeat)
                )
                save(args.output / "results.json", results)
    checks = check_pairs(args.output, results, fixtures)
    summary = {
        "checks": checks,
        "requests": len(provider.calls),
        "reserved_usd": provider.reserved,
        "estimated_cost_usd": sum(r.get("cost_estimate_usd", 0) for r in provider.calls),
        "unknown_usage_requests": sum(not r["raw_usage"] for r in provider.calls),
        "by_group": [],
    }
    for target, mode in dict.fromkeys((r["target_input_tokens"], r["mode"]) for r in results):
        rows = [r for r in results if r["target_input_tokens"] == target and r["mode"] == mode]
        summary["by_group"].append(
            {
                "target": target,
                "mode": mode,
                "n": len(rows),
                "candidate_pass": sum(r["candidate_pass"] for r in rows),
                "publish_pass": sum(r["publish_pass"] for r in rows),
                "reasoning_tokens": [r["reasoning_tokens"] for r in rows],
                "body_tokens": [r["body_tokens"] for r in rows],
                "finish_reasons": dict(Counter(r["finish_reason"] for r in rows)),
            }
        )
    save(args.output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
