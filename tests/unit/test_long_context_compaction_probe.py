"""Protect probe attribution and real checkpoint rollback without a paid API."""

import importlib.util
from pathlib import Path

import pytest

from bot.config.models import AppConfig
from bot.core.context import PositionedMessage
from bot.core.models import (
    ChatMessage,
    InputTokenEstimate,
    ModelCapabilities,
    ModelEvent,
    ModelEventKind,
    ModelRequest,
    Role,
)
from bot.providers import ModelProvider

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/test_long_context_compaction.py"
SPEC = importlib.util.spec_from_file_location("long_compaction_probe", SCRIPT)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)

SUMMARY = "\n\n".join(
    f"# {heading}\n- Preserve requirements and verify pending work."
    for heading in (
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


class FakeProvider(ModelProvider):
    def __init__(self, finishes):
        self.finishes = iter(finishes)
        self.requests = []

    def capabilities(self, model):
        return ModelCapabilities()

    def estimate_input_tokens(self, request):
        count = sum(len(m.model_dump_json()) for m in request.messages) // 3
        return InputTokenEstimate(tokens=count, budget_tokens=count, source="test")

    async def stream(self, request):
        self.requests.append(request.model_copy(deep=True))
        yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text=SUMMARY)
        yield ModelEvent(
            kind=ModelEventKind.USAGE,
            input_tokens=6000,
            output_tokens=200,
            provider_metadata={
                "raw_usage": {
                    "prompt_tokens": 6000,
                    "completion_tokens": 200,
                    "completion_tokens_details": {"reasoning_tokens": 0},
                }
            },
        )
        yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason=next(self.finishes))


def setup(tmp_path, mode, finishes):
    config = AppConfig.model_validate(
        {
            "model": {"name": "mock", "context_window_tokens": 1_000_000},
            "context": {
                "compaction_strategy": "a_fallback",
                "compaction_isolated_thinking": "inherit",
                "max_input_tokens": 900_000,
                "compaction_max_output_tokens": 8192,
                "compaction_low_water_tokens": 40_000,
            },
        }
    )
    entries = [
        PositionedMessage(
            1, ChatMessage(role=Role.USER, content="Keep the original end conditions.")
        ),
        PositionedMessage(
            2,
            ChatMessage(
                role=Role.ASSISTANT,
                content="Evidence. " * 6000,
                reasoning_content="Prior recorded reasoning.",
            ),
        ),
        PositionedMessage(3, ChatMessage(role=Role.ASSISTANT, content="Recent tail preserved.")),
    ]
    template = ModelRequest(
        model="mock",
        thinking="enabled",
        max_output_tokens=8192,
        messages=[ChatMessage(role=Role.SYSTEM, content="Policy")],
    )
    fixture = {"template": template, "entries": entries, "through": 2}
    base = template.model_copy(
        update={"messages": template.messages + [e.message for e in entries]}
    )
    provider = FakeProvider(finishes)
    engine, store, sink = probe.setup_trial(
        tmp_path / "trial", fixture, base, provider, config, mode
    )
    return engine, store, provider, sink, entries


@pytest.mark.parametrize(
    "mode,finishes,expected_thinking,published",
    [
        ("prefix_enabled", ["stop"], ["enabled"], True),
        ("prefix_enabled", ["length"], ["enabled"], False),
        ("isolated_enabled", ["stop"], ["enabled"], True),
        ("isolated_enabled", ["length"], ["enabled"], False),
        ("isolated_disabled", ["stop"], ["disabled"], True),
        ("isolated_disabled", ["length"], ["disabled"], False),
        ("fallback_disabled", ["length", "stop"], ["enabled", "disabled"], True),
        ("fallback_disabled", ["length", "length"], ["enabled", "disabled"], False),
    ],
)
async def test_real_publish_and_rollback(tmp_path, mode, finishes, expected_thinking, published):
    engine, store, provider, _, entries = setup(tmp_path, mode, finishes)
    try:
        result = await engine.compact(2, [1])
        assert [r.thinking for r in provider.requests] == expected_thinking
        assert result.compacted is published
        assert [e.message for e in store.load_positioned_messages(engine.session_id)] == [
            e.message for e in entries
        ]
        projection = engine.compactor.projection(engine.session_id)
        if published:
            assert projection["cursor_position"] == 2
            messages = engine.frame.project(projection["compaction"]).messages
            assert entries[0].message in messages and messages[-1] == entries[-1].message
        else:
            assert projection == {"cursor_position": 0, "compaction": None}
        if "disabled" in expected_thinking:
            isolated = provider.requests[-1]
            assert not isolated.tools
            assert "Prior recorded reasoning." in isolated.messages[1].content
    finally:
        store.close()


async def test_neutral_wording_only_changes_declared_phrase(tmp_path):
    engine, store, provider, _, _ = setup(tmp_path, "prefix_neutral", ["stop"])
    try:
        base_before = engine.frame.request.model_dump()
        result = await engine.compact(2, [1])
        assert result.compacted
        request = provider.requests[0]
        assert request.messages[:-1] == engine.frame.request.messages
        assert probe.NEUTRAL_WORDING in request.messages[-1].content
        assert probe.ORIGINAL_WORDING not in request.messages[-1].content
        assert engine.frame.request.model_dump() == base_before
    finally:
        store.close()


def test_probe_explicitly_preserves_enabled_arm(monkeypatch):
    # No workspace credentials are needed to inspect the experiment overrides.
    monkeypatch.setattr(
        probe, "load_config", lambda root, *, overrides: AppConfig.model_validate(overrides)
    )
    assert probe.config_for_probe().context.compaction_isolated_thinking == "inherit"
