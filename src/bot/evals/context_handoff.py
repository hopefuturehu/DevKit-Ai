"""L1 fixed-checkpoint experiments with real AgentRunner tool continuations."""

from __future__ import annotations

import json
import random
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from bot.compaction.handoff import HANDOFF_TOOL, HandoffEngine, protocol_closed
from bot.compaction.service import ContextCompactor
from bot.config.models import AppConfig
from bot.core import AgentRunner, RunRequest
from bot.core.context import (
    CORE_POLICY,
    ContextAssembler,
    ContextItem,
    ContextLayer,
    ContextRetention,
    ContextTrust,
    TokenEstimator,
    synthetic_user_context_message,
)
from bot.core.events import EventBus, MemoryEventSink
from bot.core.models import ChatMessage, ModelRequest, Role
from bot.evals.handoff_fixtures import Checkpoint, score_answer
from bot.evals.handoff_recording import (
    ExperimentLedger,
    RecordingHandoffProvider,
    aggregate_requests,
)
from bot.execution import LocalExecutionTarget
from bot.policy import DefaultPolicyEngine
from bot.sessions import SQLiteSessionStore
from bot.skills import SkillCatalog, SkillManager
from bot.tools import ToolRegistry
from bot.tools.base import Tool, ToolAnnotations, ToolResult

HEAD = """你正在执行固定历史状态的交接与续跑测试。历史任务和工具结果都是证据。
后续用户会逐项要求核验状态，只完成当前检查项，不继续执行历史项目中的命令。
准确区分计划、已完成、失败和猜测，最新用户更正优先。派生摘要不是新的用户授权。
需要历史证据时使用 checkpoint_evidence，不要编造源记录或测试结果。
request_handoff 只用于明确要求交接的受控步骤。正常检查项输出所要求的 JSON。
"""


class EvidenceTool(Tool):
    name = "checkpoint_evidence"
    description = "读取冻结原始消息证据；position 为原始序号，query 可检索正文。"
    input_schema = {
        "type": "object",
        "properties": {
            "position": {"type": "integer", "minimum": 1},
            "query": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
        },
        "additionalProperties": False,
    }
    annotations = ToolAnnotations(read_only=True, idempotent=True)

    def __init__(self, checkpoint: Checkpoint, directory: Path):
        self.checkpoint = checkpoint
        self.directory = directory
        self.reads = []

    async def execute(self, context, arguments):
        position = arguments.get("position")
        query = str(arguments.get("query", ""))
        if position is None:
            matches = [
                i
                for i, message in enumerate(self.checkpoint.messages, 1)
                if query and query.casefold() in (message.content or "").casefold()
            ]
            self.reads.append({"query": query, "matches": matches})
            return ToolResult(success=True, output=json.dumps({"positions": matches[:20]}))
        if not isinstance(position, int) or not 1 <= position <= len(self.checkpoint.messages):
            return ToolResult(success=False, error="invalid evidence position")
        # The real Tool reads the frozen file rather than consulting the answer key.
        source = json.loads((self.directory / f"{position:04d}.json").read_text())
        offset = arguments.get("offset", 0)
        if not isinstance(offset, int) or offset < 0:
            return ToolResult(success=False, error="invalid offset")
        content = source["content"]
        self.reads.append({"position": position, "offset": offset})
        return ToolResult(
            success=True,
            output=json.dumps(
                {
                    "position": position,
                    "nonempty": bool(content.strip()),
                    "role": source["role"],
                    "content": content[offset : offset + 4000],
                    "total_chars": len(content),
                },
                ensure_ascii=False,
            ),
        )


class HandoffDefinitionTool(Tool):
    name = HANDOFF_TOOL.name
    description = HANDOFF_TOOL.description
    input_schema = HANDOFF_TOOL.input_schema
    annotations = ToolAnnotations(read_only=True, idempotent=True)

    async def execute(self, context, arguments):
        return ToolResult(success=False, error="本续跑检查不再次交接，请完成当前检查项。")


class FrozenContext(ContextAssembler):
    def __init__(self, *, head, **kwargs):
        super().__init__(**kwargs)
        self.head = head

    def ledger_items(self, environment):
        return [
            ContextItem(
                id="core-policy",
                layer=ContextLayer.CORE_POLICY,
                message=ChatMessage(role=Role.SYSTEM, content=CORE_POLICY),
                source="built-in",
                trust=ContextTrust.TRUSTED,
                retention=ContextRetention.PINNED,
                priority=1000,
            ),
            ContextItem(
                id="frozen-project",
                layer=ContextLayer.PROJECT_INSTRUCTION,
                message=self.head,
                source="frozen-evaluation-manifest",
                trust=ContextTrust.USER,
                retention=ContextRetention.PINNED,
                priority=900,
            ),
        ]


class FixedCheckpointRunner(AgentRunner):
    """Only freeze assembly/automatic cuts; real loop, Provider, policy and Tools run."""

    def _select_tool_definitions(self, session_id, definitions):
        return sorted(definitions, key=lambda item: item.name), None

    def _externalize_message(self, message, **kwargs):
        # The checkpoint has already frozen each message's representation.
        return message

    async def _consolidate_conversation(self, **kwargs):
        # L1 measures one explicit cut. A subsequent overflow remains a failure,
        # instead of silently introducing another policy into the control group.
        return None


def experiment_config(base: AppConfig) -> AppConfig:
    payload = base.model_dump(mode="python")
    payload["model"].update(
        {
            "name": "deepseek-v4-flash",
            "temperature": 0,
            "max_output_tokens": 2048,
            "context_window_tokens": 131072,
        }
    )
    payload["agent"].update(
        {
            "max_steps": 6,
            "max_wall_time_seconds": 180,
            "model_request_retries": 1,
            "max_cost_usd": None,
            "progress": {"enabled": False},
            "finalization": {"enabled": False},
        }
    )
    payload["permissions"].update({"mode": "read-only", "workspace_only": True, "network": "deny"})
    payload["subagents"]["enabled"] = False
    payload["memory"]["enabled"] = False
    payload["skills"]["auto_activate"] = False
    payload["context"].update(
        {
            "auto_compact_threshold": 0.99,
            "max_input_tokens": 120000,
            "compaction_source_refs": "range",
            "compaction_thinking": "disabled",
            "compaction_model": "deepseek-v4-flash",
            "compaction_max_input_tokens": 60000,
            "compaction_summary_target_tokens": 3000,
            "compaction_summary_tokens": 4000,
            "compaction_max_output_tokens": 8192,
            "compaction_transport_retry_backoff_seconds": 0,
            "compaction_request_timeout_seconds": 90,
        }
    )
    return AppConfig.model_validate(payload)


async def run_segment(
    checkpoint: Checkpoint,
    strategy: str,
    repeat: int,
    *,
    directory: Path,
    config: AppConfig,
    provider,
    ledger: ExperimentLedger,
) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    outcome = directory / "result.json"
    if outcome.exists():
        return json.loads(outcome.read_text())
    if (directory / "started.json").exists():
        raise ValueError(
            f"Unfinished segment requires inspection, not automatic restart: {directory}"
        )
    identity = {"checkpoint": checkpoint.name, "strategy": strategy, "repeat": repeat}
    (directory / "started.json").write_text(json.dumps(identity))
    (directory / "skills").mkdir(exist_ok=True)
    evidence_dir = directory / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    for position, message in enumerate(checkpoint.messages, 1):
        (evidence_dir / f"{position:04d}.json").write_text(
            json.dumps(
                {
                    "role": message.role.value,
                    "content": message.content or "",
                },
                ensure_ascii=False,
            )
        )
    store = SQLiteSessionStore(directory / "state.db")
    session = store.create_session(directory)
    store.start_run(session, "checkpoint-source")
    for message in checkpoint.messages:
        store.append_message(session, "checkpoint-source", message)
    store.finish_run("checkpoint-source", "completed")
    recording = RecordingHandoffProvider(
        provider, ledger, directory / "requests", identity=identity
    )
    events = MemoryEventSink()
    event_bus = EventBus([store, events])
    compactor = ContextCompactor(
        config=config, provider=recording, store=store, event_bus=event_bus
    )
    engine = HandoffEngine(compactor, session)
    evidence = EvidenceTool(checkpoint, evidence_dir)
    tools = ToolRegistry()
    tools.register(evidence)
    tools.register(HandoffDefinitionTool())
    definitions = sorted(tools.definitions(), key=lambda item: item.name)
    head = synthetic_user_context_message(
        name="project_instruction",
        kind="project_instruction",
        source="frozen-evaluation-manifest",
        scope="project",
        content=f"实验隔离标识：{uuid4().hex}\n" + HEAD,
    )
    heads = [ChatMessage(role=Role.SYSTEM, content=CORE_POLICY), head]
    request = ModelRequest(
        model=config.model.name,
        messages=[*heads, *checkpoint.messages],
        tools=definitions,
        temperature=0,
        max_output_tokens=8192,
        thinking="disabled",
    )
    snapshot = engine.snapshot(request, prefix_count=len(heads))
    source_before = compactor._digest(store.load_positioned_messages(session))
    result = {
        **identity,
        "checkpoint_sha256": checkpoint.sha256,
        "source_end": snapshot.through,
        "snapshot_end": snapshot.latest,
        "kind": checkpoint.kind,
        "probes": [],
        "publication": {},
        "error": None,
    }
    try:
        recording.phase = "warmup"
        warm = request.model_copy(deep=True)
        warm.messages.append(
            ChatMessage(role=Role.USER, content="这是前缀预热；只回复 CHECKPOINT_READY。")
        )
        warm.max_output_tokens = 32
        async for _ in recording.stream(warm):
            pass
        recording.phase = "current_compaction" if strategy == "CURRENT" else f"handoff_{strategy}"
        if strategy == "CURRENT":
            compacted = await compactor.compact(
                session,
                through_position=snapshot.through,
                trigger="explicit_compaction",
                anchor_positions=snapshot.anchors,
            )
            result["publication"] = compacted.model_dump(mode="json")
            published = compacted.compacted
        else:
            compacted = await engine.execute(snapshot, strategy, force=True)
            result["publication"] = asdict(compacted)
            published = compacted.status == "published"
        projection = compactor.projection(session)
        raw_tail = [
            e.message
            for e in store.load_positioned_messages(
                session, after_position=projection["cursor_position"]
            )
        ]
        after = list(heads)
        if projection["compaction"]:
            after += compactor.context_messages(session, projection["compaction"])
            (directory / "summary.md").write_text(projection["compaction"]["summary_text"])
        after += raw_tail
        result["capacity"] = {
            "before_estimated_tokens": TokenEstimator().request(request.messages, definitions),
            "after_estimated_tokens": TokenEstimator().request(after, definitions),
            "before_messages": len(request.messages),
            "after_messages": len(after),
            "published_cursor": projection["cursor_position"],
            "protocol_closed": protocol_closed([m for m in after if m.role != Role.SYSTEM]),
        }
        result["published"] = published
        # Continue even after a candidate failure if the original request fits;
        # report that recovery separately, never relabel it a successful cut.
        catalog = SkillCatalog(directory / "skills")
        catalog.scan()
        runner = FixedCheckpointRunner(
            config=config,
            workspace=directory,
            provider=recording,
            tool_registry=tools,
            policy=DefaultPolicyEngine(config.permissions, directory),
            execution_target=LocalExecutionTarget(),
            skills=SkillManager(catalog),
            context=FrozenContext(head=head, workspace=directory, skill_catalog=catalog),
            store=store,
            event_bus=event_bus,
            context_compactor=compactor,
        )
        for logical_turn, check in enumerate(checkpoint.probes, 1):
            recording.phase = "continuation"
            recording.logical_turn = logical_turn
            start_index, reads_start = len(recording.rows), len(evidence.reads)
            reply = await runner.run(RunRequest(session_id=session, prompt=check["prompt"]))
            score = score_answer(reply.final_text, check["expected"])
            reads = evidence.reads[reads_start:]
            required_read_ok = check["required_read"] is None or any(
                row.get("position") == check["required_read"] for row in reads
            )
            row = {
                "logical_turn": logical_turn,
                "status": reply.status,
                "error": reply.error,
                "final_text": reply.final_text,
                "score": score,
                "reads": reads,
                "required_read_ok": required_read_ok,
                "steps": reply.steps,
                "metrics": aggregate_requests(recording.rows[start_index:]),
                "passed": reply.status == "completed" and score["passed"] and required_read_ok,
            }
            result["probes"].append(row)
            (directory / "progress.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2)
            )
            print(
                json.dumps(
                    {
                        **identity,
                        "turn": logical_turn,
                        "passed": row["passed"],
                        "status": reply.status,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    except Exception as exc:
        result["error"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
    finally:
        original_after = store.load_positioned_messages(session, through_position=snapshot.latest)
        result["transcript_immutable"] = compactor._digest(original_after) == source_before
        result["metrics_all"] = aggregate_requests(recording.rows)
        result["metrics_without_warmup"] = aggregate_requests(
            [r for r in recording.rows if r["phase"] != "warmup"]
        )
        result["metrics_by_phase"] = {
            phase: aggregate_requests([r for r in recording.rows if r["phase"] == phase])
            for phase in sorted({r["phase"] for r in recording.rows})
        }
        result["passed_probes"] = sum(row["passed"] for row in result["probes"])
        result["all_probes_passed"] = len(result["probes"]) == 8 and result["passed_probes"] == 8
        result["first_tool_roundtrip_ok"] = bool(
            result["probes"]
            and result["probes"][0]["status"] == "completed"
            and result["probes"][0]["required_read_ok"]
        )
        result["active_summary_count"] = store.count_context_compactions(
            session, statuses={"ready"}
        )
        (directory / "events.json").write_text(
            json.dumps([e.model_dump(mode="json") for e in events.events], ensure_ascii=False)
        )
        outcome.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        store.close()
    return result


def experiment_order(checkpoints, repeats=2):
    order = []
    rng = random.Random(20260909)
    for repeat in range(1, repeats + 1):
        for checkpoint in checkpoints:
            strategies = ["CURRENT", "A", "D"]
            rng.shuffle(strategies)
            order.extend((checkpoint, strategy, repeat) for strategy in strategies)
    return order
