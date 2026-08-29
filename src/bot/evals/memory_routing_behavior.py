from __future__ import annotations

import csv
import hashlib
import json
import re
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
from bot.memory import ExtractedMemoryCandidate, MarkdownMemoryStore, MemoryKind
from bot.policy import DefaultPolicyEngine
from bot.providers import ModelProvider
from bot.sessions import SQLiteSessionStore
from bot.skills import SkillCatalog, SkillManager
from bot.tools import ToolRegistry

MemoryRoutingProviderKind = Literal["scripted", "live"]
MemoryRoutingVariant = Literal["eager", "on_demand"]

MEMORY_ROUTING_VARIANTS: tuple[MemoryRoutingVariant, ...] = ("eager", "on_demand")

_CASE_PATTERN = re.compile(r"\[MEMORY_ROUTING_CASE=([a-z_]+)]")
_IRRELEVANT_PATTERN = re.compile(r"\[ROUTER_DONE=irrelevant:12]")
_FACT_PATTERN = re.compile(r"\[ROUTER_FACT\s+(?P<case>[a-z_]+)=(?P<value>RCH-[0-9a-f]{16})]")
_ATTRIBUTION_PATTERN = re.compile(r"\[ROUTER_ATTRIBUTION=(denied|affirmed|unknown)]")


@dataclass(frozen=True)
class MemoryRoutingScenario:
    name: str
    expected_decision: str
    expected_search_calls: int
    expected_evidence_calls: int
    kind: Literal["irrelevant", "fact", "attribution"]


MEMORY_ROUTING_SCENARIOS: tuple[MemoryRoutingScenario, ...] = (
    MemoryRoutingScenario("irrelevant", "none", 0, 0, "irrelevant"),
    MemoryRoutingScenario("implicit_relevant", "suggest_search", 1, 0, "fact"),
    MemoryRoutingScenario("explicit_history", "require_search", 1, 0, "fact"),
    MemoryRoutingScenario("attribution_conflict", "require_evidence", 1, 1, "attribution"),
)


def memory_routing_scenario(name: str) -> MemoryRoutingScenario:
    try:
        return next(item for item in MEMORY_ROUTING_SCENARIOS if item.name == name)
    except StopIteration as exc:
        raise ValueError(f"未知 memory routing scenario: {name}") from exc


def memory_routing_fact(name: str) -> str:
    digest = hashlib.sha256(f"memory-routing:{name}".encode()).hexdigest()[:16]
    return f"RCH-{digest}"


@dataclass(frozen=True)
class MemoryRoutingRequestTrace:
    request_index: int
    prompt_tokens: int
    output_tokens: int
    usage_reported: bool
    elapsed_seconds: float
    message_count: int
    tool_choice: str | dict[str, Any] | None
    requested_tool_names: tuple[str, ...]
    message_names: tuple[str, ...]
    automatic_memory_chars: int
    memory_search_delivery_chars: int
    memory_evidence_delivery_chars: int


@dataclass(frozen=True)
class MemoryRoutingBehaviorResult:
    scenario: MemoryRoutingScenario
    variant: MemoryRoutingVariant
    provider_kind: MemoryRoutingProviderKind
    workspace: Path
    summary: dict[str, Any]
    traces: tuple[MemoryRoutingRequestTrace, ...]

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


class ScriptedMemoryRoutingProvider(ModelProvider):
    """Deterministic policy that validates the benchmark plumbing before live use."""

    def __init__(self, scenario: MemoryRoutingScenario) -> None:
        self.scenario = scenario
        self.estimator = TokenEstimator()

    def capabilities(self, model: str) -> ModelCapabilities:
        del model
        return ModelCapabilities(usage_reporting=True)

    def count_tokens(self, request: ModelRequest) -> int | None:
        return self.estimator.request(request.messages, request.tools)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        response = self._response(request)
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
        yield ModelEvent(
            kind=ModelEventKind.USAGE,
            input_tokens=self.estimator.request(request.messages, request.tools),
            output_tokens=self.estimator.message(response),
            provider_metadata={"simulated": True},
        )
        yield ModelEvent(
            kind=ModelEventKind.FINISH,
            finish_reason="tool_calls" if response.tool_calls else "stop",
        )

    def _response(self, request: ModelRequest) -> ChatMessage:
        messages = request.messages
        if self.scenario.kind == "irrelevant":
            return ChatMessage(role=Role.ASSISTANT, content="[ROUTER_DONE=irrelevant:12]")

        search = _latest_tool_message(messages, "search_memory")
        evidence = _latest_tool_message(messages, "load_memory_evidence")
        automatic = next(
            (message for message in messages if message.name == "automatic_memory"),
            None,
        )
        if search is None and (self.scenario.kind == "attribution" or automatic is None):
            return _tool_response(
                ToolCall(
                    id=f"memory-search-{self.scenario.name}",
                    name="search_memory",
                    arguments={"query": _memory_query(self.scenario)},
                )
            )

        if self.scenario.kind == "attribution":
            if evidence is None:
                identifier = _first_memory_identifier(search)
                if identifier is None:
                    return ChatMessage(
                        role=Role.ASSISTANT,
                        content="[ROUTER_ATTRIBUTION=unknown]",
                    )
                return _tool_response(
                    ToolCall(
                        id="memory-evidence-attribution",
                        name="load_memory_evidence",
                        arguments={"memory": identifier},
                    )
                )
            return ChatMessage(
                role=Role.ASSISTANT,
                content=(
                    "[ROUTER_ATTRIBUTION=denied]"
                    if '"role": "user"' in (evidence.content or "")
                    and "没有贴出 MEMORY_ROUTER_AUDIT.md" in (evidence.content or "")
                    else "[ROUTER_ATTRIBUTION=unknown]"
                ),
            )

        visible = "\n".join(message.content or "" for message in messages)
        match = next(
            (
                item
                for item in _FACT_PATTERN.finditer(visible)
                if item.group("case") == self.scenario.name
            ),
            None,
        )
        if match is None:
            return ChatMessage(role=Role.ASSISTANT, content="[ROUTER_FACT_MISSING]")
        return ChatMessage(role=Role.ASSISTANT, content=match.group(0))


class RecordingMemoryRoutingProvider(ModelProvider):
    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider
        self.estimator = TokenEstimator()
        self.traces: list[MemoryRoutingRequestTrace] = []

    def capabilities(self, model: str) -> ModelCapabilities:
        return self.provider.capabilities(model)

    def count_tokens(self, request: ModelRequest) -> int | None:
        return self.provider.count_tokens(request) or self.estimator.request(
            request.messages,
            request.tools,
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
                MemoryRoutingRequestTrace(
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
                    tool_choice=request.tool_choice,
                    requested_tool_names=tuple(dict.fromkeys(requested_tools)),
                    message_names=tuple(
                        message.name for message in request.messages if message.name is not None
                    ),
                    automatic_memory_chars=sum(
                        len(message.content or "")
                        for message in request.messages
                        if message.name == "automatic_memory"
                    ),
                    memory_search_delivery_chars=_delivery_chars(
                        request.messages,
                        "search_memory",
                        "historical_memory_reference",
                    ),
                    memory_evidence_delivery_chars=_delivery_chars(
                        request.messages,
                        "load_memory_evidence",
                        "original_transcript_evidence",
                    ),
                )
            )


async def run_memory_routing_behavior_case(
    *,
    scenario: MemoryRoutingScenario,
    variant: MemoryRoutingVariant,
    workspace: Path,
    provider_kind: MemoryRoutingProviderKind = "scripted",
    provider: ModelProvider | None = None,
    base_config: AppConfig | None = None,
    require_reported_usage: bool = False,
) -> MemoryRoutingBehaviorResult:
    if (provider is None) != (base_config is None):
        raise ValueError("provider 和 base_config 必须同时提供或同时省略")
    if provider_kind == "live" and provider is None:
        raise ValueError("live case 必须提供真实 Provider")
    if variant not in MEMORY_ROUTING_VARIANTS:
        raise ValueError(f"未知 memory routing variant: {variant}")

    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    if (workspace / "state.db").exists():
        raise ValueError(f"评测 workspace 已包含 state.db，请使用新目录: {workspace}")
    skills_dir = workspace / "skills"
    skills_dir.mkdir(exist_ok=True)
    config = _memory_routing_config(base_config, variant)
    recording = RecordingMemoryRoutingProvider(provider or ScriptedMemoryRoutingProvider(scenario))
    store = SQLiteSessionStore(workspace / "state.db")
    memory_store = MarkdownMemoryStore(workspace / "memory")
    _seed_memory_fixture(store, memory_store, workspace, scenario)
    events = MemoryEventSink()
    event_bus = EventBus([store, events])
    session_id = store.create_session(workspace)
    catalog = SkillCatalog(skills_dir)
    catalog.scan()
    execution_target = LocalExecutionTarget()
    runner = AgentRunner(
        config=config,
        workspace=workspace,
        provider=recording,
        tool_registry=ToolRegistry(),
        policy=DefaultPolicyEngine(config.permissions, workspace),
        execution_target=execution_target,
        skills=SkillManager(catalog),
        context=ContextAssembler(workspace=workspace, skill_catalog=catalog),
        store=store,
        event_bus=event_bus,
        memory_store=memory_store,
    )

    started = time.perf_counter()
    try:
        result = await runner.run(
            RunRequest(prompt=_scenario_prompt(scenario), session_id=session_id)
        )
        elapsed = time.perf_counter() - started
        requested = [event for event in events.events if event.type == EventType.TOOL_REQUESTED]
        search_events = _tool_events(requested, "search_memory")
        evidence_events = _tool_events(requested, "load_memory_evidence")
        routing_events = [
            event for event in events.events if event.type == EventType.MEMORY_ROUTING_DECIDED
        ]
        decisions = tuple(str(event.payload.get("decision") or "") for event in routing_events)
        automatic_requests = sum(trace.automatic_memory_chars > 0 for trace in recording.traces)
        named_tool_choice_supported = recording.capabilities(config.model.name).named_tool_choice
        named_choices = tuple(
            _named_tool_choice(trace.tool_choice)
            for trace in recording.traces
            if _named_tool_choice(trace.tool_choice) is not None
        )
        final_correct = _final_answer_correct(scenario, result.final_text)
        exact_output_format = _final_answer_exact_format(scenario, result.final_text)
        gates = {
            "run_completed": result.status == "completed",
            "final_answer_correct": final_correct,
            "reported_usage_available": (
                all(trace.usage_reported for trace in recording.traces)
                if require_reported_usage
                else True
            ),
            "automatic_memory_shape": (
                automatic_requests == 0 if variant == "on_demand" else automatic_requests > 0
            ),
        }
        diagnostics = {
            "exact_output_format": exact_output_format,
            "minimal_memory_tool_path": (
                len(search_events) == scenario.expected_search_calls
                and len(evidence_events) == scenario.expected_evidence_calls
            ),
        }
        if variant == "on_demand":
            gates["router_decision_exact"] = decisions == (scenario.expected_decision,)
            gates["search_requirement_satisfied"] = (
                len(search_events) == 0
                if scenario.expected_search_calls == 0
                else len(search_events) >= scenario.expected_search_calls
            )
            gates["evidence_requirement_satisfied"] = (
                len(evidence_events) == 0
                if scenario.kind == "irrelevant"
                else len(evidence_events) >= scenario.expected_evidence_calls
            )
            required_choices: tuple[str, ...] = ()
            if scenario.expected_decision == "require_search":
                required_choices = ("search_memory",)
            elif scenario.expected_decision == "require_evidence":
                required_choices = ("search_memory", "load_memory_evidence")
            gates["required_tool_choice_compatible"] = (
                named_choices == required_choices
                if required_choices and named_tool_choice_supported
                else not named_choices
            )
        else:
            gates["router_disabled"] = not decisions and not named_choices

        summary: dict[str, Any] = {
            "schema_version": 1,
            "benchmark": "memory-routing-behavior",
            "provider": provider_kind,
            "variant": variant,
            "scenario": asdict(scenario),
            "metrics": {
                "model_requests": len(recording.traces),
                "input_tokens": sum(trace.prompt_tokens for trace in recording.traces),
                "output_tokens": sum(trace.output_tokens for trace in recording.traces),
                "search_tool_calls": len(search_events),
                "evidence_tool_calls": len(evidence_events),
                "automatic_memory_requests": automatic_requests,
                "automatic_memory_chars_replayed": sum(
                    trace.automatic_memory_chars for trace in recording.traces
                ),
                "memory_search_delivery_chars": sum(
                    trace.memory_search_delivery_chars for trace in recording.traces
                ),
                "memory_evidence_delivery_chars": sum(
                    trace.memory_evidence_delivery_chars for trace in recording.traces
                ),
                "model_latency_seconds": sum(trace.elapsed_seconds for trace in recording.traces),
                "end_to_end_seconds": elapsed,
                "cost_usd": float(result.cost_usd or 0),
            },
            "observed": {
                "router_decisions": list(decisions),
                "named_tool_choices": list(named_choices),
                "named_tool_choice_supported": named_tool_choice_supported,
                "requested_tool_names": [
                    str(event.payload.get("name") or "") for event in requested
                ],
                "final_text": result.final_text,
            },
            "quality": {
                "passed": all(gates.values()),
                "gates": gates,
                "diagnostics": diagnostics,
            },
        }
    finally:
        await execution_target.aclose()
        store.close()

    benchmark = MemoryRoutingBehaviorResult(
        scenario=scenario,
        variant=variant,
        provider_kind=provider_kind,
        workspace=workspace,
        summary=summary,
        traces=tuple(recording.traces),
    )
    benchmark.write_artifacts()
    return benchmark


async def run_memory_routing_behavior_suite(
    *,
    workspace: Path,
    provider_kind: MemoryRoutingProviderKind = "scripted",
    provider: ModelProvider | None = None,
    base_config: AppConfig | None = None,
    require_reported_usage: bool = False,
    variants: Iterable[MemoryRoutingVariant] = MEMORY_ROUTING_VARIANTS,
    scenarios: Iterable[MemoryRoutingScenario] = MEMORY_ROUTING_SCENARIOS,
) -> tuple[tuple[MemoryRoutingBehaviorResult, ...], dict[str, Any]]:
    results: list[MemoryRoutingBehaviorResult] = []
    selected_variants = tuple(variants)
    selected_scenarios = tuple(scenarios)
    for variant in selected_variants:
        for scenario in selected_scenarios:
            results.append(
                await run_memory_routing_behavior_case(
                    scenario=scenario,
                    variant=variant,
                    workspace=workspace / variant / scenario.name,
                    provider_kind=provider_kind,
                    provider=provider,
                    base_config=base_config,
                    require_reported_usage=require_reported_usage,
                )
            )
    summary = summarize_memory_routing_results(results, provider_kind=provider_kind)
    write_memory_routing_comparison(results, summary, workspace)
    return tuple(results), summary


def summarize_memory_routing_results(
    results: Iterable[MemoryRoutingBehaviorResult],
    *,
    provider_kind: MemoryRoutingProviderKind,
) -> dict[str, Any]:
    materialized = tuple(results)
    by_key = {(result.variant, result.scenario.name): result for result in materialized}
    deltas: dict[str, dict[str, float]] = {}
    for scenario in MEMORY_ROUTING_SCENARIOS:
        eager = by_key.get(("eager", scenario.name))
        routed = by_key.get(("on_demand", scenario.name))
        if eager is None or routed is None:
            continue
        deltas[scenario.name] = {
            key: float(routed.summary["metrics"][key]) - float(eager.summary["metrics"][key])
            for key in (
                "model_requests",
                "input_tokens",
                "cost_usd",
                "model_latency_seconds",
                "end_to_end_seconds",
            )
        }
    router_results = [result for result in materialized if result.variant == "on_demand"]
    eager_results = [result for result in materialized if result.variant == "eager"]
    hypotheses = {
        "irrelevant_avoids_memory_replay": _case_gate(
            by_key,
            "on_demand",
            "irrelevant",
            "automatic_memory_shape",
        )
        and _metric(by_key, "on_demand", "irrelevant", "input_tokens")
        < _metric(by_key, "eager", "irrelevant", "input_tokens"),
        "implicit_relevant_recalled": _case_gate(
            by_key,
            "on_demand",
            "implicit_relevant",
            "final_answer_correct",
        ),
        "explicit_history_forces_search": _case_gate(
            by_key,
            "on_demand",
            "explicit_history",
            "required_tool_choice_compatible",
        )
        and _case_gate(
            by_key,
            "on_demand",
            "explicit_history",
            "final_answer_correct",
        ),
        "attribution_uses_original_evidence": _case_gate(
            by_key,
            "on_demand",
            "attribution_conflict",
            "required_tool_choice_compatible",
        )
        and _case_gate(
            by_key,
            "on_demand",
            "attribution_conflict",
            "final_answer_correct",
        ),
    }
    return {
        "schema_version": 1,
        "benchmark": "memory-routing-behavior-suite",
        "provider": provider_kind,
        "case_count": len(materialized),
        "quality": {
            "router_passed": bool(router_results)
            and all(result.summary["quality"]["passed"] for result in router_results),
            "eager_pass_rate": (
                sum(result.summary["quality"]["passed"] for result in eager_results)
                / len(eager_results)
                if eager_results
                else 0
            ),
        },
        "hypotheses": hypotheses,
        "paired_deltas_on_demand_minus_eager": deltas,
        "cases": {
            f"{result.variant}/{result.scenario.name}": {
                "passed": result.summary["quality"]["passed"],
                **result.summary["metrics"],
            }
            for result in materialized
        },
        "acceptance": {
            "paired_matrix_complete": len(materialized)
            == len(MEMORY_ROUTING_VARIANTS) * len(MEMORY_ROUTING_SCENARIOS),
            "router_quality_passed": bool(router_results)
            and all(result.summary["quality"]["passed"] for result in router_results),
            "hypotheses_passed": all(hypotheses.values()),
        },
    }


def write_memory_routing_comparison(
    results: Iterable[MemoryRoutingBehaviorResult],
    summary: dict[str, Any],
    workspace: Path,
) -> None:
    materialized = tuple(results)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    rows = [
        {
            "variant": result.variant,
            "scenario": result.scenario.name,
            "passed": result.summary["quality"]["passed"],
            **result.summary["metrics"],
        }
        for result in materialized
    ]
    with (workspace / "cases.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _memory_routing_config(
    base_config: AppConfig | None,
    variant: MemoryRoutingVariant,
) -> AppConfig:
    payload = base_config.model_dump(mode="python") if base_config is not None else {}
    payload.setdefault("model", {})
    payload["model"].update(
        {
            "base_url": payload["model"].get("base_url") or "https://offline.invalid",
            "name": payload["model"].get("name") or "memory-routing-model",
            "context_window_tokens": payload["model"].get("context_window_tokens") or 32_000,
            "max_output_tokens": 2_048,
            "temperature": 0,
        }
    )
    payload.setdefault("agent", {})
    payload["agent"].update(
        {
            "max_steps": 8,
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
                int(payload["context"].get("max_input_tokens", 16_000)),
                int(payload["model"]["context_window_tokens"]),
            ),
            "auto_compact_threshold": 0.99,
            "output_reserve_tokens": 2_048,
            "protocol_reserve_tokens": 512,
            "safety_margin_tokens": 512,
        }
    )
    payload.setdefault("memory", {})
    payload["memory"].update(
        {
            "enabled": True,
            "auto_extract": False,
            "context_mode": variant,
            "router_enabled": True,
            "router_enforce_required": True,
            "index_tokens": 2_000,
            "search_limit": 4,
        }
    )
    return AppConfig.model_validate(payload)


def _seed_memory_fixture(
    store: SQLiteSessionStore,
    memory_store: MarkdownMemoryStore,
    workspace: Path,
    scenario: MemoryRoutingScenario,
) -> None:
    source_session = store.create_session(
        workspace,
        session_id=f"memory-source-{scenario.name}",
    )
    run_id = f"memory-source-run-{scenario.name}"
    store.start_run(source_session, run_id)
    if scenario.kind == "attribution":
        user_content = "我没有贴出 MEMORY_ROUTER_AUDIT.md 全文。"
        assistant_content = "用户贴出了 MEMORY_ROUTER_AUDIT.md 全文。"
        memory_content = assistant_content
    else:
        value = memory_routing_fact(scenario.name)
        user_content = f"项目约定的发布通道记录为 {value}。"
        assistant_content = f"已验证发布通道。 [ROUTER_FACT {scenario.name}={value}]"
        memory_content = (
            f"项目约定的发布通道是 {value}。回答时保留标记 [ROUTER_FACT {scenario.name}={value}]。"
        )
    user_position = store.append_message(
        source_session,
        run_id,
        ChatMessage(role=Role.USER, content=user_content),
    )
    assistant_position = store.append_message(
        source_session,
        run_id,
        ChatMessage(role=Role.ASSISTANT, content=assistant_content),
    )
    store.finish_run(run_id, "completed")
    padding = " reference-padding" * 48
    memory_store.consolidate(
        [
            ExtractedMemoryCandidate(
                kind=MemoryKind.PROCEDURE,
                scope="workspace",
                memory_key=f"router.{scenario.name}",
                content=memory_content + padding,
                confidence=0.97,
                evidence_positions=[user_position, assistant_position],
            )
        ],
        session_id=source_session,
        run_id=run_id,
    )


def _scenario_prompt(scenario: MemoryRoutingScenario) -> str:
    header = f"[MEMORY_ROUTING_CASE={scenario.name}]"
    if scenario.kind == "irrelevant":
        return (
            f"{header}\n这是一个与历史无关的独立算术题：7 + 5 等于多少？"
            "只返回 [ROUTER_DONE=irrelevant:12]。不要调用记忆工具。"
        )
    if scenario.name == "implicit_relevant":
        return (
            f"{header}\n请给出项目约定的发布通道。不要凭常识猜测；若当前上下文没有答案，"
            "使用可用的记忆工具。只返回 [ROUTER_FACT implicit_relevant=<value>]，"
            "把 <value> 替换成查到的精确值。"
        )
    if scenario.name == "explicit_history":
        return (
            f"{header}\n按上次约定的发布通道继续。只返回 "
            "[ROUTER_FACT explicit_history=<value>]，把 <value> 替换成查到的精确值。"
        )
    return (
        f"{header}\n我之前是否说过自己贴出了 MEMORY_ROUTER_AUDIT.md 全文？"
        "只根据原始 Transcript 中 role=user 的消息判断，不能把自动记忆或 assistant 消息"
        "当作用户原话。只返回 [ROUTER_ATTRIBUTION=denied]、"
        "[ROUTER_ATTRIBUTION=affirmed] 或 [ROUTER_ATTRIBUTION=unknown]。"
    )


def _memory_query(scenario: MemoryRoutingScenario) -> str:
    if scenario.kind == "attribution":
        return "MEMORY_ROUTER_AUDIT.md 全文"
    return "项目约定的发布通道"


def _final_answer_correct(scenario: MemoryRoutingScenario, text: str) -> bool:
    if scenario.kind == "irrelevant":
        return len(_IRRELEVANT_PATTERN.findall(text)) == 1
    if scenario.kind == "attribution":
        match = _ATTRIBUTION_PATTERN.search(text)
        return match is not None and match.group(1) == "denied"
    matches = [
        match for match in _FACT_PATTERN.finditer(text) if match.group("case") == scenario.name
    ]
    return len(matches) == 1 and matches[0].group("value") == memory_routing_fact(scenario.name)


def _final_answer_exact_format(scenario: MemoryRoutingScenario, text: str) -> bool:
    stripped = text.strip()
    if scenario.kind == "irrelevant":
        return stripped == "[ROUTER_DONE=irrelevant:12]"
    if scenario.kind == "attribution":
        return stripped == "[ROUTER_ATTRIBUTION=denied]"
    return stripped == (f"[ROUTER_FACT {scenario.name}={memory_routing_fact(scenario.name)}]")


def _tool_response(*calls: ToolCall) -> ChatMessage:
    return ChatMessage(role=Role.ASSISTANT, tool_calls=list(calls))


def _latest_tool_message(messages: Iterable[ChatMessage], name: str) -> ChatMessage | None:
    return next(
        (
            message
            for message in reversed(list(messages))
            if message.role == Role.TOOL and message.name == name
        ),
        None,
    )


def _first_memory_identifier(message: ChatMessage | None) -> str | None:
    if message is None:
        return None
    try:
        payload = json.loads(message.content or "{}")
    except json.JSONDecodeError:
        return None
    matches = payload.get("matches")
    if not isinstance(matches, list) or not matches or not isinstance(matches[0], dict):
        return None
    identifier = matches[0].get("key") or matches[0].get("id")
    return str(identifier) if identifier else None


def _delivery_chars(messages: Iterable[ChatMessage], name: str, marker: str) -> int:
    return sum(
        len(message.content or "")
        for message in messages
        if message.role == Role.TOOL and message.name == name and marker in (message.content or "")
    )


def _named_tool_choice(choice: str | dict[str, Any] | None) -> str | None:
    if not isinstance(choice, dict):
        return None
    function = choice.get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    return str(name) if name else None


def _tool_events(events: Iterable[AgentEvent], name: str) -> list[AgentEvent]:
    return [event for event in events if str(event.payload.get("name") or "") == name]


def _case_gate(
    by_key: dict[tuple[MemoryRoutingVariant, str], MemoryRoutingBehaviorResult],
    variant: MemoryRoutingVariant,
    scenario: str,
    gate: str,
) -> bool:
    result = by_key.get((variant, scenario))
    return bool(result and result.summary["quality"]["gates"].get(gate))


def _metric(
    by_key: dict[tuple[MemoryRoutingVariant, str], MemoryRoutingBehaviorResult],
    variant: MemoryRoutingVariant,
    scenario: str,
    metric: str,
) -> float:
    result = by_key.get((variant, scenario))
    return float(result.summary["metrics"][metric]) if result is not None else float("inf")
