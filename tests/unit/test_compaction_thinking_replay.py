"""Ensure the live replay measures real validation failures without running a model."""

import importlib.util
from pathlib import Path

import pytest

from bot.config.models import AppConfig
from bot.core.models import ChatMessage, ModelEvent, ModelEventKind, ModelRequest, Role

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/test_compaction_thinking_budget.py"
SPEC = importlib.util.spec_from_file_location("thinking_replay", SCRIPT)
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)

SUMMARY = "\n\n".join(
    f"# {section}\n- Task state."
    for section in (
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


def test_variants_preserve_frozen_input_and_original_request():
    request = ModelRequest(
        model="deepseek-v4-flash",
        messages=[
            ChatMessage(role=Role.SYSTEM, content="Unchanged summary prompt"),
            ChatMessage(role=Role.USER, content='{"history": "unchanged"}'),
        ],
        max_output_tokens=8192,
        temperature=0,
    )
    original = request.model_dump()
    for mode in replay.MODES:
        changed = replay.variant(request, mode).model_dump()
        assert {k: v for k, v in changed.items() if k not in {"thinking", "max_output_tokens"}} == {
            k: v for k, v in original.items() if k not in {"thinking", "max_output_tokens"}
        }
        assert (
            changed["thinking"]
            == {"current": None, "thinking_off": "disabled", "thinking_reserve_8k": "enabled"}[mode]
        )
        assert changed["max_output_tokens"] == (16384 if mode == "thinking_reserve_8k" else 8192)
    assert request.model_dump() == original


@pytest.mark.parametrize(
    "finish,summary,completion,reasoning,passes,body_passes,error_class",
    [
        ("stop", SUMMARY, 60, 10, True, True, None),
        ("length", SUMMARY, 8192, 10, False, False, "output_length"),
        ("length", "", 8192, 8192, False, False, "empty"),
        ("stop", "# Goal\nIncomplete format", 60, 10, False, False, "format"),
        (None, SUMMARY, 60, 10, False, False, "unknown"),
        ("stop", SUMMARY, 15000, 6000, True, False, None),
        ("stop", SUMMARY, 60, None, True, False, None),
    ],
)
async def test_replay_uses_runtime_validation_and_separates_body_budget(
    tmp_path, monkeypatch, finish, summary, completion, reasoning, passes, body_passes, error_class
):
    class FakeProvider(replay.RecordedProvider):
        async def stream(self, request):
            yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text=summary)
            yield ModelEvent(
                kind=ModelEventKind.USAGE,
                input_tokens=100,
                output_tokens=completion,
                provider_metadata={
                    "raw_usage": {
                        "prompt_tokens": 100,
                        "prompt_cache_hit_tokens": 50,
                        "prompt_cache_miss_tokens": 50,
                        "completion_tokens": completion,
                        "completion_tokens_details": {"reasoning_tokens": reasoning},
                    }
                },
            )
            if finish is not None:
                yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason=finish)

    monkeypatch.setattr(replay, "RecordedProvider", FakeProvider)
    config = AppConfig()
    config.model.base_url = "https://api.deepseek.com/v1"
    request = ModelRequest(
        model="mock",
        messages=[ChatMessage(role=Role.USER, content="Frozen history")],
        max_output_tokens=8192,
    )
    record = await replay.trial(
        tmp_path / "trial",
        {"request": request, "request_ref": "fake-ref", "covered_end": 20},
        1,
        "current",
        config,
        "offline-unused",
    )
    assert record["candidate_pass"] is passes
    assert record["body_budget_pass"] is body_passes
    assert record.get("error_class") == error_class
    assert record["finish_reason"] == finish
    assert record["body_tokens"] == (None if reasoning is None else completion - reasoning)
    assert (tmp_path / "trial/summary.md").read_text() == summary


@pytest.mark.parametrize(
    "when,expected",
    [
        ("2026-09-17T08:00:00+00:00", 1),
        ("2026-09-17T20:00:00+00:00", 0.5),
        ("2026-09-19T08:00:00+00:00", 0.5),
    ],
)
def test_price_uses_peak_window_and_weekends(when, expected):
    assert replay.peak_multiplier(when) == expected


@pytest.mark.parametrize(
    "thinking,reasoning,calls,expected",
    [
        ("disabled", "", {}, 0),
        (None, "", {}, None),
        ("enabled", "", {}, None),
        ("disabled", "unexpected reasoning", {}, None),
        ("disabled", "", {"0": "unexpected tool"}, None),
    ],
)
def test_missing_reasoning_usage_requires_disabled_and_no_reasoning(
    thinking, reasoning, calls, expected
):
    split = replay.token_split(
        {"completion_tokens": 8192}, {"reasoning": reasoning, "tool_calls": calls}, thinking
    )
    assert split["reported_reasoning_tokens"] is None
    assert split["reasoning_tokens"] == expected
    assert split["body_tokens"] == (8192 if expected == 0 else None)
