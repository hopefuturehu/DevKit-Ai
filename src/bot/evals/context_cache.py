from __future__ import annotations

import csv
import hashlib
import json
import re
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

from bot.compaction import ContextCompactor
from bot.config.models import AppConfig
from bot.core import AgentRunner, RunRequest
from bot.core.context import ContextAssembler, TokenEstimator
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

BenchmarkVariant = Literal["current", "no-compaction"]

_TURN_PATTERN = re.compile(r"\[CACHE_BENCHMARK_TURN=(\d+)]")
_FACT_PATTERN = re.compile(r"\[CACHE_FACT (?P<id>F\d{4})=(?P<value>[^\]\r\n]+)]")
_SOURCE_PATTERN = re.compile(r"\[m:(\d+)(?:-(\d+))?]")


@dataclass(frozen=True)
class ContextCacheBenchmarkProfile:
    """All workload and context variables needed to reproduce one benchmark."""

    name: str
    context_window_tokens: int
    max_input_tokens: int
    auto_compact_threshold: float
    output_reserve_tokens: int
    protocol_reserve_tokens: int
    safety_margin_tokens: int
    recent_conversation_tokens: int
    compaction_summary_tokens: int
    compaction_max_output_tokens: int
    compaction_max_input_tokens: int
    compaction_input_target_ratio: float
    compaction_rebuild_every: int
    logical_turns: int
    tool_output_chars: int
    stable_memory_chars: int
    fact_every: int
    minimum_cacheable_tokens: int
    expected_min_compactions: int
    expected_min_rebuilds: int
    seed: int = 7

    def with_overrides(
        self,
        *,
        logical_turns: int | None = None,
        tool_output_chars: int | None = None,
        stable_memory_chars: int | None = None,
        minimum_cacheable_tokens: int | None = None,
        seed: int | None = None,
    ) -> ContextCacheBenchmarkProfile:
        return replace(
            self,
            logical_turns=logical_turns or self.logical_turns,
            tool_output_chars=tool_output_chars or self.tool_output_chars,
            stable_memory_chars=(
                self.stable_memory_chars if stable_memory_chars is None else stable_memory_chars
            ),
            minimum_cacheable_tokens=(
                self.minimum_cacheable_tokens
                if minimum_cacheable_tokens is None
                else minimum_cacheable_tokens
            ),
            seed=self.seed if seed is None else seed,
            # An override is exploratory, so the stock profile's cycle-count gate
            # must not claim that a shorter workload is invalid.
            expected_min_compactions=(
                self.expected_min_compactions if logical_turns is None else 0
            ),
            expected_min_rebuilds=self.expected_min_rebuilds if logical_turns is None else 0,
        )


FAST_CONTEXT_CACHE_PROFILE = ContextCacheBenchmarkProfile(
    name="fast",
    # The effective input budget is 24K. A larger model window leaves enough room
    # for the fast profile to exercise the guarded raw-rebuild path as well.
    context_window_tokens=48_000,
    max_input_tokens=24_000,
    auto_compact_threshold=0.65,
    output_reserve_tokens=2_048,
    protocol_reserve_tokens=512,
    safety_margin_tokens=512,
    recent_conversation_tokens=4_000,
    compaction_summary_tokens=2_000,
    compaction_max_output_tokens=2_048,
    compaction_max_input_tokens=40_000,
    compaction_input_target_ratio=0.90,
    compaction_rebuild_every=2,
    logical_turns=40,
    tool_output_chars=5_500,
    stable_memory_chars=6_000,
    fact_every=5,
    minimum_cacheable_tokens=64,
    expected_min_compactions=3,
    expected_min_rebuilds=1,
)

SOAK_CONTEXT_CACHE_PROFILE = ContextCacheBenchmarkProfile(
    name="soak",
    context_window_tokens=131_072,
    max_input_tokens=120_000,
    auto_compact_threshold=0.80,
    output_reserve_tokens=4_096,
    protocol_reserve_tokens=2_048,
    safety_margin_tokens=2_048,
    recent_conversation_tokens=48_000,
    compaction_summary_tokens=8_000,
    compaction_max_output_tokens=8_192,
    compaction_max_input_tokens=60_000,
    compaction_input_target_ratio=0.80,
    compaction_rebuild_every=5,
    logical_turns=168,
    tool_output_chars=14_000,
    stable_memory_chars=16_000,
    fact_every=12,
    minimum_cacheable_tokens=1_024,
    expected_min_compactions=6,
    expected_min_rebuilds=1,
)


def context_cache_profile(name: str) -> ContextCacheBenchmarkProfile:
    profiles = {
        FAST_CONTEXT_CACHE_PROFILE.name: FAST_CONTEXT_CACHE_PROFILE,
        SOAK_CONTEXT_CACHE_PROFILE.name: SOAK_CONTEXT_CACHE_PROFILE,
    }
    try:
        return profiles[name]
    except KeyError as exc:
        raise ValueError(f"未知 context-cache benchmark suite: {name}") from exc


@dataclass(frozen=True)
class PrefixCacheObservation:
    prompt_tokens: int
    cache_read_tokens: int
    cache_miss_tokens: int
    ideal_reusable_tokens: int
    avoidable_miss_tokens: int
    first_changed_segment: str
    matched_atoms: int
    request_sha256: str

    @property
    def cache_hit_ratio(self) -> float:
        return self.cache_read_tokens / max(1, self.prompt_tokens)


@dataclass(frozen=True)
class _PromptAtom:
    segment: str
    sha256: str
    tokens: int


class PrefixCacheSimulator:
    """Deterministic common-prefix cache model operating on request atoms.

    It deliberately does not pretend to reproduce a vendor tokenizer. Tokens
    use the same estimator as the runtime, while reuse requires an exact match
    of complete tool-schema/message atoms. Both prompt boundaries and the
    prompt-plus-generated-assistant boundary become eligible cache prefixes.
    """

    def __init__(self, *, minimum_cacheable_tokens: int = 0) -> None:
        if minimum_cacheable_tokens < 0:
            raise ValueError("minimum_cacheable_tokens 不能小于 0")
        self.minimum_cacheable_tokens = minimum_cacheable_tokens
        self._estimator = TokenEstimator()
        self._history: dict[str, list[tuple[_PromptAtom, ...]]] = {}
        self._history_keys: dict[str, set[tuple[str, ...]]] = {}

    def observe(self, request: ModelRequest) -> PrefixCacheObservation:
        atoms = self._request_atoms(request)
        prompt_tokens = sum(atom.tokens for atom in atoms)
        best_tokens = 0
        best_atoms = 0
        for historical in self._history.get(request.model, []):
            matched_atoms = 0
            matched_tokens = 0
            for current, previous in zip(atoms, historical, strict=False):
                if current.sha256 != previous.sha256:
                    break
                matched_atoms += 1
                matched_tokens += current.tokens
            if (matched_tokens, matched_atoms) > (best_tokens, best_atoms):
                best_tokens = matched_tokens
                best_atoms = matched_atoms

        if not self._history.get(request.model):
            first_changed = "cold_start"
        elif best_atoms >= len(atoms):
            first_changed = "append_only"
        else:
            first_changed = atoms[best_atoms].segment
        cache_read = best_tokens if best_tokens >= self.minimum_cacheable_tokens else 0
        request_sha = hashlib.sha256("\n".join(atom.sha256 for atom in atoms).encode()).hexdigest()
        return PrefixCacheObservation(
            prompt_tokens=prompt_tokens,
            cache_read_tokens=cache_read,
            cache_miss_tokens=max(0, prompt_tokens - cache_read),
            ideal_reusable_tokens=best_tokens,
            avoidable_miss_tokens=max(0, best_tokens - cache_read),
            first_changed_segment=first_changed,
            matched_atoms=best_atoms,
            request_sha256=request_sha,
        )

    def commit(self, request: ModelRequest, response: ChatMessage | None = None) -> None:
        prompt = self._request_atoms(request)
        self._remember(request.model, prompt)
        if response is not None:
            self._remember(request.model, (*prompt, self._message_atom(response, len(prompt))))

    def _remember(self, model: str, atoms: tuple[_PromptAtom, ...]) -> None:
        key = tuple(atom.sha256 for atom in atoms)
        known = self._history_keys.setdefault(model, set())
        if key in known:
            return
        known.add(key)
        self._history.setdefault(model, []).append(atoms)

    def _request_atoms(self, request: ModelRequest) -> tuple[_PromptAtom, ...]:
        atoms: list[_PromptAtom] = []
        if request.tools:
            payload = json.dumps(
                [tool.to_openai() for tool in request.tools],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            atoms.append(
                _PromptAtom(
                    segment="tool_schema",
                    sha256=hashlib.sha256(payload.encode()).hexdigest(),
                    tokens=sum(self._estimator.tool(tool) for tool in request.tools),
                )
            )
        atoms.extend(
            self._message_atom(message, index) for index, message in enumerate(request.messages)
        )
        return tuple(atoms)

    def _message_atom(self, message: ChatMessage, index: int) -> _PromptAtom:
        payload = json.dumps(
            message.to_openai(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if message.name in {"context_compaction", "context_compaction_input"}:
            segment = message.name
        elif message.name == "context_compaction_repair":
            segment = "context_compaction_repair"
        elif message.role == Role.SYSTEM:
            segment = "system"
        elif message.role == Role.USER:
            segment = "user_message"
        elif message.role == Role.TOOL:
            segment = "tool_result"
        else:
            segment = "assistant_message"
        return _PromptAtom(
            segment=f"{segment}:{index}",
            sha256=hashlib.sha256(payload.encode()).hexdigest(),
            tokens=self._estimator.message(message),
        )


@dataclass(frozen=True)
class RequestCacheTrace:
    request_index: int
    phase: str
    logical_turn: int | None
    agent_step: int | None
    compaction_epoch: int
    compaction_mode: str | None
    prompt_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_miss_tokens: int
    ideal_reusable_tokens: int
    avoidable_miss_tokens: int
    cache_hit_ratio: float
    first_changed_segment: str
    matched_atoms: int
    request_sha256: str
    message_count: int
    tool_count: int
    active_summary_count: int
    visible_fact_count: int


class CacheBenchmarkTool(Tool):
    name = "cache_benchmark_step"
    description = "返回确定性的离线长任务诊断数据，用于上下文缓存评测。"
    input_schema = {
        "type": "object",
        "properties": {"turn": {"type": "integer", "minimum": 1}},
        "required": ["turn"],
        "additionalProperties": False,
    }
    annotations = ToolAnnotations(read_only=True, default_timeout=5, output_limit=2_000_000)

    def __init__(self, *, output_chars: int, seed: int) -> None:
        self.output_chars = output_chars
        self.seed = seed

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        del context
        turn = int(arguments["turn"])
        return ToolResult(
            success=True,
            output=cache_benchmark_tool_output(
                turn=turn,
                output_chars=self.output_chars,
                seed=self.seed,
            ),
            metadata={"turn": turn, "seed": self.seed},
        )


def cache_benchmark_tool_output(*, turn: int, output_chars: int, seed: int) -> str:
    if output_chars < 256:
        raise ValueError("tool_output_chars 至少为 256")
    header = f"cache-benchmark turn={turn:04d} seed={seed} status=completed\n"
    lines = [header]
    size = len(header)
    sample = 0
    while size < output_chars:
        digest = hashlib.sha256(f"{seed}:{turn}:{sample}".encode()).hexdigest()[:24]
        line = (
            f"diagnostic turn={turn:04d} sample={sample:05d} "
            f"checksum={digest} invariant=preserved\n"
        )
        lines.append(line)
        size += len(line)
        sample += 1
    return "".join(lines)[:output_chars]


class DeterministicContextCacheProvider(ModelProvider):
    """Offline provider that drives the real agent and records cache telemetry."""

    def __init__(
        self,
        *,
        cache: PrefixCacheSimulator,
        compaction_epoch: Callable[[], int],
    ) -> None:
        self.cache = cache
        self.compaction_epoch = compaction_epoch
        self.estimator = TokenEstimator()
        self.traces: list[RequestCacheTrace] = []
        self.agent_requests: list[ModelRequest] = []

    def capabilities(self, model: str) -> ModelCapabilities:
        del model
        return ModelCapabilities(usage_reporting=True)

    def count_tokens(self, request: ModelRequest) -> int | None:
        return self.estimator.request(request.messages, request.tools)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        observation = self.cache.observe(request)
        phase, logical_turn, agent_step, compaction_mode = self._classify(request)
        if phase == "compaction":
            payload = json.loads(request.messages[-1].content or "{}")
            response = ChatMessage(
                role=Role.ASSISTANT,
                content=self._summary(payload),
            )
            yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text=response.content)
        elif phase == "repair":
            payload = json.loads(request.messages[-1].content or "{}")
            response = ChatMessage(
                role=Role.ASSISTANT,
                content=self._repair_summary(payload),
            )
            yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text=response.content)
        elif agent_step == 2:
            assert logical_turn is not None
            response = ChatMessage(
                role=Role.ASSISTANT,
                content=f"[CACHE_BENCHMARK_TURN={logical_turn:04d}] completed",
            )
            yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text=response.content)
        else:
            assert logical_turn is not None
            call = ToolCall(
                id=f"cache-benchmark-{logical_turn:04d}",
                name=CacheBenchmarkTool.name,
                arguments={"turn": logical_turn},
            )
            response = ChatMessage(role=Role.ASSISTANT, tool_calls=[call])
            yield ModelEvent(
                kind=ModelEventKind.TOOL_CALL_DELTA,
                tool_index=0,
                tool_call_id=call.id,
                tool_name=call.name,
                arguments_delta=json.dumps(call.arguments, separators=(",", ":")),
            )

        output_tokens = self.estimator.message(response)
        visible_facts = _facts_in_messages(request.messages)
        self.traces.append(
            RequestCacheTrace(
                request_index=len(self.traces) + 1,
                phase=phase,
                logical_turn=logical_turn,
                agent_step=agent_step,
                compaction_epoch=self.compaction_epoch(),
                compaction_mode=compaction_mode,
                prompt_tokens=observation.prompt_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=observation.cache_read_tokens,
                cache_miss_tokens=observation.cache_miss_tokens,
                ideal_reusable_tokens=observation.ideal_reusable_tokens,
                avoidable_miss_tokens=observation.avoidable_miss_tokens,
                cache_hit_ratio=observation.cache_hit_ratio,
                first_changed_segment=observation.first_changed_segment,
                matched_atoms=observation.matched_atoms,
                request_sha256=observation.request_sha256,
                message_count=len(request.messages),
                tool_count=len(request.tools),
                active_summary_count=sum(
                    message.name == "context_compaction" for message in request.messages
                ),
                visible_fact_count=len(visible_facts),
            )
        )
        if phase == "agent":
            self.agent_requests.append(request)
        self.cache.commit(request, response)
        raw_usage = {
            "prompt_tokens": observation.prompt_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": observation.prompt_tokens + output_tokens,
            "prompt_cache_hit_tokens": observation.cache_read_tokens,
            "prompt_cache_miss_tokens": observation.cache_miss_tokens,
        }
        yield ModelEvent(
            kind=ModelEventKind.USAGE,
            input_tokens=observation.prompt_tokens,
            output_tokens=output_tokens,
            provider_metadata={"raw_usage": raw_usage, "simulated": True},
        )
        yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")

    def _classify(
        self,
        request: ModelRequest,
    ) -> tuple[str, int | None, int | None, str | None]:
        last = request.messages[-1]
        if last.name == "context_compaction_input":
            payload = json.loads(last.content or "{}")
            return "compaction", None, None, str(payload.get("mode") or "unknown")
        if last.name == "context_compaction_repair":
            return "repair", None, None, None
        logical_turn = _logical_turn(request.messages)
        tool_call_id = f"cache-benchmark-{logical_turn:04d}"
        agent_step = (
            2
            if any(
                message.role == Role.TOOL and message.tool_call_id == tool_call_id
                for message in request.messages
            )
            else 1
        )
        return "agent", logical_turn, agent_step, None

    def _summary(self, payload: dict[str, Any]) -> str:
        start, end = (int(item) for item in payload["covered_range"])
        facts: dict[str, tuple[str, int]] = {}
        previous = str(payload.get("previous_summary") or "")
        for line in previous.splitlines():
            source = _SOURCE_PATTERN.search(line)
            if source is None:
                continue
            position = int(source.group(1))
            for match in _FACT_PATTERN.finditer(line):
                facts[match.group("id")] = (match.group(0), position)
        source_messages = payload.get("raw_messages") or payload.get("new_messages") or []
        for item in source_messages:
            position = int(item["position"])
            content = str(item.get("content") or "")
            for match in _FACT_PATTERN.finditer(content):
                facts[match.group("id")] = (match.group(0), position)
        return _render_summary(start=start, end=end, facts=facts)

    def _repair_summary(self, payload: dict[str, Any]) -> str:
        start, end = (int(item) for item in payload["allowed_reference_range"])
        facts: dict[str, tuple[str, int]] = {}
        candidate = str(payload.get("candidate_summary") or "")
        for line in candidate.splitlines():
            source = _SOURCE_PATTERN.search(line)
            if source is None:
                continue
            position = int(source.group(1))
            if not start <= position <= end:
                continue
            for match in _FACT_PATTERN.finditer(line):
                facts[match.group("id")] = (match.group(0), position)
        return _render_summary(start=start, end=end, facts=facts)


@dataclass(frozen=True)
class ContextCacheBenchmarkResult:
    profile: ContextCacheBenchmarkProfile
    variant: BenchmarkVariant
    summary: dict[str, Any]
    traces: tuple[RequestCacheTrace, ...]
    epochs: tuple[dict[str, Any], ...]

    def write_artifacts(self, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "summary.json").write_text(
            json.dumps(self.summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with (output_dir / "requests.jsonl").open("w", encoding="utf-8") as stream:
            for trace in self.traces:
                stream.write(json.dumps(asdict(trace), ensure_ascii=False, sort_keys=True) + "\n")
        _write_csv(output_dir / "epochs.csv", self.epochs)


async def run_context_cache_benchmark(
    *,
    profile: ContextCacheBenchmarkProfile,
    workspace: Path,
    variant: BenchmarkVariant = "current",
    cached_input_cost_ratio: float = 0.10,
    output_cost_ratio: float = 1.0,
    write_artifacts: bool = True,
) -> ContextCacheBenchmarkResult:
    """Run a deterministic long task through the production context pipeline."""

    if variant not in {"current", "no-compaction"}:
        raise ValueError(f"未知 benchmark variant: {variant}")
    if not 0 <= cached_input_cost_ratio <= 1:
        raise ValueError("cached_input_cost_ratio 必须位于 [0, 1]")
    if output_cost_ratio < 0:
        raise ValueError("output_cost_ratio 不能小于 0")
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "skills").mkdir(exist_ok=True)

    config = _benchmark_config(profile, workspace=workspace, variant=variant)
    store = SQLiteSessionStore(workspace / "state.db")
    events = MemoryEventSink()
    event_bus = EventBus([store, events])
    session_id = store.create_session(workspace)
    stable_memory = _stable_memory_content(profile)
    _seed_stable_memory(store, stable_memory)
    cache = PrefixCacheSimulator(
        minimum_cacheable_tokens=profile.minimum_cacheable_tokens,
    )
    provider = DeterministicContextCacheProvider(
        cache=cache,
        compaction_epoch=lambda: store.count_context_compactions(
            session_id,
            statuses={"ready", "superseded"},
        ),
    )
    catalog = SkillCatalog(workspace / "skills")
    catalog.scan()
    execution_target = LocalExecutionTarget()
    compactor = ContextCompactor(
        config=config,
        provider=provider,
        store=store,
        event_bus=event_bus,
    )
    tools = ToolRegistry()
    tools.register(
        CacheBenchmarkTool(
            output_chars=profile.tool_output_chars,
            seed=profile.seed,
        )
    )
    runner = AgentRunner(
        config=config,
        workspace=workspace,
        provider=provider,
        tool_registry=tools,
        policy=DefaultPolicyEngine(config.permissions, workspace),
        execution_target=execution_target,
        skills=SkillManager(catalog),
        context=ContextAssembler(workspace=workspace, skill_catalog=catalog),
        store=store,
        event_bus=event_bus,
        context_compactor=compactor,
    )
    expected_facts: dict[str, str] = {}
    fact_checks = 0
    stable_memory_checks = 0
    immutable_checks = 0
    run_statuses: list[str] = []
    workload_hasher = hashlib.sha256()
    workload_hasher.update(b"stable-memory\0")
    workload_hasher.update(stable_memory.encode())
    workload_hasher.update(b"\n")

    try:
        for turn in range(1, profile.logical_turns + 1):
            prompt, introduced = _workload_prompt(profile, turn)
            expected_facts.update(introduced)
            tool_output = cache_benchmark_tool_output(
                turn=turn,
                output_chars=profile.tool_output_chars,
                seed=profile.seed,
            )
            workload_hasher.update(prompt.encode())
            workload_hasher.update(b"\0")
            workload_hasher.update(tool_output.encode())
            workload_hasher.update(b"\n")
            prefix_position = store.latest_message_position(session_id)
            prefix_digest = _digest_through(store, session_id, prefix_position)
            compactions_before = store.count_context_compactions(
                session_id,
                statuses={"ready", "superseded"},
            )
            result = await runner.run(RunRequest(prompt=prompt, session_id=session_id))
            run_statuses.append(result.status)
            if result.status != "completed":
                raise RuntimeError(
                    f"benchmark turn {turn} 未完成: status={result.status}, error={result.error}"
                )
            compactions_after = store.count_context_compactions(
                session_id,
                statuses={"ready", "superseded"},
            )
            if compactions_after > compactions_before:
                immutable_checks += compactions_after - compactions_before
                if _digest_through(store, session_id, prefix_position) != prefix_digest:
                    raise AssertionError(f"turn {turn} 压缩改写了既有 Transcript")

            last_request = provider.agent_requests[-1]
            visible = _facts_in_messages(last_request.messages)
            fact_checks += 1
            incorrect = sorted(
                fact_id for fact_id, fact in expected_facts.items() if visible.get(fact_id) != fact
            )
            if incorrect:
                raise AssertionError(
                    f"turn {turn} 的最终模型请求丢失或改写事实: {', '.join(incorrect)}"
                )
            if stable_memory:
                memory_visible = any(
                    stable_memory in (message.content or "") for message in last_request.messages
                )
                if not memory_visible:
                    raise AssertionError(f"turn {turn} 的最终模型请求缺少稳定长期记忆")
                stable_memory_checks += 1
            summaries = sum(
                message.name == "context_compaction" for message in last_request.messages
            )
            if summaries > 1:
                raise AssertionError(f"turn {turn} 注入了 {summaries} 个活动摘要")
    except BaseException:
        store.close()
        raise
    finally:
        await execution_target.aclose()

    completed_events = [
        event for event in events.events if event.type == EventType.CONTEXT_COMPACTION_COMPLETED
    ]
    protocol_repairs = sum(
        event.type == EventType.CONTEXT_TOOL_PROTOCOL_REPAIRED for event in events.events
    )
    context_limits = sum(event.type == EventType.CONTEXT_LIMIT_REACHED for event in events.events)
    compaction_failures = sum(
        event.type == EventType.CONTEXT_COMPACTION_FAILED for event in events.events
    )
    store.close()
    rebuilds = sum(bool(event.payload.get("rebuilt_from_raw")) for event in completed_events)
    incrementals = len(completed_events) - rebuilds
    traces = tuple(provider.traces)
    epochs = tuple(_epoch_rows(traces))
    summary = _benchmark_summary(
        profile=profile,
        variant=variant,
        traces=traces,
        epochs=epochs,
        workload_sha256=workload_hasher.hexdigest(),
        expected_fact_count=len(expected_facts),
        fact_checks=fact_checks,
        stable_memory_checks=stable_memory_checks,
        immutable_checks=immutable_checks,
        run_statuses=run_statuses,
        compactions=len(completed_events),
        rebuilds=rebuilds,
        incrementals=incrementals,
        compaction_failures=compaction_failures,
        protocol_repairs=protocol_repairs,
        context_limits=context_limits,
        cached_input_cost_ratio=cached_input_cost_ratio,
        output_cost_ratio=output_cost_ratio,
    )
    benchmark = ContextCacheBenchmarkResult(
        profile=profile,
        variant=variant,
        summary=summary,
        traces=traces,
        epochs=epochs,
    )
    if write_artifacts:
        benchmark.write_artifacts(workspace / "artifacts")
    return benchmark


def write_context_cache_comparison(
    results: Iterable[ContextCacheBenchmarkResult],
    output_dir: Path,
) -> None:
    rows: list[dict[str, Any]] = []
    for result in results:
        metrics = result.summary["metrics"]
        rows.append(
            {
                "suite": result.profile.name,
                "variant": result.variant,
                "workload_sha256": result.summary["workload"]["sha256"],
                "logical_turns": result.profile.logical_turns,
                "compactions": result.summary["compaction"]["completed"],
                "weighted_cache_hit_ratio": metrics["weighted_cache_hit_ratio"],
                "agent_cache_hit_ratio": metrics["agent_cache_hit_ratio"],
                "cache_adjusted_cost_units": metrics["cache_adjusted_cost_units"],
                "no_cache_cost_units": metrics["no_cache_cost_units"],
                "perfect_prefix_cost_units": metrics["perfect_prefix_cost_units"],
                "cost_per_logical_turn": metrics["cost_per_logical_turn"],
            }
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "comparisons.csv", rows)
    (output_dir / "comparisons.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _benchmark_config(
    profile: ContextCacheBenchmarkProfile,
    *,
    workspace: Path,
    variant: BenchmarkVariant,
) -> AppConfig:
    if variant == "no-compaction":
        context_window = 4_000_000
        max_input = 3_800_000
        threshold = 0.99
    else:
        context_window = profile.context_window_tokens
        max_input = profile.max_input_tokens
        threshold = profile.auto_compact_threshold
    return AppConfig.model_validate(
        {
            "model": {
                "base_url": "https://offline.invalid",
                "name": "deterministic-context-cache-model",
                "context_window_tokens": context_window,
                "max_output_tokens": profile.output_reserve_tokens,
            },
            "agent": {
                "max_steps": 4,
                "process_wait_seconds": 0,
                "progress": {"enabled": False},
                "finalization": {"enabled": False},
            },
            "subagents": {"enabled": False},
            "permissions": {
                "mode": "read-only",
                "workspace_only": True,
                "network": "deny",
            },
            "context": {
                "max_input_tokens": max_input,
                "auto_compact_threshold": threshold,
                "output_reserve_tokens": profile.output_reserve_tokens,
                "protocol_reserve_tokens": profile.protocol_reserve_tokens,
                "safety_margin_tokens": profile.safety_margin_tokens,
                "recent_conversation_tokens": profile.recent_conversation_tokens,
                "compaction_summary_tokens": profile.compaction_summary_tokens,
                "compaction_max_output_tokens": profile.compaction_max_output_tokens,
                "compaction_max_input_tokens": profile.compaction_max_input_tokens,
                "compaction_input_target_ratio": profile.compaction_input_target_ratio,
                "compaction_repair_attempts": 1,
                "compaction_range_attempts": 2,
                "compaction_failure_backoff_seconds": 0,
                "compaction_rebuild_every": profile.compaction_rebuild_every,
            },
            "memory": {"enabled": False, "auto_extract": False},
            "skills": {"path": str(workspace / "skills"), "auto_activate": False},
            "display": {"progress": False},
            "storage": {"state_path": str(workspace / "state.db")},
        }
    )


def _workload_prompt(
    profile: ContextCacheBenchmarkProfile,
    turn: int,
) -> tuple[str, dict[str, str]]:
    introduced: dict[str, str] = {}
    lines = [
        f"[CACHE_BENCHMARK_TURN={turn:04d}]",
        "继续同一个长任务；运行确定性诊断并保留所有 CACHE_FACT。",
    ]
    if turn % profile.fact_every == 0:
        fact_id = f"F{turn // profile.fact_every:04d}"
        fact = f"[CACHE_FACT {fact_id}=seed-{profile.seed}-turn-{turn:04d}]"
        introduced[fact_id] = fact
        lines.append(fact)
    return "\n".join(lines), introduced


def _stable_memory_content(profile: ContextCacheBenchmarkProfile) -> str:
    """Build one deterministic, session-stable memory block for prefix tests."""

    if profile.stable_memory_chars <= 0:
        return ""
    marker = f"[CACHE_STABLE_MEMORY seed={profile.seed}]"
    unit = f" stable-prefix-{profile.seed:04d};"
    repeats = max(0, (profile.stable_memory_chars - len(marker) + len(unit) - 1) // len(unit))
    return (marker + unit * repeats)[: profile.stable_memory_chars]


def _seed_stable_memory(store: SQLiteSessionStore, content: str) -> None:
    """Keep repeated runs in one benchmark workspace input-identical."""

    source = "context-cache-benchmark"
    memories = store.list_memories()
    foreign = [memory for memory in memories if memory["source"] != source]
    if foreign:
        raise ValueError(
            "context-cache benchmark workspace 含有非评测长期记忆；"
            "请改用独立输出目录"
        )
    if len(memories) == 1 and memories[0]["content"] == content:
        return
    for memory in memories:
        store.delete_memory(int(memory["id"]))
    if content:
        store.add_memory(content, source=source)


def _logical_turn(messages: Iterable[ChatMessage]) -> int:
    for message in reversed(list(messages)):
        if message.role != Role.USER:
            continue
        match = _TURN_PATTERN.search(message.content or "")
        if match:
            return int(match.group(1))
    raise ValueError("Agent 请求中缺少 CACHE_BENCHMARK_TURN marker")


def _facts_in_messages(messages: Iterable[ChatMessage]) -> dict[str, str]:
    facts: dict[str, str] = {}
    for message in messages:
        for match in _FACT_PATTERN.finditer(message.content or ""):
            facts[match.group("id")] = match.group(0)
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
        or f"- 当前覆盖范围尚未引入 CACHE_FACT。 {reference}"
    )
    return "\n\n".join(
        [
            f"# Goal\n- 完成确定性长上下文缓存评测。 {reference}",
            f"# Constraints\n- 保留所有 CACHE_FACT，工作负载不得随变体变化。 {reference}",
            f"# Progress\n- 已处理覆盖范围内的诊断阶段。 {reference}",
            f"# Key Decisions\n- 使用同一个离线工具与固定消息结构。 {reference}",
            f"# Relevant Files\n- 本评测不修改任务工作区文件。 {reference}",
            f"# Failures\n- 当前覆盖范围没有模拟失败。 {reference}",
            f"# Next Steps\n- 继续下一逻辑轮并检查缓存恢复。 {reference}",
            f"# Critical Context\n{critical}",
        ]
    )


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


def _epoch_rows(traces: tuple[RequestCacheTrace, ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    epochs = sorted({trace.compaction_epoch for trace in traces if trace.phase == "agent"})
    for epoch in epochs:
        selected = [
            trace for trace in traces if trace.phase == "agent" and trace.compaction_epoch == epoch
        ]
        prompt = sum(item.prompt_tokens for item in selected)
        cache_read = sum(item.cache_read_tokens for item in selected)
        cache_miss = sum(item.cache_miss_tokens for item in selected)
        recovery = next(
            (index for index, item in enumerate(selected, start=1) if item.cache_hit_ratio >= 0.80),
            None,
        )
        rows.append(
            {
                "compaction_epoch": epoch,
                "request_count": len(selected),
                "first_logical_turn": selected[0].logical_turn,
                "last_logical_turn": selected[-1].logical_turn,
                "prompt_tokens": prompt,
                "cache_read_tokens": cache_read,
                "cache_miss_tokens": cache_miss,
                "weighted_cache_hit_ratio": cache_read / max(1, prompt),
                "first_request_cache_hit_ratio": selected[0].cache_hit_ratio,
                "requests_to_80pct_recovery": recovery,
            }
        )
    return rows


def _benchmark_summary(
    *,
    profile: ContextCacheBenchmarkProfile,
    variant: BenchmarkVariant,
    traces: tuple[RequestCacheTrace, ...],
    epochs: tuple[dict[str, Any], ...],
    workload_sha256: str,
    expected_fact_count: int,
    fact_checks: int,
    stable_memory_checks: int,
    immutable_checks: int,
    run_statuses: list[str],
    compactions: int,
    rebuilds: int,
    incrementals: int,
    compaction_failures: int,
    protocol_repairs: int,
    context_limits: int,
    cached_input_cost_ratio: float,
    output_cost_ratio: float,
) -> dict[str, Any]:
    prompt = sum(item.prompt_tokens for item in traces)
    output = sum(item.output_tokens for item in traces)
    cache_read = sum(item.cache_read_tokens for item in traces)
    cache_miss = sum(item.cache_miss_tokens for item in traces)
    ideal = sum(item.ideal_reusable_tokens for item in traces)
    avoidable = sum(item.avoidable_miss_tokens for item in traces)
    agent = [item for item in traces if item.phase == "agent"]
    agent_prompt = sum(item.prompt_tokens for item in agent)
    agent_read = sum(item.cache_read_tokens for item in agent)
    cache_adjusted = cache_miss + cache_read * cached_input_cost_ratio + output * output_cost_ratio
    no_cache = prompt + output * output_cost_ratio
    perfect_prefix = (prompt - ideal) + ideal * cached_input_cost_ratio + output * output_cost_ratio
    quality_gates = {
        "all_runs_completed": all(status == "completed" for status in run_statuses),
        "all_facts_retained": fact_checks == profile.logical_turns,
        "stable_memory_visible": (
            stable_memory_checks == profile.logical_turns
            if profile.stable_memory_chars > 0
            else stable_memory_checks == 0
        ),
        "single_active_summary": all(item.active_summary_count <= 1 for item in agent),
        "transcript_immutable": immutable_checks == compactions,
        "no_tool_protocol_repairs": protocol_repairs == 0,
        "no_context_limit": context_limits == 0,
        "no_compaction_failures": compaction_failures == 0,
        "minimum_compaction_cycles": (
            compactions >= profile.expected_min_compactions
            if variant == "current"
            else compactions == 0
        ),
        "minimum_raw_rebuilds": (
            rebuilds >= profile.expected_min_rebuilds if variant == "current" else rebuilds == 0
        ),
    }
    return {
        "schema_version": 1,
        "suite": profile.name,
        "variant": variant,
        "profile": asdict(profile),
        "workload": {
            "sha256": workload_sha256,
            "logical_turns": profile.logical_turns,
            "tool_output_chars_per_turn": profile.tool_output_chars,
            "total_tool_output_chars": profile.logical_turns * profile.tool_output_chars,
            "stable_memory_chars": profile.stable_memory_chars,
            "expected_fact_count": expected_fact_count,
        },
        "requests": {
            "total": len(traces),
            "agent": len(agent),
            "compaction": sum(item.phase == "compaction" for item in traces),
            "repair": sum(item.phase == "repair" for item in traces),
        },
        "compaction": {
            "completed": compactions,
            "rebuild_from_raw": rebuilds,
            "incremental_update": incrementals,
            "failed": compaction_failures,
            "epochs": len(epochs),
        },
        "metrics": {
            "prompt_tokens": prompt,
            "output_tokens": output,
            "cache_read_tokens": cache_read,
            "cache_miss_tokens": cache_miss,
            "ideal_reusable_tokens": ideal,
            "avoidable_miss_tokens": avoidable,
            "weighted_cache_hit_ratio": cache_read / max(1, prompt),
            "agent_cache_hit_ratio": agent_read / max(1, agent_prompt),
            "cache_adjusted_cost_units": cache_adjusted,
            "no_cache_cost_units": no_cache,
            "perfect_prefix_cost_units": perfect_prefix,
            "prefix_cache_savings_ratio": (no_cache - cache_adjusted) / max(1, no_cache),
            "perfect_prefix_regret_units": cache_adjusted - perfect_prefix,
            "cost_per_logical_turn": cache_adjusted / max(1, profile.logical_turns),
            "cached_input_cost_ratio": cached_input_cost_ratio,
            "output_cost_ratio": output_cost_ratio,
        },
        "quality": {
            "fact_visibility_checks": fact_checks,
            "stable_memory_visibility_checks": stable_memory_checks,
            "transcript_immutability_checks": immutable_checks,
            "tool_protocol_repairs": protocol_repairs,
            "context_limit_events": context_limits,
            "gates": quality_gates,
            "passed": all(quality_gates.values()),
        },
    }


def _write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    materialized = list(rows)
    with path.open("w", encoding="utf-8", newline="") as stream:
        if not materialized:
            return
        writer = csv.DictWriter(stream, fieldnames=list(materialized[0]))
        writer.writeheader()
        writer.writerows(materialized)
