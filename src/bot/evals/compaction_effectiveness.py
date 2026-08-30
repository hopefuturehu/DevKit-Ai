from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from bot.compaction import ContextCompactor
from bot.config.models import AppConfig
from bot.core.context import TokenEstimator
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
from bot.providers import ModelProvider, ProviderError, ProviderErrorKind
from bot.sessions import SQLiteSessionStore

ReplayScenario = Literal[
    "success",
    "length",
    "format",
    "rate-limit",
    "context-overflow",
    "authentication",
]

_SECTIONS = (
    "Goal",
    "Constraints",
    "Progress",
    "Key Decisions",
    "Relevant Files",
    "Failures",
    "Next Steps",
    "Critical Context",
)


@dataclass(frozen=True)
class ReplayEntry:
    run_id: str
    message: ChatMessage


@dataclass(frozen=True)
class CompactionEffectivenessResult:
    summary: dict[str, Any]
    artifacts: Path


class ScriptedCompactionProvider(ModelProvider):
    """Deterministic provider for replaying expensive failure paths offline."""

    def __init__(self, scenario: ReplayScenario = "success") -> None:
        self.scenario = scenario
        self.requests: list[ModelRequest] = []
        self._injected = False
        self._estimator = TokenEstimator()

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(usage_reporting=True)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        phase = request.messages[-1].name
        if not self._injected:
            if self.scenario == "rate-limit":
                self._injected = True
                raise ProviderError("scripted HTTP 429", kind=ProviderErrorKind.RATE_LIMIT)
            if self.scenario == "context-overflow":
                self._injected = True
                raise ProviderError(
                    "scripted maximum context length exceeded",
                    kind=ProviderErrorKind.CONTEXT_LENGTH,
                )
            if self.scenario == "authentication":
                self._injected = True
                raise ProviderError(
                    "scripted HTTP 401",
                    kind=ProviderErrorKind.AUTHENTICATION,
                )

        payload = json.loads(request.messages[-1].content or "{}")
        if self.scenario == "format" and not self._injected and phase == "context_compaction_input":
            self._injected = True
            facts = self._facts(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            text = "缺少标题的候选摘要\n" + "\n".join(f"- {fact}" for fact in facts)
            finish_reason = "stop"
        else:
            text = self._summary(
                payload,
                require_item_refs="[m:N]" in (request.messages[0].content or ""),
            )
            finish_reason = "stop"
            if (
                self.scenario == "length"
                and not self._injected
                and phase == "context_compaction_input"
            ):
                self._injected = True
                finish_reason = "length"
        yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text=text)
        yield ModelEvent(
            kind=ModelEventKind.USAGE,
            input_tokens=self._estimator.request(request.messages, request.tools),
            output_tokens=self._estimator.text(text),
            provider_metadata={
                "raw_usage": {
                    "prompt_tokens": self._estimator.request(request.messages, request.tools),
                    "completion_tokens": self._estimator.text(text),
                },
                "scripted": True,
            },
        )
        yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason=finish_reason)

    @classmethod
    def _summary(
        cls,
        payload: dict[str, Any],
        *,
        require_item_refs: bool,
    ) -> str:
        source = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        facts = sorted(set(cls._facts(source)))
        covered = payload.get("covered_range") or payload.get("allowed_reference_range") or [1, 1]
        reference = f" [m:{covered[0]}-{covered[-1]}]" if require_item_refs else ""
        fact_lines = (
            "\n".join(f"- {fact}{reference}" for fact in facts)
            or f"- 没有额外评测事实。{reference}"
        )
        content = {
            "Goal": f"- 继续完成可恢复的长任务。{reference}",
            "Constraints": fact_lines,
            "Progress": f"- 已将较早历史压缩为单一活动摘要。{reference}",
            "Key Decisions": f"- 使用不可变 Transcript 和范围级溯源。{reference}",
            "Relevant Files": f"- 文件状态由事实清单保留。{reference}",
            "Failures": f"- 不把失败或计划推断为成功。{reference}",
            "Next Steps": f"- 依据近期原文和摘要继续执行。{reference}",
            "Critical Context": fact_lines,
        }
        return "\n\n".join(f"# {section}\n{content[section]}" for section in _SECTIONS)

    @staticmethod
    def _facts(text: str) -> list[str]:
        return [
            match.group(0).strip()
            for match in re.finditer(
                r"\[EVAL_FACT:[^\]]+\]\s*[^\\\"\n}]*",
                text,
            )
        ]


def synthetic_replay_entries(
    *,
    turns: int = 12,
    tool_output_chars: int = 6_000,
) -> tuple[ReplayEntry, ...]:
    entries: list[ReplayEntry] = []
    for turn in range(1, turns + 1):
        run_id = f"eval-run-{turn:02d}"
        call_id = f"eval-call-{turn:02d}"
        fact = f"[EVAL_FACT:F{turn:02d}] constraint-{turn:02d}"
        entries.extend(
            [
                ReplayEntry(
                    run_id,
                    ChatMessage(role=Role.USER, content=f"第 {turn} 轮。{fact}"),
                ),
                ReplayEntry(
                    run_id,
                    ChatMessage(
                        role=Role.ASSISTANT,
                        tool_calls=[
                            ToolCall(
                                id=call_id,
                                name="eval_tool",
                                arguments={"turn": turn},
                            )
                        ],
                    ),
                ),
                ReplayEntry(
                    run_id,
                    ChatMessage(
                        role=Role.TOOL,
                        name="eval_tool",
                        tool_call_id=call_id,
                        content=(f"turn={turn} success=true\n" + "x" * tool_output_chars),
                    ),
                ),
                ReplayEntry(
                    run_id,
                    ChatMessage(role=Role.ASSISTANT, content=f"第 {turn} 轮完成。"),
                ),
            ]
        )
    return tuple(entries)


def load_replay_entries(path: Path) -> tuple[ReplayEntry, ...]:
    entries: list[ReplayEntry] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            payload = json.loads(line)
            try:
                run_id = str(payload.get("run_id") or "replay")
                message = ChatMessage.model_validate(payload["message"])
            except Exception as exc:
                raise ValueError(f"Transcript 第 {line_number} 行无效: {exc}") from exc
            entries.append(ReplayEntry(run_id=run_id, message=message))
    if not entries:
        raise ValueError("Transcript 为空")
    return tuple(entries)


async def run_compaction_effectiveness(
    *,
    config: AppConfig,
    provider: ModelProvider,
    entries: Iterable[ReplayEntry],
    workspace: Path,
    scenario: str,
    through_position: int | None = None,
    expect_success: bool = True,
    preserve_recent_tail: bool = False,
) -> CompactionEffectivenessResult:
    workspace.mkdir(parents=True, exist_ok=True)
    artifacts = workspace / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    store = SQLiteSessionStore(workspace / "state.db")
    events = MemoryEventSink()
    event_bus = EventBus([store, events])
    session_id = store.create_session(workspace)
    materialized = tuple(entries)
    active_run: str | None = None
    for entry in materialized:
        if entry.run_id != active_run:
            if active_run is not None:
                store.finish_run(active_run, "completed")
            store.start_run(session_id, entry.run_id)
            active_run = entry.run_id
        store.append_message(session_id, entry.run_id, entry.message)
    if active_run is not None:
        store.finish_run(active_run, "completed")

    latest = store.latest_message_position(session_id)
    if through_position is not None:
        target = min(through_position, latest)
    elif preserve_recent_tail:
        target = _target_before_recent_tail(
            store.load_positioned_messages(session_id),
            token_limit=config.context.recent_conversation_tokens,
        )
    else:
        target = latest
    before_digest = _transcript_digest(store, session_id)
    compactor = ContextCompactor(
        config=config,
        provider=provider,
        store=store,
        event_bus=event_bus,
    )
    result = await compactor.compact(
        session_id,
        through_position=target,
        trigger="explicit_compaction",
        request_limit=config.context.compaction_command_max_requests,
    )
    projection = compactor.projection(session_id)
    cursor = int(projection["cursor_position"])
    active = projection.get("compaction")
    assembled_parts: list[str] = []
    checkpoint_messages: list[ChatMessage] = []
    if isinstance(active, dict):
        checkpoint_messages = compactor.context_messages(session_id, active)
        assembled_parts.extend(message.content or "" for message in checkpoint_messages)
    assembled_parts.extend(
        entry.message.content or ""
        for entry in store.load_positioned_messages(session_id, after_position=cursor)
    )
    assembled = "\n".join(assembled_parts)
    expected_facts = sorted(
        set(
            fact
            for entry in materialized
            for fact in ScriptedCompactionProvider._facts(entry.message.content or "")
        )
    )
    visible_facts = [fact for fact in expected_facts if fact in assembled]
    summary_text = str(active.get("summary_text") or "") if isinstance(active, dict) else ""
    summary_fact_ids = set(_fact_ids(summary_text))
    expected_fact_ids = set(_fact_ids("\n".join(expected_facts)))
    request_events = [
        event
        for event in events.events
        if event.type
        in {
            EventType.CONTEXT_COMPACTION_REQUEST_STARTED,
            EventType.CONTEXT_COMPACTION_REQUEST_COMPLETED,
            EventType.CONTEXT_COMPACTION_REQUEST_FAILED,
        }
    ]
    request_rows = [event.model_dump(mode="json") for event in request_events]
    full_source_generations = sum(
        event.type == EventType.CONTEXT_COMPACTION_REQUEST_STARTED
        and event.payload.get("original_phase") == "generate"
        and event.payload.get("phase") == "generate"
        for event in request_events
    )
    raw_tail = store.load_positioned_messages(session_id, after_position=cursor)
    gates = {
        "expected_status": result.compacted is expect_success,
        "all_facts_visible": len(visible_facts) == len(expected_facts),
        "no_unsupported_fact_ids": summary_fact_ids <= expected_fact_ids,
        "transcript_immutable": before_digest == _transcript_digest(store, session_id),
        "cursor_monotonic": cursor >= 0 and (not result.compacted or cursor > 0),
        "single_active_summary": len(
            [
                item
                for item in store.list_context_compactions(session_id)
                if item["status"] == "ready"
            ]
        )
        <= 1,
        "bounded_input": result.planned_input_tokens <= result.input_limit,
        "tool_protocol_balanced": _tool_protocol_balanced(raw_tail),
        "latest_user_anchor_visible": (
            any(entry.message.role == Role.USER for entry in raw_tail)
            or any(message.role == Role.USER for message in checkpoint_messages)
            or not any(entry.message.role == Role.USER for entry in materialized)
            if preserve_recent_tail
            else True
        ),
        "no_full_source_repeat_for_output_or_format": (
            full_source_generations <= 1 if scenario in {"length", "format"} else True
        ),
    }
    summary = {
        "schema_version": 1,
        "scenario": scenario,
        "model": compactor.model_name,
        "thinking": compactor.thinking_mode or "provider_default",
        "source": {
            "messages": len(materialized),
            "through_position": target,
            "retained_messages": len(raw_tail),
            "retained_user_turns": sum(
                entry.message.role == Role.USER for entry in raw_tail
            ),
            "sha256": before_digest,
        },
        "result": result.model_dump(mode="json"),
        "quality": {
            "expected_facts": len(expected_facts),
            "visible_facts": len(visible_facts),
            "fact_recall": len(visible_facts) / max(1, len(expected_facts)),
            "unsupported_fact_ids": sorted(summary_fact_ids - expected_fact_ids),
            "gates": gates,
            "passed": all(gates.values()),
        },
        "requests": {
            "total": result.request_count,
            "full_source_generations": full_source_generations,
            "repairs": result.repair_attempts,
            "condenses": result.condense_attempts,
            "transport_retries": result.transport_retries,
        },
    }
    (artifacts / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with (artifacts / "requests.jsonl").open("w", encoding="utf-8") as stream:
        for row in request_rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    store.close()
    return CompactionEffectivenessResult(summary=summary, artifacts=artifacts)


def _transcript_digest(store: SQLiteSessionStore, session_id: str) -> str:
    digest = hashlib.sha256()
    for entry in store.load_positioned_messages(session_id):
        digest.update(str(entry.position).encode())
        digest.update(b"\0")
        digest.update(entry.message.model_dump_json().encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _fact_ids(text: str) -> list[str]:
    # Golden IDs deliberately use F + digits. Placeholders such as FXX or the
    # prompt's literal "..." describe a format and must not be scored as claims.
    return [match.group(1) for match in re.finditer(r"\[EVAL_FACT:(F\d+)\]", text)]


def _target_before_recent_tail(
    entries,
    *,
    token_limit: int,
) -> int:
    groups = ContextCompactor._atomic_groups(list(entries))
    estimator = TokenEstimator()
    retained_positions: set[int] = set()
    retained_tokens = 0
    for group in reversed(groups):
        cost = sum(estimator.message(entry.message) for entry in group)
        if retained_positions and retained_tokens + cost > token_limit:
            break
        retained_positions.update(entry.position for entry in group)
        retained_tokens += cost
    older = [entry.position for entry in entries if entry.position not in retained_positions]
    return max(older, default=0)


def _tool_protocol_balanced(entries) -> bool:
    calls = {
        call.id
        for entry in entries
        for call in entry.message.tool_calls
    }
    results = {
        entry.message.tool_call_id
        for entry in entries
        if entry.message.role == Role.TOOL and entry.message.tool_call_id
    }
    return calls <= results and results <= calls
