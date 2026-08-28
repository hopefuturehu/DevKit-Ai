from __future__ import annotations

import csv
import hashlib
import json
import re
import statistics
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from bot.config.models import AppConfig
from bot.core import AgentRunner, RunRequest
from bot.core.context import ContextAssembler, TokenEstimator
from bot.core.events import AgentEvent, EventBus, EventType, MemoryEventSink
from bot.core.models import (
    ChatMessage,
    ModelCapabilities,
    ModelEvent,
    ModelEventKind,
    ModelRequest,
    Role,
    ToolCall,
)
from bot.execution import LocalExecutionTarget
from bot.policy import DefaultPolicyEngine
from bot.providers import ModelProvider
from bot.sessions import SQLiteSessionStore
from bot.skills import SkillCatalog, SkillManager
from bot.tools import ToolRegistry
from bot.tools.base import Tool, ToolAnnotations, ToolContext, ToolResult

RetrievalProviderKind = Literal["scripted", "live"]

_CASE_PATTERN = re.compile(r"\[RETRIEVAL_CASE=([a-z_-]+)]")
_FACT_PATTERN = re.compile(r"\[RETRIEVAL_FACT (?P<case>[a-z_-]+)=(?P<value>[0-9a-f]{20})]")
_REFERENCE_PATTERN = re.compile(r"context_ref=(blob:[0-9a-f]{64})")


@dataclass(frozen=True)
class RetrievalScenario:
    name: str
    sources: tuple[str, ...]
    requested_source: str | None
    lookup_key: str | None
    expected_source_calls: int
    expected_reference_calls: int
    needs_fact: bool = True


RETRIEVAL_SCENARIOS: tuple[RetrievalScenario, ...] = (
    RetrievalScenario("irrelevant", ("primary",), None, None, 1, 0, needs_fact=False),
    RetrievalScenario("preview", ("primary",), "primary", "PREVIEW-KEY", 1, 0),
    RetrievalScenario("middle", ("primary",), "primary", "MIDDLE-KEY", 1, 1),
    RetrievalScenario("no_match_retry", ("primary",), "primary", "RECOVERY-KEY", 1, 2),
    RetrievalScenario("multi_match", ("primary",), "primary", "SHARED-KEY", 1, 1),
    RetrievalScenario("multi_blob", ("alpha", "beta"), "beta", "BETA-KEY", 2, 1),
)


def retrieval_scenario(name: str) -> RetrievalScenario:
    try:
        return next(item for item in RETRIEVAL_SCENARIOS if item.name == name)
    except StopIteration as exc:
        raise ValueError(f"未知 retrieval scenario: {name}") from exc


@dataclass(frozen=True)
class RetrievalRequestTrace:
    request_index: int
    prompt_tokens: int
    output_tokens: int
    usage_reported: bool
    elapsed_seconds: float
    message_count: int
    requested_tool_names: tuple[str, ...]


@dataclass(frozen=True)
class ContextRetrievalBehaviorResult:
    scenario: RetrievalScenario
    provider_kind: RetrievalProviderKind
    workspace: Path
    summary: dict[str, Any]
    traces: tuple[RetrievalRequestTrace, ...]

    def write_artifacts(self) -> None:
        artifacts = self.workspace / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "summary.json").write_text(
            json.dumps(self.summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with (artifacts / "requests.jsonl").open("w", encoding="utf-8") as handle:
            for trace in self.traces:
                handle.write(json.dumps(asdict(trace), ensure_ascii=False, sort_keys=True) + "\n")


class RetrievalFixtureTool(Tool):
    name = "retrieval_fixture"
    description = "返回指定数据源的完整诊断结果；结果可能很大。"
    input_schema = {
        "type": "object",
        "properties": {
            "case": {"type": "string"},
            "source": {"type": "string"},
        },
        "required": ["case", "source"],
        "additionalProperties": False,
    }
    annotations = ToolAnnotations(read_only=True, idempotent=True, output_limit=128_000)

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        del context
        case = str(arguments["case"])
        source = str(arguments["source"])
        scenario = retrieval_scenario(case)
        if source not in scenario.sources:
            return ToolResult(success=False, error=f"case={case} 不存在 source={source}")
        return ToolResult(
            success=True,
            output=retrieval_fixture_output(scenario, source),
            metadata={"case": case, "source": source},
        )


class RetrievalVerifyTool(Tool):
    name = "retrieval_verify"
    description = "校验从诊断结果中提取出的精确事实。"
    input_schema = {
        "type": "object",
        "properties": {
            "case": {"type": "string"},
            "value": {"type": "string"},
        },
        "required": ["case", "value"],
        "additionalProperties": False,
    }
    annotations = ToolAnnotations(read_only=True, idempotent=True)

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        del context
        case = str(arguments["case"])
        expected = retrieval_fact(case)
        value = str(arguments["value"])
        if value != expected:
            return ToolResult(success=False, error=f"事实校验失败: case={case}")
        return ToolResult(
            success=True,
            output=f"verification=ok [RETRIEVAL_FACT {case}={expected}]",
            metadata={"case": case},
        )


def retrieval_fact(case: str) -> str:
    return hashlib.sha256(f"retrieval-behavior:{case}".encode()).hexdigest()[:20]


def retrieval_fixture_output(scenario: RetrievalScenario, source: str, size: int = 24_000) -> str:
    header = (
        f"retrieval-fixture case={scenario.name} source={source} status=complete\n"
        "The index hint can be stale; use evidence from this result.\n"
    )
    body = bytearray((header + ("diagnostic-noise\n" * 2_000)).encode("ascii")[:size])
    if len(body) < size:
        body.extend(b"x" * (size - len(body)))
    fact = f"[RETRIEVAL_FACT {scenario.name}={retrieval_fact(scenario.name)}]"

    inserts: list[tuple[int, str]] = []
    if scenario.name == "preview":
        inserts.append((256, f"PREVIEW-KEY {fact}"))
    elif scenario.name == "middle":
        inserts.append((size // 2, f"MIDDLE-KEY {fact}"))
    elif scenario.name == "no_match_retry":
        inserts.extend(
            [
                (320, "index_hint=STALE-KEY"),
                (size // 2, f"RECOVERY-KEY {fact}"),
            ]
        )
    elif scenario.name == "multi_match":
        inserts.extend(
            [
                (size // 2 - 300, "SHARED-KEY decoy=ignore-this-record"),
                (size // 2 + 300, f"SHARED-KEY authoritative=true {fact}"),
            ]
        )
    elif scenario.name == "multi_blob" and source == "beta":
        inserts.append((size // 2, f"BETA-KEY source=beta {fact}"))
    for offset, text in inserts:
        encoded = text.encode("ascii")
        body[offset : offset + len(encoded)] = encoded
    return body.decode("ascii")


class ScriptedRetrievalProvider(ModelProvider):
    """A deterministic policy used to gate the retrieval plumbing offline."""

    def __init__(self, scenario: RetrievalScenario) -> None:
        self.scenario = scenario
        self.estimator = TokenEstimator()

    def capabilities(self, model: str) -> ModelCapabilities:
        del model
        return ModelCapabilities(usage_reporting=True)

    def count_tokens(self, request: ModelRequest) -> int | None:
        return self.estimator.request(request.messages, request.tools)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        response = self._response(request.messages)
        if response.tool_calls:
            for index, call in enumerate(response.tool_calls):
                yield ModelEvent(
                    kind=ModelEventKind.TOOL_CALL_DELTA,
                    tool_index=index,
                    tool_call_id=call.id,
                    tool_name=call.name,
                    arguments_delta=json.dumps(call.arguments, separators=(",", ":")),
                )
        else:
            yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text=response.content)
        input_tokens = self.estimator.request(request.messages, request.tools)
        output_tokens = self.estimator.message(response)
        yield ModelEvent(
            kind=ModelEventKind.USAGE,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            provider_metadata={"simulated": True},
        )
        yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")

    def _response(self, messages: list[ChatMessage]) -> ChatMessage:
        missing_sources = [
            source
            for source in self.scenario.sources
            if not _has_tool_result(
                messages,
                f"retrieval-source-{self.scenario.name}-{source}",
            )
        ]
        if missing_sources:
            return _tool_response(
                *(
                    ToolCall(
                        id=f"retrieval-source-{self.scenario.name}-{source}",
                        name=RetrievalFixtureTool.name,
                        arguments={"case": self.scenario.name, "source": source},
                    )
                    for source in missing_sources
                )
            )

        if not self.scenario.needs_fact:
            return ChatMessage(
                role=Role.ASSISTANT,
                content=f"[RETRIEVAL_DONE={self.scenario.name}]",
            )

        visible = _facts_in_messages(messages)
        expected = retrieval_fact(self.scenario.name)
        verify_id = f"retrieval-verify-{self.scenario.name}"
        if visible.get(self.scenario.name) == expected:
            if not _has_tool_result(messages, verify_id):
                return _tool_response(
                    ToolCall(
                        id=verify_id,
                        name=RetrievalVerifyTool.name,
                        arguments={"case": self.scenario.name, "value": expected},
                    )
                )
            return ChatMessage(
                role=Role.ASSISTANT,
                content=f"[RETRIEVAL_FACT {self.scenario.name}={expected}]",
            )

        source = self.scenario.requested_source or self.scenario.sources[0]
        source_call_id = f"retrieval-source-{self.scenario.name}-{source}"
        reference = _reference_for_tool_result(messages, source_call_id)
        if reference is None:
            return ChatMessage(role=Role.ASSISTANT, content="[RETRIEVAL_MISSING_REFERENCE]")
        prior_reference_calls = sum(
            message.role == Role.TOOL
            and message.name == "load_context_reference"
            and (message.tool_call_id or "").startswith(f"retrieval-ref-{self.scenario.name}")
            for message in messages
        )
        query = self.scenario.lookup_key or ""
        if self.scenario.name == "no_match_retry" and prior_reference_calls == 0:
            query = "STALE-KEY-NOT-PRESENT"
        return _tool_response(
            ToolCall(
                id=f"retrieval-ref-{self.scenario.name}-{prior_reference_calls + 1}",
                name="load_context_reference",
                arguments={
                    "reference": reference,
                    "query": query,
                    "max_matches": 4,
                    "context_chars": 420,
                    "case_sensitive": True,
                },
            )
        )


class RecordingRetrievalProvider(ModelProvider):
    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider
        self.estimator = TokenEstimator()
        self.traces: list[RetrievalRequestTrace] = []

    def capabilities(self, model: str) -> ModelCapabilities:
        return self.provider.capabilities(model)

    def count_tokens(self, request: ModelRequest) -> int | None:
        return self.provider.count_tokens(request) or self.estimator.request(
            request.messages, request.tools
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        started = time.perf_counter()
        reported_input: int | None = None
        reported_output: int | None = None
        requested_tools: list[str] = []
        generated_chars = 0
        try:
            async for event in self.provider.stream(request):
                if event.kind == ModelEventKind.USAGE:
                    reported_input = event.input_tokens
                    reported_output = event.output_tokens
                elif event.kind == ModelEventKind.TOOL_CALL_DELTA:
                    if event.tool_name:
                        requested_tools.append(event.tool_name)
                    generated_chars += len(event.arguments_delta or "")
                elif event.kind == ModelEventKind.TEXT_DELTA:
                    generated_chars += len(event.text or "")
                yield event
        finally:
            self.traces.append(
                RetrievalRequestTrace(
                    request_index=len(self.traces) + 1,
                    prompt_tokens=(
                        reported_input
                        if reported_input is not None
                        else self.estimator.request(request.messages, request.tools)
                    ),
                    output_tokens=(
                        reported_output
                        if reported_output is not None
                        else max(1, generated_chars // 4)
                    ),
                    usage_reported=reported_input is not None and reported_output is not None,
                    elapsed_seconds=time.perf_counter() - started,
                    message_count=len(request.messages),
                    requested_tool_names=tuple(dict.fromkeys(requested_tools)),
                )
            )


async def run_context_retrieval_behavior_case(
    *,
    scenario: RetrievalScenario,
    workspace: Path,
    provider_kind: RetrievalProviderKind = "scripted",
    provider: ModelProvider | None = None,
    base_config: AppConfig | None = None,
    require_reported_usage: bool = False,
) -> ContextRetrievalBehaviorResult:
    if (provider is None) != (base_config is None):
        raise ValueError("provider 和 base_config 必须同时提供或同时省略")
    if provider_kind == "live" and provider is None:
        raise ValueError("live case 必须提供真实 Provider")

    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    skills_dir = workspace / "skills"
    skills_dir.mkdir(exist_ok=True)
    config = _retrieval_config(base_config)
    recording = RecordingRetrievalProvider(provider or ScriptedRetrievalProvider(scenario))
    store = SQLiteSessionStore(workspace / "state.db")
    events = MemoryEventSink()
    event_bus = EventBus([store, events])
    session_id = store.create_session(workspace)
    catalog = SkillCatalog(skills_dir)
    catalog.scan()
    execution_target = LocalExecutionTarget()
    tools = ToolRegistry()
    tools.register(RetrievalFixtureTool())
    tools.register(RetrievalVerifyTool())
    runner = AgentRunner(
        config=config,
        workspace=workspace,
        provider=recording,
        tool_registry=tools,
        policy=DefaultPolicyEngine(config.permissions, workspace),
        execution_target=execution_target,
        skills=SkillManager(catalog),
        context=ContextAssembler(workspace=workspace, skill_catalog=catalog),
        store=store,
        event_bus=event_bus,
    )

    try:
        result = await runner.run(
            RunRequest(prompt=_scenario_prompt(scenario), session_id=session_id)
        )
        requested = [
            event
            for event in events.events
            if event.type == EventType.TOOL_REQUESTED
        ]
        source_events = _tool_events(requested, RetrievalFixtureTool.name)
        reference_events = _tool_events(requested, "load_context_reference")
        verify_events = _tool_events(requested, RetrievalVerifyTool.name)
        reference_queries = [
            str(event.payload.get("arguments", {}).get("query", ""))
            for event in reference_events
            if "query" in event.payload.get("arguments", {})
        ]
        range_calls = sum(
            "query" not in event.payload.get("arguments", {}) for event in reference_events
        )
        empty_query_results = _empty_reference_result_count(
            store.load_messages(session_id),
        )
        source_messages = [
            message
            for message in store.load_messages(session_id)
            if message.role == Role.TOOL and message.name == RetrievalFixtureTool.name
        ]
        final_facts = _facts_in_text(result.final_text)
        expected = retrieval_fact(scenario.name) if scenario.needs_fact else None
        reference_call_count_ok = len(reference_events) == scenario.expected_reference_calls
        if provider_kind == "live" and scenario.name == "no_match_retry":
            # A live model may ignore the stale preview hint and use the task's
            # authoritative key directly. Both the direct path and one bounded
            # recovery are acceptable; the scripted gate always exercises retry.
            reference_call_count_ok = 1 <= len(reference_events) <= 2
        gates = {
            "run_completed": result.status == "completed",
            "source_calls_exact": len(source_events) == scenario.expected_source_calls,
            "reference_calls_within_budget": reference_call_count_ok,
            "query_mode_only": range_calls == 0,
            "verification_exact": len(verify_events) == (1 if scenario.needs_fact else 0),
            "final_fact_exact": (
                final_facts.get(scenario.name) == expected
                if scenario.needs_fact
                else f"[RETRIEVAL_DONE={scenario.name}]" in result.final_text
            ),
            "large_results_externalized": len(source_messages) == scenario.expected_source_calls
            and all("chars externalized" in (message.content or "") for message in source_messages),
            "reported_usage_available": (
                all(trace.usage_reported for trace in recording.traces)
                if require_reported_usage
                else True
            ),
        }
        if scenario.name == "no_match_retry":
            gates["direct_or_recovers_after_empty_match"] = (
                empty_query_results == 0 and len(reference_events) == 1
            ) or (empty_query_results >= 1 and len(reference_events) == 2)
        if scenario.name == "multi_match":
            gates["multi_match_uses_one_query"] = len(reference_events) == 1
        if scenario.name == "multi_blob":
            beta_call_id = f"retrieval-source-{scenario.name}-beta"
            beta_reference = _reference_for_tool_result(
                store.load_messages(session_id), beta_call_id
            )
            gates["selects_correct_blob"] = bool(reference_events) and (
                reference_events[0].payload.get("arguments", {}).get("reference")
                == beta_reference
            )

        latencies = [trace.elapsed_seconds for trace in recording.traces]
        summary: dict[str, Any] = {
            "schema_version": 1,
            "benchmark": "context-retrieval-behavior",
            "provider": provider_kind,
            "scenario": asdict(scenario),
            "metrics": {
                "model_requests": len(recording.traces),
                "input_tokens": sum(trace.prompt_tokens for trace in recording.traces),
                "output_tokens": sum(trace.output_tokens for trace in recording.traces),
                "source_tool_calls": len(source_events),
                "reference_tool_calls": len(reference_events),
                "query_calls": len(reference_queries),
                "range_calls": range_calls,
                "empty_query_results": empty_query_results,
                "verify_tool_calls": len(verify_events),
                "cost_usd": float(result.cost_usd or 0),
                "request_latency_p50_seconds": statistics.median(latencies)
                if latencies
                else 0.0,
                "request_latency_max_seconds": max(latencies, default=0.0),
            },
            "observed": {
                "reference_queries": reference_queries,
                "final_text": result.final_text,
            },
            "quality": {"passed": all(gates.values()), "gates": gates},
        }
    finally:
        await execution_target.aclose()
        store.close()

    benchmark = ContextRetrievalBehaviorResult(
        scenario=scenario,
        provider_kind=provider_kind,
        workspace=workspace,
        summary=summary,
        traces=tuple(recording.traces),
    )
    benchmark.write_artifacts()
    return benchmark


async def run_context_retrieval_behavior_suite(
    *,
    workspace: Path,
    provider_kind: RetrievalProviderKind = "scripted",
    provider: ModelProvider | None = None,
    base_config: AppConfig | None = None,
    require_reported_usage: bool = False,
    scenarios: Iterable[RetrievalScenario] = RETRIEVAL_SCENARIOS,
) -> tuple[tuple[ContextRetrievalBehaviorResult, ...], dict[str, Any]]:
    results = []
    for scenario in scenarios:
        results.append(
            await run_context_retrieval_behavior_case(
                scenario=scenario,
                workspace=workspace / scenario.name,
                provider_kind=provider_kind,
                provider=provider,
                base_config=base_config,
                require_reported_usage=require_reported_usage,
            )
        )
    totals = {
        key: sum(float(result.summary["metrics"][key]) for result in results)
        for key in (
            "model_requests",
            "input_tokens",
            "output_tokens",
            "source_tool_calls",
            "reference_tool_calls",
            "query_calls",
            "range_calls",
            "empty_query_results",
            "verify_tool_calls",
            "cost_usd",
        )
    }
    summary = {
        "schema_version": 1,
        "benchmark": "context-retrieval-behavior-suite",
        "provider": provider_kind,
        "case_count": len(results),
        "metrics": totals,
        "cases": {
            result.scenario.name: {
                "passed": result.summary["quality"]["passed"],
                **result.summary["metrics"],
            }
            for result in results
        },
        "quality": {
            "passed": bool(results)
            and all(result.summary["quality"]["passed"] for result in results)
        },
    }
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with (workspace / "cases.csv").open("w", newline="", encoding="utf-8") as handle:
        rows = [
            {
                "scenario": result.scenario.name,
                "passed": result.summary["quality"]["passed"],
                **result.summary["metrics"],
            }
            for result in results
        ]
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return tuple(results), summary


def _retrieval_config(base_config: AppConfig | None) -> AppConfig:
    payload = base_config.model_dump(mode="python") if base_config is not None else {}
    payload.setdefault("model", {})
    payload["model"].update(
        {
            "base_url": payload["model"].get("base_url") or "https://offline.invalid",
            "name": payload["model"].get("name") or "retrieval-behavior-model",
            "context_window_tokens": payload["model"].get("context_window_tokens") or 48_000,
            "max_output_tokens": 2_048,
        }
    )
    payload.setdefault("agent", {})
    payload["agent"].update(
        {
            "max_steps": 12,
            "process_wait_seconds": 0,
            "progress": {"enabled": False},
            "finalization": {"enabled": False},
        }
    )
    payload["subagents"] = {**payload.get("subagents", {}), "enabled": False}
    payload["permissions"] = {
        **payload.get("permissions", {}),
        "mode": "read-only",
        "workspace_only": True,
        "network": "deny",
    }
    payload.setdefault("context", {})
    payload["context"].update(
        {
            "max_input_tokens": min(
                int(payload["context"].get("max_input_tokens", 24_000)),
                int(payload["model"]["context_window_tokens"]),
            ),
            "auto_compact_threshold": 0.99,
            "output_reserve_tokens": 2_048,
            "protocol_reserve_tokens": 512,
            "safety_margin_tokens": 512,
            "tool_result_inline_tokens": 400,
            "tool_result_head_chars": 1_200,
            "tool_result_tail_chars": 300,
        }
    )
    return AppConfig.model_validate(payload)


def _scenario_prompt(scenario: RetrievalScenario) -> str:
    lines = [
        f"[RETRIEVAL_CASE={scenario.name}]",
        "Use retrieval_fixture once for each source listed below and answer from its evidence.",
        f"Sources: {', '.join(scenario.sources)}.",
    ]
    if scenario.needs_fact:
        lines.extend(
            [
                f"Find the authoritative value attached to {scenario.lookup_key} in "
                f"source {scenario.requested_source}.",
                "Validate the exact value with retrieval_verify, then return the exact "
                f"[RETRIEVAL_FACT {scenario.name}=<value>] marker.",
            ]
        )
    else:
        lines.append(
            f"No detailed record is needed; after inspecting metadata return "
            f"[RETRIEVAL_DONE={scenario.name}]."
        )
    return "\n".join(lines)


def _tool_response(*calls: ToolCall) -> ChatMessage:
    return ChatMessage(role=Role.ASSISTANT, tool_calls=list(calls))


def _has_tool_result(messages: Iterable[ChatMessage], call_id: str) -> bool:
    return any(
        message.role == Role.TOOL and message.tool_call_id == call_id for message in messages
    )


def _reference_for_tool_result(messages: Iterable[ChatMessage], call_id: str) -> str | None:
    for message in reversed(list(messages)):
        if message.role != Role.TOOL or message.tool_call_id != call_id:
            continue
        match = _REFERENCE_PATTERN.search(message.content or "")
        if match:
            return match.group(1)
    return None


def _facts_in_text(text: str) -> dict[str, str]:
    return {match.group("case"): match.group("value") for match in _FACT_PATTERN.finditer(text)}


def _facts_in_messages(messages: Iterable[ChatMessage]) -> dict[str, str]:
    return _facts_in_text("\n".join(message.content or "" for message in messages))


def _tool_events(events: Iterable[AgentEvent], name: str) -> list[AgentEvent]:
    return [event for event in events if str(event.payload.get("name") or "") == name]


def _empty_reference_result_count(messages: Iterable[ChatMessage]) -> int:
    count = 0
    for message in messages:
        if message.role != Role.TOOL or message.name != "load_context_reference":
            continue
        content = message.content or ""
        if '"returned_matches": 0' in content or '"matches": []' in content:
            count += 1
    return count
