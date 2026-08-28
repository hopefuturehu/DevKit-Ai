from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import statistics
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

from bot.compaction import ContextCompactor
from bot.config.models import AppConfig
from bot.core import AgentRunner, RunRequest
from bot.core.context import ContextAssembler, PositionedMessage, TokenEstimator
from bot.core.events import EventBus, EventType, MemoryEventSink
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

ContextEfficiencyVariant = Literal[
    "raw",
    "compact-inline",
    "query-persistent",
    "range-one-shot",
    "current",
]

CONTEXT_EFFICIENCY_VARIANTS: tuple[ContextEfficiencyVariant, ...] = (
    "raw",
    "compact-inline",
    "query-persistent",
    "range-one-shot",
    "current",
)

_TURN_PATTERN = re.compile(r"\[CONTEXT_EFFICIENCY_TURN=(\d+)]")
_FACT_PATTERN = re.compile(r"\[CTX_FACT (?P<id>F\d{4})=(?P<value>[^\]\r\n]+)]")
_SOURCE_PATTERN = re.compile(r"\[m:(\d+)(?:-(\d+))?]")
_REFERENCE_PATTERN = re.compile(r"context_ref=(blob:[0-9a-f]{64})")
_NEXT_OFFSET_PATTERN = re.compile(r'"next_offset"\s*:\s*(\d+)')
_COMPACTION_NAMES = {
    "context_compaction_input": "compaction",
    "context_compaction_repair": "repair",
    "context_compaction_condense": "condense",
}


@dataclass(frozen=True)
class ContextEfficiencyProfile:
    """Reproducible workload and budget controls for the composite case."""

    name: str = "fast"
    logical_turns: int = 12
    tool_output_chars: int = 32_000
    lookup_turns: tuple[int, ...] = (3, 7, 11)
    forced_compaction_turns: tuple[int, ...] = (5, 9)
    range_chunk_bytes: int = 4_096
    seed: int = 19
    context_window_tokens: int = 48_000
    max_input_tokens: int = 24_000
    recent_conversation_tokens: int = 4_000
    tool_result_inline_tokens: int = 800
    tool_result_head_chars: int = 2_400
    tool_result_tail_chars: int = 600
    compaction_summary_tokens: int = 2_000
    compaction_max_input_tokens: int = 40_000
    compaction_max_output_tokens: int = 2_048

    def with_overrides(self, *, seed: int | None = None) -> ContextEfficiencyProfile:
        return replace(self, seed=self.seed if seed is None else seed)


FAST_CONTEXT_EFFICIENCY_PROFILE = ContextEfficiencyProfile()


@dataclass(frozen=True)
class ContextEfficiencyRequestTrace:
    request_index: int
    phase: str
    logical_turn: int | None
    prompt_tokens: int
    output_tokens: int
    usage_reported: bool
    message_count: int
    tool_schema_count: int
    requested_tool_names: tuple[str, ...]
    active_summary_count: int
    visible_fact_count: int
    reference_delivery_tokens: int
    replayed_reference_tokens: int
    model_latency_seconds: float
    request_sha256: str


@dataclass(frozen=True)
class ContextEfficiencyResult:
    profile: ContextEfficiencyProfile
    variant: ContextEfficiencyVariant
    summary: dict[str, Any]
    traces: tuple[ContextEfficiencyRequestTrace, ...]
    turns: tuple[dict[str, Any], ...]

    def write_artifacts(self, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "summary.json").write_text(
            json.dumps(self.summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with (output_dir / "requests.jsonl").open("w", encoding="utf-8") as stream:
            for trace in self.traces:
                stream.write(json.dumps(asdict(trace), ensure_ascii=False, sort_keys=True) + "\n")
        _write_csv(output_dir / "turns.csv", self.turns)


class ContextEfficiencyFixtureTool(Tool):
    name = "context_efficiency_fixture"
    description = "返回上下文节省机制评测所需的确定性大结果。"
    input_schema = {
        "type": "object",
        "properties": {"turn": {"type": "integer", "minimum": 1}},
        "required": ["turn"],
        "additionalProperties": False,
    }
    annotations = ToolAnnotations(read_only=True, default_timeout=5, output_limit=2_000_000)

    def __init__(self, profile: ContextEfficiencyProfile) -> None:
        self.profile = profile

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        del context
        turn = int(arguments["turn"])
        return ToolResult(
            success=True,
            output=context_efficiency_tool_output(self.profile, turn),
            metadata={"turn": turn, "seed": self.profile.seed},
        )


class ContextEvidenceVerifyTool(Tool):
    name = "context_evidence_verify"
    description = "确定性校验从大结果中找到的事实，防止把干扰项当成答案。"
    input_schema = {
        "type": "object",
        "properties": {
            "turn": {"type": "integer", "minimum": 1},
            "value": {"type": "string"},
        },
        "required": ["turn", "value"],
        "additionalProperties": False,
    }
    annotations = ToolAnnotations(read_only=True, idempotent=True, default_timeout=5)

    def __init__(self, profile: ContextEfficiencyProfile) -> None:
        self.profile = profile

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        del context
        turn = int(arguments["turn"])
        expected = context_efficiency_fact(self.profile, turn)
        value = str(arguments["value"])
        if value != expected[1]:
            return ToolResult(
                success=False,
                error=f"事实校验失败: turn={turn:04d}",
                metadata={"turn": turn},
            )
        return ToolResult(
            success=True,
            output=f"verification=ok {_fact_text(*expected)}",
            metadata={"turn": turn, "fact_id": expected[0]},
        )


def context_efficiency_fact(
    profile: ContextEfficiencyProfile,
    turn: int,
) -> tuple[str, str]:
    fact_id = f"F{turn:04d}"
    value = hashlib.sha256(f"{profile.seed}:{turn}:target".encode()).hexdigest()[:20]
    return fact_id, value


def context_efficiency_tool_output(
    profile: ContextEfficiencyProfile,
    turn: int,
) -> str:
    if profile.tool_output_chars < profile.range_chunk_bytes * 5:
        raise ValueError("tool_output_chars 必须至少容纳五个分页分块")
    head_value = hashlib.sha256(f"{profile.seed}:{turn}:head".encode()).hexdigest()[:12]
    tail_value = hashlib.sha256(f"{profile.seed}:{turn}:tail".encode()).hexdigest()[:12]
    key = f"needle-turn-{turn:04d}"
    fact_id, fact_value = context_efficiency_fact(profile, turn)
    fact = (
        _fact_text(fact_id, fact_value)
        if turn in profile.lookup_turns
        else f"[CTX_UNUSED U{turn:04d}={fact_value}]"
    )
    header = (
        f"context-efficiency turn={turn:04d} seed={profile.seed} status=completed\n"
        f"[CTX_HEAD H{turn:04d}={head_value}]\n"
    )
    target = f"\n[TARGET key={key}] {fact}\n"
    tail = f"\n[CTX_TAIL T{turn:04d}={tail_value}] eof=true\n"
    target_offset = max(profile.range_chunk_bytes * 4 + 257, profile.tool_output_chars // 2)
    before = _diagnostic_fill(
        profile,
        turn,
        target_offset - len(header),
        start=0,
    )
    remaining = profile.tool_output_chars - len(header) - len(before) - len(target) - len(tail)
    after = _diagnostic_fill(profile, turn, max(0, remaining), start=10_000)
    output = header + before + target + after + tail
    if len(output) < profile.tool_output_chars:
        output = output[: -len(tail)] + "x" * (profile.tool_output_chars - len(output)) + tail
    return output[: profile.tool_output_chars]


def _diagnostic_fill(
    profile: ContextEfficiencyProfile,
    turn: int,
    length: int,
    *,
    start: int,
) -> str:
    chunks: list[str] = []
    size = 0
    sample = start
    while size < length:
        digest = hashlib.sha256(f"{profile.seed}:{turn}:{sample}".encode()).hexdigest()[:24]
        line = (
            f"diagnostic sample={sample:05d} checksum={digest} "
            f"decoy=unrelated-key-{(turn + sample) % 97:04d}\n"
        )
        chunks.append(line)
        size += len(line)
        sample += 1
    return "".join(chunks)[:length]


class ScriptedContextEfficiencyProvider(ModelProvider):
    """Deterministically drives the production Agent loop for the offline gate."""

    def __init__(
        self,
        *,
        profile: ContextEfficiencyProfile,
        variant: ContextEfficiencyVariant,
    ) -> None:
        self.profile = profile
        self.variant = variant
        self.estimator = TokenEstimator()

    def capabilities(self, model: str) -> ModelCapabilities:
        del model
        return ModelCapabilities(usage_reporting=True)

    def count_tokens(self, request: ModelRequest) -> int | None:
        return self.estimator.request(request.messages, request.tools)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        last_name = request.messages[-1].name
        if last_name in _COMPACTION_NAMES:
            response = ChatMessage(
                role=Role.ASSISTANT,
                content=self._compaction_response(request),
            )
        else:
            response = self._agent_response(request)
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
            provider_metadata={
                "raw_usage": {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
                "simulated": True,
            },
        )
        yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")

    def _agent_response(self, request: ModelRequest) -> ChatMessage:
        turn = _logical_turn(request.messages)
        fixture_call_id = f"eff-fixture-{turn:04d}"
        if not _has_tool_result(request.messages, fixture_call_id):
            return _tool_response(
                ToolCall(
                    id=fixture_call_id,
                    name=ContextEfficiencyFixtureTool.name,
                    arguments={"turn": turn},
                )
            )

        if turn not in self.profile.lookup_turns:
            facts = _facts_in_messages(request.messages)
            expected = _expected_facts(self.profile)
            visible = [
                _fact_text(fact_id, facts[fact_id])
                for fact_id in sorted(expected)
                if facts.get(fact_id) == expected[fact_id]
            ]
            suffix = " " + " ".join(visible) if turn == self.profile.logical_turns else ""
            return ChatMessage(
                role=Role.ASSISTANT,
                content=f"[CONTEXT_EFFICIENCY_DONE={turn:04d}]{suffix}",
            )

        fact_id, expected_value = context_efficiency_fact(self.profile, turn)
        visible_facts = _facts_in_messages(request.messages)
        verify_call_id = f"eff-verify-{turn:04d}"
        if visible_facts.get(fact_id) == expected_value:
            if not _has_tool_result(request.messages, verify_call_id):
                return _tool_response(
                    ToolCall(
                        id=verify_call_id,
                        name=ContextEvidenceVerifyTool.name,
                        arguments={"turn": turn, "value": expected_value},
                    )
                )
            return ChatMessage(
                role=Role.ASSISTANT,
                content=(
                    f"[CONTEXT_EFFICIENCY_DONE={turn:04d}] {_fact_text(fact_id, expected_value)}"
                ),
            )

        reference = _current_fixture_reference(request.messages, fixture_call_id)
        if reference is None:
            return ChatMessage(
                role=Role.ASSISTANT,
                content=f"[CONTEXT_EFFICIENCY_MISSING_REF={turn:04d}]",
            )
        if self.variant == "range-one-shot":
            offset = _next_range_offset(request.messages, reference)
            call = ToolCall(
                id=f"eff-ref-{turn:04d}-{offset:06d}",
                name="load_context_reference",
                arguments={
                    "reference": reference,
                    "offset": offset,
                    "limit": self.profile.range_chunk_bytes,
                },
            )
        else:
            call = ToolCall(
                id=f"eff-ref-{turn:04d}",
                name="load_context_reference",
                arguments={
                    "reference": reference,
                    "query": f"needle-turn-{turn:04d}",
                    "max_matches": 1,
                    "context_chars": 1_000,
                },
            )
        return _tool_response(call)

    @staticmethod
    def _compaction_response(request: ModelRequest) -> str:
        payload = json.loads(request.messages[-1].content or "{}")
        if request.messages[-1].name in {
            "context_compaction_repair",
            "context_compaction_condense",
        }:
            allowed = payload.get("allowed_reference_range") or payload["covered_range"]
            start, end = (int(item) for item in allowed)
            candidate = str(payload.get("candidate_summary") or "")
            facts = _facts_with_positions(candidate, default_position=start)
        else:
            start, end = (int(item) for item in payload["covered_range"])
            facts = _facts_with_positions(str(payload.get("previous_summary") or ""))
            for item in payload.get("raw_messages") or payload.get("new_messages") or []:
                position = int(item["position"])
                for match in _FACT_PATTERN.finditer(str(item.get("content") or "")):
                    facts[match.group("id")] = (match.group(0), position)
        return _render_summary(start=start, end=end, facts=facts)


class RecordingContextEfficiencyProvider(ModelProvider):
    """Records comparable request metrics around either scripted or live providers."""

    def __init__(self, provider: ModelProvider, *, measure_latency: bool) -> None:
        self.provider = provider
        self.measure_latency = measure_latency
        self.estimator = TokenEstimator()
        self.traces: list[ContextEfficiencyRequestTrace] = []
        self.logical_turn_hint: int | None = None
        self._seen_deliveries: set[str] = set()

    def capabilities(self, model: str) -> ModelCapabilities:
        return self.provider.capabilities(model)

    def count_tokens(self, request: ModelRequest) -> int | None:
        counted = self.provider.count_tokens(request)
        if counted is not None:
            return counted
        return self.estimator.request(request.messages, request.tools)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        started = time.perf_counter()
        reported_input: int | None = None
        reported_output: int | None = None
        requested_tools: list[str] = []
        try:
            async for event in self.provider.stream(request):
                if event.kind == ModelEventKind.USAGE:
                    if event.input_tokens is not None:
                        reported_input = event.input_tokens
                    if event.output_tokens is not None:
                        reported_output = event.output_tokens
                elif event.kind == ModelEventKind.TOOL_CALL_DELTA and event.tool_name:
                    requested_tools.append(event.tool_name)
                yield event
        finally:
            phase = _request_phase(request)
            logical_turn = (
                self.logical_turn_hint if phase != "agent" else _logical_turn(request.messages)
            )
            delivery_tokens, replayed_tokens = self._reference_delivery_cost(request.messages)
            prompt_tokens = (
                reported_input
                if reported_input is not None
                else self.estimator.request(request.messages, request.tools)
            )
            output_tokens = reported_output or 0
            digest_payload = json.dumps(
                {
                    "messages": [message.to_openai() for message in request.messages],
                    "tools": [tool.to_openai() for tool in request.tools],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            self.traces.append(
                ContextEfficiencyRequestTrace(
                    request_index=len(self.traces) + 1,
                    phase=phase,
                    logical_turn=logical_turn,
                    prompt_tokens=prompt_tokens,
                    output_tokens=output_tokens,
                    usage_reported=reported_input is not None and reported_output is not None,
                    message_count=len(request.messages),
                    tool_schema_count=len(request.tools),
                    requested_tool_names=tuple(dict.fromkeys(requested_tools)),
                    active_summary_count=sum(
                        message.name == "context_compaction" for message in request.messages
                    ),
                    visible_fact_count=len(_facts_in_messages(request.messages)),
                    reference_delivery_tokens=delivery_tokens,
                    replayed_reference_tokens=replayed_tokens,
                    model_latency_seconds=(
                        time.perf_counter() - started if self.measure_latency else 0.0
                    ),
                    request_sha256=hashlib.sha256(digest_payload.encode()).hexdigest(),
                )
            )

    def _reference_delivery_cost(self, messages: Iterable[ChatMessage]) -> tuple[int, int]:
        delivered = 0
        replayed = 0
        for message in messages:
            if message.role != Role.TOOL or message.name != "load_context_reference":
                continue
            if _is_delivery_receipt(message.content or ""):
                continue
            tokens = self.estimator.message(message)
            delivered += tokens
            call_id = message.tool_call_id or ""
            if call_id in self._seen_deliveries:
                replayed += tokens
            else:
                self._seen_deliveries.add(call_id)
        return delivered, replayed


class _PersistentDeliveryAgentRunner(AgentRunner):
    """Eval-only counterfactual that keeps rehydrated payloads in the current run."""

    @staticmethod
    def _expire_disposable_tool_results(
        conversation: list[PositionedMessage],
        replacements: dict[int, ChatMessage],
        *,
        positions: set[int],
    ) -> list[PositionedMessage]:
        del replacements, positions
        return conversation


async def run_context_efficiency_benchmark(
    *,
    profile: ContextEfficiencyProfile,
    workspace: Path,
    variant: ContextEfficiencyVariant = "current",
    provider: ModelProvider | None = None,
    base_config: AppConfig | None = None,
    require_reported_usage: bool = False,
    write_artifacts: bool = True,
) -> ContextEfficiencyResult:
    if variant not in CONTEXT_EFFICIENCY_VARIANTS:
        raise ValueError(f"未知 context-efficiency variant: {variant}")
    if provider is not None and base_config is None:
        raise ValueError("真实 Provider 评测必须提供 base_config")

    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "skills").mkdir(exist_ok=True)
    config = _benchmark_config(
        profile,
        variant=variant,
        base_config=base_config,
    )
    underlying = provider or ScriptedContextEfficiencyProvider(
        profile=profile,
        variant=variant,
    )
    recording_provider = RecordingContextEfficiencyProvider(
        underlying,
        measure_latency=provider is not None,
    )
    store = SQLiteSessionStore(workspace / "state.db")
    events = MemoryEventSink()
    event_bus = EventBus([store, events])
    session_id = store.create_session(workspace)
    catalog = SkillCatalog(workspace / "skills")
    catalog.scan()
    execution_target = LocalExecutionTarget()
    compactor = None
    if variant != "raw":
        compactor = ContextCompactor(
            config=config,
            provider=recording_provider,
            store=store,
            event_bus=event_bus,
        )
    tools = ToolRegistry()
    tools.register(ContextEfficiencyFixtureTool(profile))
    tools.register(ContextEvidenceVerifyTool(profile))
    runner_type = _PersistentDeliveryAgentRunner if variant == "query-persistent" else AgentRunner
    runner = runner_type(
        config=config,
        workspace=workspace,
        provider=recording_provider,
        tool_registry=tools,
        policy=DefaultPolicyEngine(config.permissions, workspace),
        execution_target=execution_target,
        skills=SkillManager(catalog),
        context=ContextAssembler(workspace=workspace, skill_catalog=catalog),
        store=store,
        event_bus=event_bus,
        context_compactor=compactor,
    )
    expected = _expected_facts(profile)
    workload_hasher = hashlib.sha256()
    results = []
    turns: list[dict[str, Any]] = []
    immutable_checks = 0
    cumulative_input = 0
    total_cost = 0.0

    try:
        for turn in range(1, profile.logical_turns + 1):
            prompt = _workload_prompt(profile, variant, turn)
            workload_hasher.update(_workload_identity_prompt(profile, turn).encode())
            workload_hasher.update(b"\0")
            workload_hasher.update(context_efficiency_tool_output(profile, turn).encode())
            workload_hasher.update(b"\n")
            prefix_position = store.latest_message_position(session_id)
            prefix_digest = _digest_through(store, session_id, prefix_position)
            compactions_before = store.count_context_compactions(
                session_id,
                statuses={"ready", "superseded"},
            )
            trace_start = len(recording_provider.traces)
            event_start = len(events.events)
            if variant != "raw" and turn in profile.forced_compaction_turns:
                runner.request_compaction(session_id)
            recording_provider.logical_turn_hint = turn
            result = await runner.run(RunRequest(prompt=prompt, session_id=session_id))
            results.append(result)
            total_cost += float(result.cost_usd or 0)
            compactions_after = store.count_context_compactions(
                session_id,
                statuses={"ready", "superseded"},
            )
            compaction_delta = compactions_after - compactions_before
            if (
                compaction_delta > 0
                and _digest_through(store, session_id, prefix_position) == prefix_digest
            ):
                immutable_checks += compaction_delta

            turn_traces = recording_provider.traces[trace_start:]
            turn_events = events.events[event_start:]
            input_tokens = sum(item.prompt_tokens for item in turn_traces)
            cumulative_input += input_tokens
            tool_names = [
                str(event.payload.get("name") or "")
                for event in turn_events
                if event.type == EventType.TOOL_REQUESTED
            ]
            visible = _facts_in_text(result.final_text)
            turns.append(
                {
                    "logical_turn": turn,
                    "status": result.status,
                    "agent_requests": sum(item.phase == "agent" for item in turn_traces),
                    "compaction_requests": sum(item.phase != "agent" for item in turn_traces),
                    "input_tokens": input_tokens,
                    "cumulative_input_tokens": cumulative_input,
                    "output_tokens": sum(item.output_tokens for item in turn_traces),
                    "source_tool_calls": tool_names.count(ContextEfficiencyFixtureTool.name),
                    "reference_tool_calls": tool_names.count("load_context_reference"),
                    "verify_tool_calls": tool_names.count(ContextEvidenceVerifyTool.name),
                    "compactions": compaction_delta,
                    "visible_expected_facts": sum(
                        visible.get(fact_id) == value for fact_id, value in expected.items()
                    ),
                }
            )
    except BaseException:
        store.close()
        raise
    finally:
        await execution_target.aclose()

    traces = tuple(recording_provider.traces)
    completed_events = [
        event for event in events.events if event.type == EventType.CONTEXT_COMPACTION_COMPLETED
    ]
    compaction_failures = sum(
        event.type == EventType.CONTEXT_COMPACTION_FAILED for event in events.events
    )
    protocol_repairs = sum(
        event.type == EventType.CONTEXT_TOOL_PROTOCOL_REPAIRED for event in events.events
    )
    context_limits = sum(event.type == EventType.CONTEXT_LIMIT_REACHED for event in events.events)
    tool_names = [
        str(event.payload.get("name") or "")
        for event in events.events
        if event.type == EventType.TOOL_REQUESTED
    ]
    persisted_reference_messages = [
        entry.message
        for entry in store.load_positioned_messages(session_id)
        if entry.message.role == Role.TOOL and entry.message.name == "load_context_reference"
    ]
    source_messages = [
        entry.message
        for entry in store.load_positioned_messages(session_id)
        if entry.message.role == Role.TOOL
        and entry.message.name == ContextEfficiencyFixtureTool.name
    ]
    final_outputs = [result.final_text for result in results]
    final_facts = _facts_in_text(final_outputs[-1] if final_outputs else "")
    checkpoint_facts_ok = all(
        _facts_in_text(results[turn - 1].final_text).get(fact_id) == value
        for turn, (fact_id, value) in (
            (turn, context_efficiency_fact(profile, turn)) for turn in profile.lookup_turns
        )
    )
    observed_facts = _facts_in_text("\n".join(final_outputs))
    unsupported = {
        fact_id: value
        for fact_id, value in observed_facts.items()
        if expected.get(fact_id) != value
    }
    non_lookup_reference_calls = sum(
        int(row["reference_tool_calls"])
        for row in turns
        if int(row["logical_turn"]) not in profile.lookup_turns
    )
    quality_gates = {
        "all_runs_completed": all(result.status == "completed" for result in results),
        "lookup_checkpoints_exact": checkpoint_facts_ok,
        "final_report_exact": all(final_facts.get(key) == value for key, value in expected.items()),
        "no_unsupported_facts": not unsupported,
        "source_tool_once_per_turn": tool_names.count(ContextEfficiencyFixtureTool.name)
        == profile.logical_turns,
        "verify_tool_once_per_lookup": tool_names.count(ContextEvidenceVerifyTool.name)
        == len(profile.lookup_turns),
        "references_only_for_lookup_turns": non_lookup_reference_calls == 0,
        "transcript_immutable": immutable_checks == len(completed_events),
        "single_active_summary": all(item.active_summary_count <= 1 for item in traces),
        "no_tool_protocol_repairs": protocol_repairs == 0,
        "no_context_limit": context_limits == 0,
        "no_compaction_failures": compaction_failures == 0,
        "expected_compaction_cycles": (
            len(completed_events) == 0 if variant == "raw" else len(completed_events) >= 2
        ),
        "reference_receipts_are_short": all(
            _valid_delivery_receipt(message.content or "")
            for message in persisted_reference_messages
        ),
        "reported_usage_available": (
            all(trace.usage_reported for trace in traces) if require_reported_usage else True
        ),
    }
    summary = _benchmark_summary(
        profile=profile,
        variant=variant,
        traces=traces,
        turns=turns,
        workload_sha256=workload_hasher.hexdigest(),
        tool_names=tool_names,
        compactions=len(completed_events),
        compaction_failures=compaction_failures,
        protocol_repairs=protocol_repairs,
        context_limits=context_limits,
        externalized_source_results=sum(
            "chars externalized" in (message.content or "") for message in source_messages
        ),
        persisted_reference_messages=len(persisted_reference_messages),
        unsupported=unsupported,
        quality_gates=quality_gates,
        total_cost=total_cost,
    )
    store.close()
    benchmark = ContextEfficiencyResult(
        profile=profile,
        variant=variant,
        summary=summary,
        traces=traces,
        turns=tuple(turns),
    )
    if write_artifacts:
        benchmark.write_artifacts(workspace / "artifacts")
    return benchmark


async def run_context_efficiency_suite(
    *,
    profile: ContextEfficiencyProfile,
    workspace: Path,
    variants: Iterable[ContextEfficiencyVariant] = CONTEXT_EFFICIENCY_VARIANTS,
    write_artifacts: bool = True,
) -> tuple[tuple[ContextEfficiencyResult, ...], dict[str, Any]]:
    results = []
    for variant in dict.fromkeys(variants):
        results.append(
            await run_context_efficiency_benchmark(
                profile=profile,
                workspace=workspace / variant,
                variant=variant,
                write_artifacts=write_artifacts,
            )
        )
    comparison = write_context_efficiency_comparison(results, workspace)
    return tuple(results), comparison


def write_context_efficiency_comparison(
    results: Iterable[ContextEfficiencyResult],
    output_dir: Path,
) -> dict[str, Any]:
    materialized = tuple(results)
    by_variant = {result.variant: result for result in materialized}
    rows = []
    for result in materialized:
        metrics = result.summary["metrics"]
        rows.append(
            {
                "variant": result.variant,
                "workload_sha256": result.summary["workload"]["sha256"],
                "quality_passed": result.summary["quality"]["passed"],
                "input_tokens": metrics["input_tokens"],
                "agent_input_tokens": metrics["agent_input_tokens"],
                "compaction_input_tokens": metrics["compaction_input_tokens"],
                "total_tokens": metrics["total_tokens"],
                "model_requests": metrics["model_requests"],
                "agent_requests": metrics["agent_requests"],
                "reference_tool_calls": metrics["reference_tool_calls"],
                "replayed_reference_tokens": metrics["replayed_reference_tokens"],
                "compactions": result.summary["compaction"]["completed"],
            }
        )

    raw = by_variant.get("raw")
    compact = by_variant.get("compact-inline")
    persistent = by_variant.get("query-persistent")
    ranged = by_variant.get("range-one-shot")
    current = by_variant.get("current")
    break_even = _break_even_turn(raw, current)
    gates: dict[str, bool | None] = {
        "all_quality_gates_passed": all(
            result.summary["quality"]["passed"] for result in materialized
        ),
        "identical_workload": len({result.summary["workload"]["sha256"] for result in materialized})
        <= 1,
        "compaction_saves_at_least_25pct": _metric_ratio_at_most(
            compact, raw, "input_tokens", 0.75
        ),
        "current_saves_at_least_50pct": _metric_ratio_at_most(current, raw, "input_tokens", 0.50),
        "externalization_beats_compaction_only": _metric_less(current, compact, "input_tokens"),
        "query_uses_one_reference_call_per_lookup": (
            current.summary["metrics"]["reference_tool_calls"] == len(current.profile.lookup_turns)
            if current is not None
            else None
        ),
        "query_reduces_reference_calls_by_3x": (
            ranged.summary["metrics"]["reference_tool_calls"]
            >= current.summary["metrics"]["reference_tool_calls"] * 3
            if ranged is not None and current is not None
            else None
        ),
        "query_reduces_agent_requests": _metric_less(current, ranged, "agent_requests"),
        "one_shot_has_zero_replay": (
            current.summary["metrics"]["replayed_reference_tokens"] == 0
            if current is not None
            else None
        ),
        "persistent_delivery_replays_tokens": (
            persistent.summary["metrics"]["replayed_reference_tokens"] > 0
            if persistent is not None
            else None
        ),
        "one_shot_beats_persistent_delivery": _metric_less(current, persistent, "input_tokens"),
        "break_even_by_turn_4": break_even is not None and break_even <= 4,
    }
    required = [value for value in gates.values() if value is not None]
    comparison = {
        "schema_version": 1,
        "rows": rows,
        "break_even_turn": break_even,
        "effects": {
            "compaction_input_tokens_saved": _metric_delta(raw, compact, "input_tokens"),
            "current_input_tokens_saved_vs_raw": _metric_delta(raw, current, "input_tokens"),
            "current_reference_call_delta_vs_raw": _metric_delta(
                raw, current, "reference_tool_calls", left_minus_right=False
            ),
            "query_reference_calls_saved_vs_range": _metric_delta(
                ranged, current, "reference_tool_calls"
            ),
            "query_agent_requests_saved_vs_range": _metric_delta(ranged, current, "agent_requests"),
            "one_shot_replay_tokens_saved": _metric_delta(
                persistent, current, "replayed_reference_tokens"
            ),
        },
        "acceptance": {"gates": gates, "passed": all(required)},
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "comparison.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return comparison


def _benchmark_config(
    profile: ContextEfficiencyProfile,
    *,
    variant: ContextEfficiencyVariant,
    base_config: AppConfig | None,
) -> AppConfig:
    inline_full = variant in {"raw", "compact-inline"}
    context_window = profile.context_window_tokens
    max_input = profile.max_input_tokens
    threshold = 0.80
    if variant == "raw" and base_config is None:
        context_window = 4_000_000
        max_input = 3_800_000
        threshold = 0.99
    elif base_config is not None:
        context_window = base_config.model.context_window_tokens
        max_input = min(base_config.context.max_input_tokens, context_window)
        threshold = 0.99 if variant == "raw" else 0.80

    payload = base_config.model_dump(mode="python") if base_config is not None else {}
    payload.setdefault("model", {})
    payload["model"].update(
        {
            "base_url": payload["model"].get("base_url") or "https://offline.invalid",
            "name": payload["model"].get("name") or "context-efficiency-model",
            "context_window_tokens": context_window,
            "max_output_tokens": 2_048,
        }
    )
    payload.setdefault("agent", {})
    payload["agent"].update(
        {
            "max_steps": 16,
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
            "max_input_tokens": max_input,
            "auto_compact_threshold": threshold,
            "output_reserve_tokens": 2_048,
            "protocol_reserve_tokens": 512,
            "safety_margin_tokens": 512,
            "recent_conversation_tokens": profile.recent_conversation_tokens,
            "compaction_min_recent_user_turns": 1,
            "tool_result_inline_tokens": (
                1_000_000 if inline_full else profile.tool_result_inline_tokens
            ),
            "tool_result_head_chars": (
                profile.tool_output_chars + 1 if inline_full else profile.tool_result_head_chars
            ),
            "tool_result_tail_chars": 0 if inline_full else profile.tool_result_tail_chars,
            "compaction_summary_target_tokens": 1_200,
            "compaction_summary_tokens": profile.compaction_summary_tokens,
            "compaction_max_output_tokens": profile.compaction_max_output_tokens,
            "compaction_max_input_tokens": profile.compaction_max_input_tokens,
            "compaction_input_target_ratio": 0.90,
            "compaction_source_refs": "range",
            "compaction_thinking": "disabled",
            "compaction_failure_backoff_seconds": 0,
            "compaction_transport_retry_backoff_seconds": 0,
            "compaction_rebuild_every": 2,
        }
    )
    return AppConfig.model_validate(payload)


def _workload_prompt(
    profile: ContextEfficiencyProfile,
    variant: ContextEfficiencyVariant,
    turn: int,
) -> str:
    instructions = [
        f"[CONTEXT_EFFICIENCY_TURN={turn:04d}]",
        f"调用 {ContextEfficiencyFixtureTool.name}，参数 turn={turn}，每轮只能调用一次。",
    ]
    if turn in profile.lookup_turns:
        fact_id, _ = context_efficiency_fact(profile, turn)
        instructions.append(
            f"找到 key=needle-turn-{turn:04d} 对应的 {fact_id} 值，"
            f"再调用 {ContextEvidenceVerifyTool.name} 校验。"
        )
        if variant == "range-one-shot":
            instructions.append(
                f"若正文被外置，只能从 offset=0 开始按 {profile.range_chunk_bytes} bytes 分页读取。"
            )
        elif variant in {"query-persistent", "current"}:
            instructions.append("若正文被外置，使用 load_context_reference 的 query 模式一次定位。")
        else:
            instructions.append("本变体提供完整 Tool Result，不得调用 load_context_reference。")
        instructions.append(f"最终回答必须原样包含 [CTX_FACT {fact_id}=<校验后的值>]。")
    else:
        instructions.append("本轮不需要正文中的任何目标事实，不得调用 load_context_reference。")
    if turn == profile.logical_turns:
        fact_ids = ", ".join(sorted(_expected_facts(profile)))
        instructions.append(f"最终回答还必须原样列出已验证事实：{fact_ids}。")
    return "\n".join(instructions)


def _workload_identity_prompt(profile: ContextEfficiencyProfile, turn: int) -> str:
    """Hash the task while excluding the retrieval strategy being compared."""
    payload: dict[str, Any] = {
        "turn": turn,
        "source_tool": ContextEfficiencyFixtureTool.name,
        "requires_lookup": turn in profile.lookup_turns,
        "requires_cumulative_report": turn == profile.logical_turns,
    }
    if turn in profile.lookup_turns:
        fact_id, _ = context_efficiency_fact(profile, turn)
        payload.update({"fact_id": fact_id, "key": f"needle-turn-{turn:04d}"})
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _benchmark_summary(
    *,
    profile: ContextEfficiencyProfile,
    variant: ContextEfficiencyVariant,
    traces: tuple[ContextEfficiencyRequestTrace, ...],
    turns: list[dict[str, Any]],
    workload_sha256: str,
    tool_names: list[str],
    compactions: int,
    compaction_failures: int,
    protocol_repairs: int,
    context_limits: int,
    externalized_source_results: int,
    persisted_reference_messages: int,
    unsupported: dict[str, str],
    quality_gates: dict[str, bool],
    total_cost: float,
) -> dict[str, Any]:
    agent = [trace for trace in traces if trace.phase == "agent"]
    compaction = [trace for trace in traces if trace.phase != "agent"]
    prompt_tokens = sum(trace.prompt_tokens for trace in traces)
    output_tokens = sum(trace.output_tokens for trace in traces)
    model_latencies = [trace.model_latency_seconds for trace in traces]
    return {
        "schema_version": 1,
        "suite": profile.name,
        "variant": variant,
        "profile": asdict(profile),
        "workload": {
            "sha256": workload_sha256,
            "logical_turns": profile.logical_turns,
            "tool_output_chars_per_turn": profile.tool_output_chars,
            "lookup_turns": list(profile.lookup_turns),
            "expected_fact_count": len(profile.lookup_turns),
        },
        "metrics": {
            "input_tokens": prompt_tokens,
            "agent_input_tokens": sum(trace.prompt_tokens for trace in agent),
            "compaction_input_tokens": sum(trace.prompt_tokens for trace in compaction),
            "output_tokens": output_tokens,
            "total_tokens": prompt_tokens + output_tokens,
            "model_requests": len(traces),
            "agent_requests": len(agent),
            "compaction_requests": len(compaction),
            "peak_request_tokens": max((trace.prompt_tokens for trace in traces), default=0),
            "source_tool_calls": tool_names.count(ContextEfficiencyFixtureTool.name),
            "reference_tool_calls": tool_names.count("load_context_reference"),
            "verify_tool_calls": tool_names.count(ContextEvidenceVerifyTool.name),
            "reference_delivery_tokens": sum(trace.reference_delivery_tokens for trace in traces),
            "replayed_reference_tokens": sum(trace.replayed_reference_tokens for trace in traces),
            "externalized_source_results": externalized_source_results,
            "persisted_reference_receipts": persisted_reference_messages,
            "cost_usd": total_cost,
            "model_latency_seconds": sum(model_latencies),
            "model_request_latency_p50_seconds": (
                statistics.median(model_latencies) if model_latencies else 0.0
            ),
            "model_request_latency_p95_seconds": _percentile(model_latencies, 0.95),
            "model_request_latency_max_seconds": max(model_latencies, default=0.0),
        },
        "compaction": {
            "completed": compactions,
            "failed": compaction_failures,
        },
        "quality": {
            "unsupported_facts": unsupported,
            "tool_protocol_repairs": protocol_repairs,
            "context_limit_events": context_limits,
            "gates": quality_gates,
            "passed": all(quality_gates.values()),
        },
        "turns": turns,
    }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return ordered[index]


def _request_phase(request: ModelRequest) -> str:
    return _COMPACTION_NAMES.get(request.messages[-1].name or "", "agent")


def _logical_turn(messages: Iterable[ChatMessage]) -> int:
    for message in reversed(list(messages)):
        if message.role != Role.USER:
            continue
        match = _TURN_PATTERN.search(message.content or "")
        if match:
            return int(match.group(1))
    raise ValueError("Agent 请求缺少 CONTEXT_EFFICIENCY_TURN marker")


def _tool_response(call: ToolCall) -> ChatMessage:
    return ChatMessage(role=Role.ASSISTANT, tool_calls=[call])


def _has_tool_result(messages: Iterable[ChatMessage], call_id: str) -> bool:
    return any(
        message.role == Role.TOOL and message.tool_call_id == call_id for message in messages
    )


def _current_fixture_reference(
    messages: Iterable[ChatMessage],
    call_id: str,
) -> str | None:
    for message in reversed(list(messages)):
        if message.role != Role.TOOL or message.tool_call_id != call_id:
            continue
        match = _REFERENCE_PATTERN.search(message.content or "")
        return match.group(1) if match else None
    return None


def _next_range_offset(messages: Iterable[ChatMessage], reference: str) -> int:
    offsets = [0]
    for message in messages:
        if message.role != Role.TOOL or message.name != "load_context_reference":
            continue
        content = message.content or ""
        if reference not in content:
            continue
        offsets.extend(int(match.group(1)) for match in _NEXT_OFFSET_PATTERN.finditer(content))
    return max(offsets)


def _expected_facts(profile: ContextEfficiencyProfile) -> dict[str, str]:
    return dict(context_efficiency_fact(profile, turn) for turn in profile.lookup_turns)


def _fact_text(fact_id: str, value: str) -> str:
    return f"[CTX_FACT {fact_id}={value}]"


def _facts_in_messages(messages: Iterable[ChatMessage]) -> dict[str, str]:
    return _facts_in_text("\n".join(message.content or "" for message in messages))


def _facts_in_text(text: str) -> dict[str, str]:
    return {match.group("id"): match.group("value") for match in _FACT_PATTERN.finditer(text)}


def _facts_with_positions(
    text: str,
    *,
    default_position: int | None = None,
) -> dict[str, tuple[str, int]]:
    facts: dict[str, tuple[str, int]] = {}
    for line in text.splitlines():
        source = _SOURCE_PATTERN.search(line)
        position = int(source.group(1)) if source is not None else default_position
        if position is None:
            continue
        for match in _FACT_PATTERN.finditer(line):
            facts[match.group("id")] = (match.group(0), position)
    return facts


def _render_summary(
    *,
    start: int,
    end: int,
    facts: dict[str, tuple[str, int]],
) -> str:
    reference = f"[m:{start}]" if start == end else f"[m:{start}-{end}]"
    critical = (
        "\n".join(f"- {fact} [m:{position}]" for _, (fact, position) in sorted(facts.items()))
        or f"- 当前覆盖范围没有已验证的 CTX_FACT。 {reference}"
    )
    return "\n\n".join(
        [
            f"# Goal\n- 完成上下文节省机制综合评测。 {reference}",
            f"# Constraints\n- 只保留经过校验的 CTX_FACT。 {reference}",
            f"# Progress\n- 已处理覆盖范围内的工作负载。 {reference}",
            f"# Key Decisions\n- 使用固定大结果和确定性评分。 {reference}",
            f"# Relevant Files\n- 本评测不修改任务文件。 {reference}",
            f"# Failures\n- 当前覆盖范围没有确认失败。 {reference}",
            f"# Next Steps\n- 继续下一逻辑轮。 {reference}",
            f"# Critical Context\n{critical}",
        ]
    )


def _is_delivery_receipt(content: str) -> bool:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return False
    return payload.get("status") == "disposable_context_delivery"


def _valid_delivery_receipt(content: str) -> bool:
    if not _is_delivery_receipt(content) or "完整内容已持久化" in content:
        return False
    return content.count("blob:") == 1


def _digest_through(
    store: SQLiteSessionStore,
    session_id: str,
    through_position: int,
) -> str:
    digest = hashlib.sha256()
    for entry in store.load_positioned_messages(
        session_id,
        through_position=through_position,
    ):
        digest.update(str(entry.position).encode())
        digest.update(b"\0")
        digest.update(entry.message.model_dump_json().encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _metric_ratio_at_most(
    left: ContextEfficiencyResult | None,
    right: ContextEfficiencyResult | None,
    metric: str,
    ratio: float,
) -> bool | None:
    if left is None or right is None:
        return None
    return left.summary["metrics"][metric] <= right.summary["metrics"][metric] * ratio


def _metric_less(
    left: ContextEfficiencyResult | None,
    right: ContextEfficiencyResult | None,
    metric: str,
) -> bool | None:
    if left is None or right is None:
        return None
    return left.summary["metrics"][metric] < right.summary["metrics"][metric]


def _metric_delta(
    left: ContextEfficiencyResult | None,
    right: ContextEfficiencyResult | None,
    metric: str,
    *,
    left_minus_right: bool = True,
) -> int | float | None:
    if left is None or right is None:
        return None
    left_value = left.summary["metrics"][metric]
    right_value = right.summary["metrics"][metric]
    return left_value - right_value if left_minus_right else right_value - left_value


def _break_even_turn(
    raw: ContextEfficiencyResult | None,
    current: ContextEfficiencyResult | None,
) -> int | None:
    if raw is None or current is None:
        return None
    for raw_turn, current_turn in zip(raw.turns, current.turns, strict=True):
        if current_turn["cumulative_input_tokens"] < raw_turn["cumulative_input_tokens"]:
            return int(current_turn["logical_turn"])
    return None


def _write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    materialized = list(rows)
    with path.open("w", encoding="utf-8", newline="") as stream:
        if not materialized:
            return
        writer = csv.DictWriter(stream, fieldnames=list(materialized[0]))
        writer.writeheader()
        writer.writerows(materialized)
