"""Opt-in A/B strategies used by the real AgentRunner.

B prepares immutable, closed transcript ranges while the main model runs. Only
the root is published; intermediate nodes never change the active context.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from time import monotonic
from typing import Any
from uuid import uuid4

from bot.compaction.handoff import SUMMARY_INSTRUCTION, protocol_closed
from bot.compaction.models import CompactionErrorClass, ContextCompactionResult
from bot.compaction.service import ContextCompactor
from bot.core.context import PositionedMessage
from bot.core.events import EventType
from bot.core.models import ChatMessage, ModelEventKind, ModelRequest, Role
from bot.providers import ModelProvider, ProviderError
from bot.providers.base import estimate_input_tokens


@dataclass(frozen=True)
class SummaryNode:
    start: int
    end: int
    source_digest: str
    summary: str
    reference: str
    children: tuple[str, ...] = ()


@dataclass
class StrategyFrame:
    request: ModelRequest
    # Must include Skill recovery and the normal runtime/context layers.
    project: Callable[[dict[str, Any]], ModelRequest]


class StrategyCompactor:
    def __init__(
        self,
        compactor: ContextCompactor,
        main_provider: ModelProvider,
        session_id: str,
        run_id: str,
    ) -> None:
        self.compactor = compactor
        self.config = compactor.config
        self.store = compactor.store
        self.provider = main_provider
        self.session_id, self.run_id = session_id, run_id
        self.strategy = self.config.context.compaction_strategy
        self.frame: StrategyFrame | None = None
        self.nodes: list[SummaryNode] = []
        self.background: asyncio.Task | None = None
        self.background_error: str | None = None
        self.failed_background_ends: set[int] = set()
        self.background_retry_after = self.retry_after = 0.0
        self.input_tokens = self.output_tokens = self.requests = 0
        self.accounted_input = self.accounted_output = 0
        self.published_input = self.published_output = 0
        self.last_metrics: dict[str, Any] = {}
        self.closed = False

    @property
    def input_limit(self) -> int:
        cfg = self.config.context
        return min(
            cfg.max_input_tokens,
            self.config.model.context_window_tokens
            - cfg.compaction_max_output_tokens
            - cfg.protocol_reserve_tokens
            - cfg.safety_margin_tokens,
        )

    def count(self, request: ModelRequest) -> int:
        estimate = estimate_input_tokens(self.provider, request)
        return (
            estimate.budget_tokens
            if estimate is not None
            else self.compactor._estimator.request(request.messages, request.tools)
        )

    async def emit(self, kind: EventType, **payload: Any) -> None:
        await self.compactor.event_bus.emit(
            kind,
            session_id=self.session_id,
            run_id=self.run_id,
            payload={"strategy": self.strategy, **payload},
        )

    def take_usage(self) -> tuple[int, int]:
        result = (
            self.input_tokens - self.accounted_input,
            self.output_tokens - self.accounted_output,
        )
        self.accounted_input, self.accounted_output = self.input_tokens, self.output_tokens
        return result

    async def close(self) -> None:
        self.closed = True
        if self.background is not None:
            if not self.background.done():
                self.background.cancel()
            await asyncio.gather(self.background, return_exceptions=True)
            self.background = None

    def schedule(self, through: int) -> None:
        if (
            self.closed
            or self.strategy != "b"
            or not self.config.context.compaction_background
            or through in self.failed_background_ends
            or monotonic() < self.background_retry_after
        ):
            return
        if self.background is not None and not self.background.done():
            return
        self.background = asyncio.create_task(self._prepare_background(through))

    async def _prepare_background(self, through: int) -> None:
        try:
            await self.prepare_leaves(through, flush=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.background_error = str(exc)
            self.failed_background_ends.add(through)
            self.background_retry_after = (
                monotonic() + self.config.context.compaction_failure_backoff_seconds
            )
            await self.emit(
                EventType.CONTEXT_COMPACTION_BLOCKED,
                phase="background",
                error=str(exc),
                source_end=through,
            )

    def node_request(self, payload: dict[str, Any]) -> ModelRequest:
        # JSON is a data representation, not a conversation with executable calls.
        return ModelRequest(
            model=self.compactor.model_name,
            messages=[
                ChatMessage(
                    role=Role.SYSTEM,
                    content=SUMMARY_INSTRUCTION
                    + "\n输入是按时间排列的历史数据。仅输出上述八节 Markdown。"
                    "叶子记录局部事实，未知项写未记录；合并时较晚的更正优先，保留失败和证据范围。"
                    "Skill 全文由运行器另行恢复，仅记录其用途，不复制整份手册。",
                ),
                ChatMessage(role=Role.USER, content=json.dumps(payload, ensure_ascii=False)),
            ],
            max_output_tokens=self.config.context.compaction_max_output_tokens,
            temperature=0,
            thinking=self.compactor.thinking_mode,
        )

    @staticmethod
    def source_payload(entries: list[PositionedMessage]) -> dict[str, Any]:
        return {
            "kind": "leaf",
            "source_range": [entries[0].position, entries[-1].position],
            "messages": [
                {"position": entry.position, "message": entry.message.model_dump(mode="json")}
                for entry in entries
            ],
        }

    async def summarize(
        self,
        request: ModelRequest,
        *,
        phase: str,
        start: int,
        end: int,
        limit: int,
    ) -> str:
        planned = self.count(request)
        if planned > limit:
            raise ValueError(f"summary_input_budget: {planned} > {limit}")
        for attempt in range(1, 3):
            attempt_id = uuid4().hex
            base = {
                "compaction_id": attempt_id,
                "phase": phase,
                "source_range": [start, end],
                "request_attempt": attempt,
                "model": request.model,
                "thinking": request.thinking or "provider_default",
                "planned_input_tokens": planned,
                "input_limit": limit,
                "max_output_tokens": request.max_output_tokens,
            }
            await self.emit(EventType.CONTEXT_COMPACTION_REQUEST_STARTED, **base)
            began = monotonic()
            text, finish, calls, raw_usage, metadata = [], None, False, {}, {}
            actual_input = actual_output = 0
            self.requests += 1
            try:
                async with asyncio.timeout(self.config.context.compaction_request_timeout_seconds):
                    async for event in self.provider.stream(request):
                        if event.kind == ModelEventKind.TEXT_DELTA:
                            text.append(event.text or "")
                        elif event.kind == ModelEventKind.TOOL_CALL_DELTA:
                            calls = True
                        elif event.kind == ModelEventKind.USAGE:
                            actual_input = event.input_tokens or actual_input
                            actual_output = event.output_tokens or actual_output
                            raw_usage = event.provider_metadata.get("raw_usage") or raw_usage
                            metadata = {
                                k: v
                                for k, v in event.provider_metadata.items()
                                if k
                                in {
                                    "input_token_estimate",
                                    "input_token_error",
                                    "input_budget_exceeded",
                                    "response_id",
                                    "system_fingerprint",
                                }
                            }
                        elif event.kind == ModelEventKind.FINISH:
                            finish = event.finish_reason
                if finish is None:
                    raise ValueError("stream_incomplete")
                if calls:
                    raise ValueError("unexpected_summary_tool_call")
                summary = self.compactor._normalize_summary("".join(text))
                self.compactor._validate_candidate(
                    summary,
                    finish_reason=finish,
                    covered_start=start,
                    covered_end=end,
                )
            except BaseException as exc:
                await self.emit(
                    EventType.CONTEXT_COMPACTION_REQUEST_FAILED,
                    **base,
                    duration_ms=(monotonic() - began) * 1000,
                    input_tokens=actual_input,
                    output_tokens=actual_output,
                    raw_usage=raw_usage,
                    finish_reason=finish,
                    usage_missing=not bool(raw_usage),
                    error=str(exc) or type(exc).__name__,
                    **metadata,
                )
                if (
                    not isinstance(exc, Exception)
                    or attempt == 2
                    or (isinstance(exc, ProviderError) and not exc.retryable)
                ):
                    raise
            else:
                await self.emit(
                    EventType.CONTEXT_COMPACTION_REQUEST_COMPLETED,
                    **base,
                    duration_ms=(monotonic() - began) * 1000,
                    input_tokens=actual_input,
                    output_tokens=actual_output,
                    raw_usage=raw_usage,
                    finish_reason=finish,
                    usage_missing=not bool(raw_usage),
                    **metadata,
                )
                return summary
            finally:
                # Only actual usage; never fabricate charges for failed requests.
                self.input_tokens += actual_input
                self.output_tokens += actual_output
        raise AssertionError("unreachable")

    async def save_node(
        self,
        start: int,
        end: int,
        summary: str,
        children: tuple[str, ...] = (),
    ) -> SummaryNode:
        entries = [
            e
            for e in self.store.load_positioned_messages(self.session_id)
            if start <= e.position <= end
        ]
        digest = self.compactor._digest(entries)
        payload = {
            "start": start,
            "end": end,
            "source_digest": digest,
            "summary": summary,
            "children": children,
        }
        reference = self.store.put_context_blob(
            session_id=self.session_id,
            run_id=self.run_id,
            content=json.dumps(payload, ensure_ascii=False),
            media_type="application/vnd.bot.summary-node+json",
        )
        node = SummaryNode(start, end, digest, summary, reference, children)
        await self.emit(
            EventType.CONTEXT_COMPACTION_NODE_READY,
            **{k: v for k, v in asdict(node).items() if k != "summary"},
        )
        return node

    async def prepare_leaves(self, through: int, *, flush: bool) -> list[SummaryNode]:
        active = self.compactor.projection(self.session_id)
        cursor = int(active["cursor_position"])
        entries = self.store.load_positioned_messages(self.session_id, after_position=cursor)
        entries = [e for e in entries if e.position <= through]
        # A forced publication can stop before a previously prepared leaf ends.
        # Reuse only a contiguous matching prefix, never all cached intervals:
        # rebuilding the uncovered suffix can leave overlapping cache entries.
        eligible = sorted(
            (n for n in self.nodes if cursor < n.start and n.end <= through),
            key=lambda n: (n.start, -n.end),
        )
        nodes = []
        next_position = cursor + 1
        for node in eligible:
            if node.start < next_position:
                continue
            if node.start > next_position:
                break
            covered = [e for e in entries if node.start <= e.position <= node.end]
            if self.compactor._digest(covered) != node.source_digest:
                raise ValueError("summary_node_source_changed")
            nodes.append(node)
            next_position = node.end + 1
        pending = [e for e in entries if e.position >= next_position]
        if not pending:
            return nodes
        if not protocol_closed([e.message for e in pending]):
            raise ValueError("incomplete_tool_group")
        limit = min(self.config.context.compaction_leaf_input_tokens, self.input_limit)
        chunks: list[list[PositionedMessage]] = []
        chunk: list[PositionedMessage] = []
        for group in self.compactor._atomic_groups(pending):
            if self.count(self.node_request(self.source_payload(group))) > limit:
                raise ValueError("atomic_group_exceeds_leaf_budget")
            candidate = chunk + group
            if chunk and self.count(self.node_request(self.source_payload(candidate))) > limit:
                chunks.append(chunk)
                chunk = []
            chunk.extend(group)
        if chunk and (
            flush or self.count(self.node_request(self.source_payload(chunk))) >= limit * 0.8
        ):
            chunks.append(chunk)
        for chunk in chunks:
            summary = await self.summarize(
                self.node_request(self.source_payload(chunk)),
                phase="b_leaf",
                start=chunk[0].position,
                end=chunk[-1].position,
                limit=limit,
            )
            node = await self.save_node(chunk[0].position, chunk[-1].position, summary)
            self.nodes.append(node)
            nodes.append(node)
        return nodes

    async def merge(self, nodes: list[SummaryNode]) -> SummaryNode:
        fanout = self.config.context.compaction_merge_fanout
        limit = min(self.compactor._compaction_input_limit(), self.input_limit)
        # At least one merge creates a task-level summary, even with one leaf.
        while True:
            groups: list[list[SummaryNode]] = []
            group: list[SummaryNode] = []
            for node in nodes:
                candidate = group + [node]
                request = self.node_request(
                    {"kind": "merge", "nodes": [asdict(n) for n in candidate]}
                )
                if group and (len(candidate) > fanout or self.count(request) > limit):
                    groups.append(group)
                    group = []
                group.append(node)
            if group:
                groups.append(group)
            if len(nodes) > 1 and all(len(group) == 1 for group in groups):
                raise ValueError("merge_input_budget_no_progress")
            parents = []
            for group in groups:
                request = self.node_request({"kind": "merge", "nodes": [asdict(n) for n in group]})
                summary = await self.summarize(
                    request, phase="b_merge", start=group[0].start, end=group[-1].end, limit=limit
                )
                parents.append(
                    await self.save_node(
                        group[0].start, group[-1].end, summary, tuple(n.reference for n in group)
                    )
                )
            if len(parents) == 1:
                return parents[0]
            nodes = parents

    def select_boundary(
        self,
        frame: StrategyFrame,
        entries: list[PositionedMessage],
        through: int,
        anchors: list[int],
    ) -> tuple[int, int]:
        """Budget the complete retained view before paying for a root summary.

        The legacy tail target uses a text heuristic. A/B must additionally fit
        the calibrated full request, including Skills and pending deliveries.
        Reserve the entire output allowance, rather than assume an average
        summary length. Final candidate validation remains authoritative.
        """
        candidates = [through] + [
            group[-1].position
            for group in self.compactor._atomic_groups(entries)[:-1]
            if group[-1].position > through
        ]
        reserve = math.ceil(self.config.context.compaction_max_output_tokens * 1.1) + 256
        limit = min(self.config.context.compaction_low_water_tokens, self.input_limit)
        for boundary in candidates:
            record = {
                "id": "0" * 32,
                "session_id": self.session_id,
                "covered_start_position": 1,
                "covered_end_position": boundary,
                "source_sha256": "0" * 64,
                "anchor_positions": [
                    e.position
                    for e in entries
                    if e.position <= boundary and e.is_real_user and e.position in anchors
                ],
                "summary_text": "[Summary output reserved separately]",
            }
            projected = self.count(frame.project(record)) + reserve
            if projected <= limit:
                return boundary, projected
        raise ValueError("fixed_context_exceeds_low_water")

    async def compact(self, through: int, anchors: list[int]) -> ContextCompactionResult:
        if self.frame is None:
            raise ValueError("strategy_context_frame_missing")
        if monotonic() < self.retry_after:
            return ContextCompactionResult(
                compacted=False, trigger=f"strategy:{self.strategy}", reason="failure_backoff"
            )
        frame = self.frame
        began = monotonic()
        self.last_metrics = {}
        before_requests = self.requests
        entries = self.store.load_positioned_messages(self.session_id)
        requested_through = through
        covered = [e for e in entries if e.position <= through]
        previous = self.compactor.projection(self.session_id)["compaction"]
        start = int(previous["covered_end_position"]) + 1 if previous else 1
        result = ContextCompactionResult(
            compacted=False,
            trigger=f"strategy:{self.strategy}",
            parent_id=previous["id"] if previous else None,
            covered_start_position=1,
            covered_end_position=through,
            requested_end_position=through,
            previous_end_position=start - 1,
            messages_compacted=len(covered),
            source_chars=sum(len(e.message.model_dump_json()) for e in covered),
            input_limit=self.input_limit,
        )
        await self.emit(
            EventType.CONTEXT_COMPACTION_STARTED, covered_range=[1, through], trigger=result.trigger
        )
        try:
            if not covered or not protocol_closed([e.message for e in entries]):
                raise ValueError("incomplete_tool_group")
            through, reserved_after = self.select_boundary(frame, entries, through, anchors)
            covered = [e for e in entries if e.position <= through]
            result.covered_end_position = through
            result.messages_compacted = len(covered)
            result.source_chars = sum(len(e.message.model_dump_json()) for e in covered)
            self.last_metrics = {
                "tail_target_end_position": requested_through,
                "selected_end_position": through,
                "reserved_after_tokens": reserved_after,
            }
            digest = self.compactor._digest(covered)
            references: list[str] = []
            if self.strategy == "a":
                request = frame.request.model_copy(deep=True)
                request.messages.append(
                    ChatMessage(
                        role=Role.USER,
                        content=SUMMARY_INSTRUCTION
                        + "\n只输出交接摘要，不调用工具。Skill 正文由运行器恢复，不复制整份手册。",
                    )
                )
                request.max_output_tokens = self.config.context.compaction_max_output_tokens
                request.tool_choice = None
                summary = await self.summarize(
                    request, phase="a_generate", start=1, end=through, limit=self.input_limit
                )
            else:
                if self.background is not None:
                    await self.background
                    self.background = None
                nodes = await self.prepare_leaves(through, flush=True)
                if previous:
                    nodes.insert(
                        0,
                        SummaryNode(
                            1,
                            start - 1,
                            previous["source_sha256"],
                            previous["summary_text"],
                            f"compaction:{previous['id']}",
                        ),
                    )
                position = 1
                for node in nodes:
                    if node.start != position:
                        raise ValueError("summary_coverage_gap")
                    position = node.end + 1
                if position != through + 1:
                    raise ValueError("summary_coverage_gap")
                root = await self.merge(nodes)
                summary, references = root.summary, [root.reference]
            live = self.store.load_positioned_messages(self.session_id)
            if self.compactor._digest(live) != self.compactor._digest(entries):
                raise ValueError("source_changed_during_compaction")
            real_anchors = [e.position for e in covered if e.is_real_user and e.position in anchors]
            record = {
                "id": "0" * 32,
                "session_id": self.session_id,
                "covered_start_position": 1,
                "covered_end_position": through,
                "source_sha256": digest,
                "anchor_positions": real_anchors,
                "summary_text": summary,
            }
            after_request = frame.project(record)
            before, after = self.count(frame.request), self.count(after_request)
            self.last_metrics = {
                **self.last_metrics,
                "strategy": self.strategy,
                "before_tokens": before,
                "after_tokens": after,
                "net_release_tokens": before - after,
                "background_error": self.background_error,
            }
            if not protocol_closed([m for m in after_request.messages if m.role != Role.SYSTEM]):
                raise ValueError("projection_protocol")
            if after > min(self.config.context.compaction_low_water_tokens, self.input_limit):
                raise ValueError(f"low_water_not_met: {after}")
            if after >= before:
                raise ValueError("insufficient_release")
            identifier = self.store.start_context_compaction(
                session_id=self.session_id,
                parent_id=result.parent_id,
                trigger=result.trigger,
                model=frame.request.model,
                covered_start_position=1,
                covered_end_position=through,
                delta_start_position=start,
                source_sha256=digest,
                anchor_positions=real_anchors,
                source_chars=result.source_chars,
            )
            try:
                self.store.complete_context_compaction(
                    identifier,
                    summary_text=summary,
                    summary_token_estimate=self.compactor._estimator.text(summary),
                    source_refs=references,
                    input_tokens=self.input_tokens - self.published_input,
                    output_tokens=self.output_tokens - self.published_output,
                    duration_ms=(monotonic() - began) * 1000,
                )
            except BaseException as exc:
                self.store.fail_context_compaction(identifier, str(exc))
                raise
            result.compacted, result.compaction_id = True, identifier
            result.summary_chars = len(summary)
            result.summary_tokens = self.compactor._estimator.text(summary)
            result.source_refs = references
            result.input_tokens = self.input_tokens - self.published_input
            result.output_tokens = self.output_tokens - self.published_output
            self.published_input, self.published_output = self.input_tokens, self.output_tokens
            result.request_count = self.requests - before_requests
            result.duration_ms = (monotonic() - began) * 1000
            await self.emit(
                EventType.CONTEXT_COMPACTION_COMPLETED,
                **result.model_dump(mode="json"),
                **self.last_metrics,
            )
        except Exception as exc:
            self.retry_after = monotonic() + self.config.context.compaction_failure_backoff_seconds
            result.error, result.reason = str(exc), type(exc).__name__
            result.error_class = CompactionErrorClass.UNKNOWN
            await self.emit(
                EventType.CONTEXT_COMPACTION_FAILED,
                **result.model_dump(mode="json"),
                **self.last_metrics,
            )
        result.input_tokens, result.output_tokens = self.take_usage()
        result.request_count = self.requests - before_requests
        result.duration_ms = (monotonic() - began) * 1000
        return result
