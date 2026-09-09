from __future__ import annotations

import json
from uuid import uuid4

import pytest

from bot.compaction.handoff import SUMMARY_INSTRUCTION
from bot.config.models import AppConfig
from bot.core.models import ChatMessage, ModelCapabilities, ModelEvent, ModelEventKind, Role
from bot.evals.context_handoff import experiment_config, run_segment
from bot.evals.handoff_fixtures import Checkpoint, probe
from bot.evals.handoff_recording import ExperimentLedger
from bot.providers import ModelProvider

SUMMARY = "\n".join(
    f"# {title}\n测试状态未完成，请核验原始证据。"
    for title in (
        "Goal",
        "Constraints",
        "Progress",
        "Key Decisions",
        "Relevant Files",
        "Failures",
        "Next Steps",
        "Critical Context",
    )
)


class LoopProvider(ModelProvider):
    def capabilities(self, model):
        return ModelCapabilities()

    async def stream(self, request):
        last = request.messages[-1]
        content = last.content or ""
        if "只调用 request_handoff" in content:
            yield ModelEvent(
                kind=ModelEventKind.TOOL_CALL_DELTA,
                tool_index=0,
                tool_call_id=uuid4().hex,
                tool_name="request_handoff",
                arguments_delta=json.dumps({"reason": "test", "summary": SUMMARY}),
            )
        elif content.startswith(SUMMARY_INSTRUCTION) or last.name == "context_compaction_input":
            yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text=SUMMARY)
        elif "CHECKPOINT_READY" in content:
            yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="CHECKPOINT_READY")
        elif last.role == Role.TOOL:
            assert json.JSONDecoder().raw_decode(content)[0]["position"] == 1
            yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text='{"ok":true}')
        else:
            yield ModelEvent(
                kind=ModelEventKind.TOOL_CALL_DELTA,
                tool_index=0,
                tool_call_id=uuid4().hex,
                tool_name="checkpoint_evidence",
                arguments_delta='{"position":1}',
            )
        yield ModelEvent(
            kind=ModelEventKind.USAGE,
            input_tokens=1000,
            output_tokens=100,
            provider_metadata={
                "raw_usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 100,
                    "prompt_cache_hit_tokens": 800,
                    "prompt_cache_miss_tokens": 200,
                }
            },
        )
        yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["CURRENT", "A", "D"])
async def test_all_strategies_really_continue_through_runner_and_file_tools(tmp_path, strategy):
    checkpoint = Checkpoint(
        "test",
        "scripted-test-only",
        [
            ChatMessage(role=Role.USER, content="verify retained state"),
            ChatMessage(role=Role.ASSISTANT, content="old trace " * 10000),
        ],
        [probe("返回 ok 布尔值", {"ok": True}, read_position=1) for _ in range(8)],
        {},
    )
    ledger = ExperimentLedger(tmp_path / "requests.jsonl", max_cost_usd=1)
    result = await run_segment(
        checkpoint,
        strategy,
        1,
        directory=tmp_path / "segment",
        config=experiment_config(AppConfig()),
        provider=LoopProvider(),
        ledger=ledger,
    )
    assert result["error"] is None
    assert result["published"]
    assert result["all_probes_passed"] and result["first_tool_roundtrip_ok"]
    assert result["transcript_immutable"] and result["active_summary_count"] == 1
    assert result["metrics_by_phase"]["continuation"]["requests"] == 16
    assert all(len(probe["reads"]) == 1 for probe in result["probes"])
    shared = [
        row
        for row in ledger.rows
        if row["phase"] in {"warmup", "handoff_A", "handoff_D", "continuation"}
    ]
    assert len({row["system_sha256"] for row in shared}) == 1
    assert len({row["tools_sha256"] for row in shared}) == 1
