from __future__ import annotations

import csv
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

from bot.compaction import ContextCompactor
from bot.config.models import AppConfig
from bot.core import AgentRunner, RunRequest
from bot.core.context import ContextAssembler
from bot.core.events import EventBus, EventType, MemoryEventSink
from bot.core.models import ToolCall
from bot.evals.context_cache import (
    FAST_CONTEXT_CACHE_PROFILE,
    SOAK_CONTEXT_CACHE_PROFILE,
    CacheBenchmarkTool,
    ContextCacheBenchmarkProfile,
    DeterministicContextCacheProvider,
    PrefixCacheSimulator,
    _benchmark_config,
    _digest_through,
    _facts_in_messages,
    _seed_stable_memory,
    _workload_prompt,
)
from bot.execution import LocalExecutionTarget
from bot.policy import DefaultPolicyEngine
from bot.sessions import SQLiteSessionStore
from bot.skills import SkillCatalog, SkillManager
from bot.tools import ToolRegistry
from bot.tools.base import Tool, ToolAnnotations, ToolContext, ToolResult

FullStackSuite = Literal["fast", "soak"]
_REFERENCE_PATTERN = re.compile(r"context_ref=(blob:[0-9a-f]{64})")
_AGENTS_MARKER = "FULL_STACK_AGENTS_MARKER"
_MEMORY_MARKER = "FULL_STACK_MEMORY_MARKER"
_SKILL_MARKER = "FULL_STACK_SKILL_MARKER"


@dataclass(frozen=True)
class ContextFullStackProfile:
    name: FullStackSuite
    logical_turns: int
    tool_output_chars: int
    fact_every: int
    forced_compaction_turns: tuple[int, ...]
    resume_after_turn: int
    fork_after_turn: int
    dummy_tool_count: int
    tool_schema_tokens: int
    seed: int

    def with_overrides(
        self,
        *,
        logical_turns: int | None = None,
        tool_output_chars: int | None = None,
        forced_compaction_turns: tuple[int, ...] | None = None,
        resume_after_turn: int | None = None,
        fork_after_turn: int | None = None,
        dummy_tool_count: int | None = None,
    ) -> ContextFullStackProfile:
        turns = self.logical_turns if logical_turns is None else logical_turns
        resume = self.resume_after_turn if resume_after_turn is None else resume_after_turn
        fork = self.fork_after_turn if fork_after_turn is None else fork_after_turn
        if logical_turns is not None:
            resume = min(resume, max(1, turns // 3))
            fork = min(fork, max(resume + 1, (turns * 2) // 3))
        return replace(
            self,
            logical_turns=turns,
            tool_output_chars=(
                self.tool_output_chars if tool_output_chars is None else tool_output_chars
            ),
            forced_compaction_turns=(
                self.forced_compaction_turns
                if forced_compaction_turns is None
                else forced_compaction_turns
            ),
            resume_after_turn=resume,
            fork_after_turn=fork,
            dummy_tool_count=(
                self.dummy_tool_count if dummy_tool_count is None else dummy_tool_count
            ),
        )


FAST_CONTEXT_FULL_STACK_PROFILE = ContextFullStackProfile(
    name="fast",
    logical_turns=24,
    tool_output_chars=9_000,
    fact_every=4,
    forced_compaction_turns=(6, 12, 18),
    resume_after_turn=8,
    fork_after_turn=16,
    dummy_tool_count=32,
    tool_schema_tokens=8_000,
    seed=43,
)

SOAK_CONTEXT_FULL_STACK_PROFILE = ContextFullStackProfile(
    name="soak",
    logical_turns=120,
    tool_output_chars=32_000,
    fact_every=10,
    forced_compaction_turns=(15, 30, 45, 60, 75, 90, 105),
    resume_after_turn=40,
    fork_after_turn=80,
    dummy_tool_count=96,
    tool_schema_tokens=12_000,
    seed=47,
)


def context_full_stack_profile(name: str) -> ContextFullStackProfile:
    profiles = {
        FAST_CONTEXT_FULL_STACK_PROFILE.name: FAST_CONTEXT_FULL_STACK_PROFILE,
        SOAK_CONTEXT_FULL_STACK_PROFILE.name: SOAK_CONTEXT_FULL_STACK_PROFILE,
    }
    try:
        return profiles[name]
    except KeyError as exc:
        raise ValueError(f"未知 context-full-stack suite: {name}") from exc


@dataclass(frozen=True)
class ContextFullStackResult:
    profile: ContextFullStackProfile
    workspace: Path
    summary: dict[str, Any]
    turns: tuple[dict[str, Any], ...]


class SchemaNoiseTool(Tool):
    annotations = ToolAnnotations(read_only=True, idempotent=True)

    def __init__(self, index: int) -> None:
        self.name = f"full_stack_schema_noise_{index:03d}"
        self.description = (
            f"Unrelated diagnostic schema {index}; it exists only to compete for context budget. "
            + "noise " * 20
        )
        self.input_schema = {
            "type": "object",
            "properties": {
                f"parameter_{field:02d}": {
                    "type": "string",
                    "description": f"Unused verbose field {field} for schema {index}. "
                    + "diagnostic " * 12,
                }
                for field in range(8)
            },
            "additionalProperties": False,
        }

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        del context, arguments
        return ToolResult(success=True, output="unused")


async def run_context_full_stack_soak(
    *,
    profile: ContextFullStackProfile,
    workspace: Path,
) -> ContextFullStackResult:
    """Run context layers and lifecycle boundaries together through AgentRunner."""

    _validate_profile(profile)
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    _prepare_workspace(workspace)
    database = workspace / "state.db"
    cache_profile = _cache_profile(profile)
    config = _benchmark_config(cache_profile, workspace=workspace, variant="current")
    config.context.tool_schema_tokens = profile.tool_schema_tokens
    config.context.tool_result_inline_tokens = 400
    config.context.tool_result_head_chars = 1_200
    config.context.tool_result_tail_chars = 300

    events = MemoryEventSink()
    cache = PrefixCacheSimulator(minimum_cacheable_tokens=64)
    state: dict[str, Any] = {}
    session_holder: dict[str, str] = {}
    provider = DeterministicContextCacheProvider(
        cache=cache,
        compaction_epoch=lambda: state["store"].count_context_compactions(
            session_holder["session_id"],
            statuses={"ready", "superseded"},
        ),
    )
    tools = ToolRegistry()
    tools.register(CacheBenchmarkTool(output_chars=profile.tool_output_chars, seed=profile.seed))
    for index in range(profile.dummy_tool_count):
        tools.register(SchemaNoiseTool(index))

    store = SQLiteSessionStore(database)
    session_id = store.create_session(workspace, session_id="full-stack-root")
    session_holder["session_id"] = session_id
    state["store"] = store
    _seed_stable_memory(store, f"[{_MEMORY_MARKER}] stable context across lifecycle boundaries")
    runner, execution_target = _build_runtime(
        config=config,
        workspace=workspace,
        store=store,
        events=events,
        provider=provider,
        tools=tools,
    )
    _activate_primary_schema(runner, session_id)

    expected_facts: dict[str, str] = {}
    run_statuses: list[str] = []
    turns: list[dict[str, Any]] = []
    layer_checks = {"agents": 0, "memory": 0, "skill": 0, "primary_schema": 0}
    immutable_checks = 0
    resume_digest_ok = False
    fork_digest_ok = False
    fork_blob_access_ok = False
    source_session_id: str | None = None
    fork_session_id: str | None = None
    runtime_rebuilds = 0

    try:
        for turn in range(1, profile.logical_turns + 1):
            prompt, introduced = _workload_prompt(cache_profile, turn)
            expected_facts.update(introduced)
            prefix_position = store.latest_message_position(session_id)
            prefix_digest = _digest_through(store, session_id, prefix_position)
            compactions_before = _completed_compactions(store, session_id)
            if turn in profile.forced_compaction_turns:
                runner.request_compaction(session_id)
            trace_start = len(provider.traces)
            request_start = len(provider.agent_requests)
            result = await runner.run(
                RunRequest(
                    prompt=prompt,
                    session_id=session_id,
                    explicit_skills=["full-stack-context"],
                )
            )
            run_statuses.append(result.status)
            compactions_after = _completed_compactions(store, session_id)
            if compactions_after > compactions_before:
                immutable_checks += compactions_after - compactions_before
                if _digest_through(store, session_id, prefix_position) != prefix_digest:
                    raise AssertionError(f"turn {turn} 压缩改写了既有 Transcript")

            request = provider.agent_requests[-1]
            visible_text = "\n".join(message.content or "" for message in request.messages)
            visible_facts = _facts_in_messages(request.messages)
            incorrect = [
                fact_id
                for fact_id, fact in expected_facts.items()
                if visible_facts.get(fact_id) != fact
            ]
            if incorrect:
                raise AssertionError(f"turn {turn} 丢失事实: {', '.join(incorrect)}")
            layer_checks["agents"] += _AGENTS_MARKER in visible_text
            layer_checks["memory"] += _MEMORY_MARKER in visible_text
            layer_checks["skill"] += _SKILL_MARKER in visible_text
            layer_checks["primary_schema"] += any(
                definition.name == CacheBenchmarkTool.name for definition in request.tools
            )
            turn_traces = provider.traces[trace_start:]
            turn_requests = provider.agent_requests[request_start:]
            turns.append(
                {
                    "logical_turn": turn,
                    "session_id": session_id,
                    "status": result.status,
                    "model_requests": len(turn_traces),
                    "input_tokens": sum(item.prompt_tokens for item in turn_traces),
                    "selected_tool_schemas": max(
                        (len(item.tools) for item in turn_requests), default=0
                    ),
                    "expected_facts": len(expected_facts),
                    "visible_facts": len(visible_facts),
                    "active_summaries": sum(
                        message.name == "context_compaction" for message in request.messages
                    ),
                    "compactions": compactions_after - compactions_before,
                }
            )

            if turn == profile.resume_after_turn:
                before_position = store.latest_message_position(session_id)
                before_digest = _digest_through(store, session_id, before_position)
                await execution_target.aclose()
                store.close()
                store = SQLiteSessionStore(database)
                state["store"] = store
                resume_digest_ok = (
                    store.session_exists(session_id)
                    and store.latest_message_position(session_id) == before_position
                    and _digest_through(store, session_id, before_position) == before_digest
                )
                runner, execution_target = _build_runtime(
                    config=config,
                    workspace=workspace,
                    store=store,
                    events=events,
                    provider=provider,
                    tools=tools,
                )
                _activate_primary_schema(runner, session_id)
                runtime_rebuilds += 1

            if turn == profile.fork_after_turn:
                source_session_id = session_id
                before_position = store.latest_message_position(session_id)
                before_digest = _digest_through(store, session_id, before_position)
                fork_session_id = store.fork_session(session_id)
                session_id = fork_session_id
                session_holder["session_id"] = session_id
                fork_digest_ok = (
                    store.latest_message_position(session_id) == before_position
                    and _digest_through(store, session_id, before_position) == before_digest
                )
                inherited_references = _message_references(store, session_id)
                fork_blob_access_ok = bool(inherited_references) and (
                    store.read_context_blob(session_id, inherited_references[-1], limit=64)
                    is not None
                )
                _activate_primary_schema(runner, session_id)
    except BaseException:
        store.close()
        raise
    finally:
        await execution_target.aclose()

    current_messages = store.load_messages(session_id)
    references = _message_references(store, session_id)
    externalized_source_results = sum(
        message.name == CacheBenchmarkTool.name
        and "chars externalized" in (message.content or "")
        for message in current_messages
    )
    child_session = store.create_session(workspace, parent_session_id=session_id)
    unrelated_session = store.create_session(workspace)
    child_blocked_before_grant = bool(references) and (
        store.read_context_blob(child_session, references[-1], limit=64) is None
    )
    child_grant_succeeded = bool(references) and store.grant_context_blob_access(
        source_session_id=session_id,
        target_session_id=child_session,
        reference=references[-1],
    )
    child_access_after_grant = bool(references) and (
        store.read_context_blob(child_session, references[-1], limit=64) is not None
    )
    unrelated_blocked = bool(references) and (
        store.read_context_blob(unrelated_session, references[-1], limit=64) is None
    )
    blob_stats = _blob_stats(database)
    compaction_events = [
        event for event in events.events if event.type == EventType.CONTEXT_COMPACTION_COMPLETED
    ]
    compaction_failures = sum(
        event.type == EventType.CONTEXT_COMPACTION_FAILED for event in events.events
    )
    protocol_repairs = sum(
        event.type == EventType.CONTEXT_TOOL_PROTOCOL_REPAIRED for event in events.events
    )
    context_limits = sum(
        event.type == EventType.CONTEXT_LIMIT_REACHED for event in events.events
    )
    store.close()

    total_registered_tools = len(tools.definitions())
    schema_shedding_observed = any(
        len(request.tools) < total_registered_tools for request in provider.agent_requests
    )
    gates = {
        "all_runs_completed": len(run_statuses) == profile.logical_turns
        and all(status == "completed" for status in run_statuses),
        "project_instructions_rehydrated": layer_checks["agents"] == profile.logical_turns,
        "memory_rehydrated": layer_checks["memory"] == profile.logical_turns,
        "skill_rehydrated": layer_checks["skill"] == profile.logical_turns,
        "primary_tool_schema_available": layer_checks["primary_schema"]
        == profile.logical_turns,
        "tool_schema_shedding_observed": schema_shedding_observed,
        "facts_survive_compaction_resume_and_fork": bool(expected_facts)
        and all(row["visible_facts"] >= row["expected_facts"] for row in turns),
        "multiple_compactions_completed": len(compaction_events) >= 2,
        "transcript_immutable_during_compaction": immutable_checks
        == len(compaction_events),
        "resume_preserves_transcript": resume_digest_ok,
        "fork_preserves_transcript": fork_digest_ok,
        "fork_inherits_blob_access": fork_blob_access_ok,
        "large_tool_results_externalized": externalized_source_results
        == profile.logical_turns,
        "child_requires_explicit_blob_grant": child_blocked_before_grant
        and child_grant_succeeded
        and child_access_after_grant,
        "unrelated_session_remains_blocked": unrelated_blocked,
        "single_active_summary": all(row["active_summaries"] <= 1 for row in turns),
        "no_compaction_failure": compaction_failures == 0,
        "no_protocol_repair": protocol_repairs == 0,
        "no_context_limit": context_limits == 0,
    }
    summary: dict[str, Any] = {
        "schema_version": 1,
        "benchmark": "context-full-stack-soak",
        "profile": asdict(profile),
        "metrics": {
            "logical_turns": profile.logical_turns,
            "model_requests": len(provider.traces),
            "agent_requests": len(provider.agent_requests),
            "input_tokens": sum(trace.prompt_tokens for trace in provider.traces),
            "output_tokens": sum(trace.output_tokens for trace in provider.traces),
            "compactions": len(compaction_events),
            "runtime_rebuilds": runtime_rebuilds,
            "session_forks": int(fork_session_id is not None),
            "externalized_source_results": externalized_source_results,
            "unique_context_blobs": blob_stats["unique_blobs"],
            "logical_blob_bytes": blob_stats["logical_blob_bytes"],
            "registered_external_tools": total_registered_tools,
            "max_selected_tool_schemas": max(
                (len(request.tools) for request in provider.agent_requests), default=0
            ),
            "expected_fact_count": len(expected_facts),
        },
        "lifecycle": {
            "source_session_id": source_session_id,
            "fork_session_id": fork_session_id,
            "final_session_id": session_id,
            "child_session_id": child_session,
            "unrelated_session_id": unrelated_session,
        },
        "quality": {"passed": all(gates.values()), "gates": gates},
    }
    artifacts = workspace / "artifacts"
    artifacts.mkdir(exist_ok=True)
    (artifacts / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with (artifacts / "turns.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(turns[0]))
        writer.writeheader()
        writer.writerows(turns)
    with (artifacts / "requests.jsonl").open("w", encoding="utf-8") as handle:
        for trace in provider.traces:
            handle.write(json.dumps(asdict(trace), ensure_ascii=False, sort_keys=True) + "\n")
    return ContextFullStackResult(
        profile=profile,
        workspace=workspace,
        summary=summary,
        turns=tuple(turns),
    )


def _validate_profile(profile: ContextFullStackProfile) -> None:
    if profile.logical_turns < 6:
        raise ValueError("logical_turns 至少为 6")
    if profile.tool_output_chars < 512:
        raise ValueError("tool_output_chars 至少为 512")
    if not 1 <= profile.resume_after_turn < profile.fork_after_turn < profile.logical_turns:
        raise ValueError("resume/fork 位置必须严格位于长任务内部且顺序正确")
    if profile.dummy_tool_count < 1:
        raise ValueError("dummy_tool_count 必须大于 0")


def _cache_profile(profile: ContextFullStackProfile) -> ContextCacheBenchmarkProfile:
    base = (
        FAST_CONTEXT_CACHE_PROFILE if profile.name == "fast" else SOAK_CONTEXT_CACHE_PROFILE
    )
    return replace(
        base,
        logical_turns=profile.logical_turns,
        tool_output_chars=profile.tool_output_chars,
        stable_memory_chars=128,
        fact_every=profile.fact_every,
        seed=profile.seed,
        expected_min_compactions=0,
        expected_min_rebuilds=0,
    )


def _prepare_workspace(workspace: Path) -> None:
    (workspace / "AGENTS.md").write_text(
        f"# Full-stack benchmark\n\nPreserve {_AGENTS_MARKER} in every request.\n",
        encoding="utf-8",
    )
    skill_dir = workspace / "skills" / "full-stack-context"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: full-stack-context\n"
        "description: Deterministic context rehydration benchmark skill.\n"
        "---\n"
        f"Keep {_SKILL_MARKER} available throughout the long task.\n",
        encoding="utf-8",
    )


def _build_runtime(
    *,
    config: AppConfig,
    workspace: Path,
    store: SQLiteSessionStore,
    events: MemoryEventSink,
    provider: DeterministicContextCacheProvider,
    tools: ToolRegistry,
) -> tuple[AgentRunner, LocalExecutionTarget]:
    catalog = SkillCatalog(workspace / "skills")
    catalog.scan()
    execution_target = LocalExecutionTarget()
    event_bus = EventBus([store, events])
    compactor = ContextCompactor(
        config=config,
        provider=provider,
        store=store,
        event_bus=event_bus,
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
    return runner, execution_target


def _activate_primary_schema(runner: AgentRunner, session_id: str) -> None:
    result = runner._activate_tools(  # noqa: SLF001 - benchmark exercises schema activation
        ToolCall(
            id=f"activate-primary-{session_id}",
            name="activate_tools",
            arguments={"names": [CacheBenchmarkTool.name]},
        ),
        session_id,
    )
    if not result.success:
        raise AssertionError(result.error or "无法激活 benchmark Tool schema")


def _completed_compactions(store: SQLiteSessionStore, session_id: str) -> int:
    return store.count_context_compactions(
        session_id,
        statuses={"ready", "superseded"},
    )


def _message_references(store: SQLiteSessionStore, session_id: str) -> list[str]:
    references: list[str] = []
    for message in store.load_messages(session_id):
        references.extend(_REFERENCE_PATTERN.findall(message.content or ""))
    return list(dict.fromkeys(references))


def _blob_stats(database: Path) -> dict[str, int]:
    with sqlite3.connect(database) as connection:
        count, byte_count = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(byte_count), 0) FROM context_blobs"
        ).fetchone()
    return {"unique_blobs": int(count), "logical_blob_bytes": int(byte_count)}


__all__ = [
    "ContextFullStackProfile",
    "ContextFullStackResult",
    "FAST_CONTEXT_FULL_STACK_PROFILE",
    "SOAK_CONTEXT_FULL_STACK_PROFILE",
    "context_full_stack_profile",
    "run_context_full_stack_soak",
]
