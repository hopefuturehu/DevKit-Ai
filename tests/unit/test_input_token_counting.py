from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from bot.core.models import (
    ChatMessage,
    ModelEventKind,
    ModelRequest,
    Role,
    ToolCall,
    ToolDefinition,
)
from bot.providers import OpenAICompatibleProvider
from bot.providers import token_counting as counting


def tool_request(thinking="disabled"):
    return ModelRequest(
        model="deepseek-v4-flash",
        thinking=thinking,
        messages=[
            ChatMessage(role=Role.USER, content="read"),
            ChatMessage(
                role=Role.ASSISTANT,
                reasoning_content="HISTORICAL_REASONING",
                tool_calls=[ToolCall(id="c1", name="read", arguments={"path": "file.py"})],
            ),
            ChatMessage(role=Role.TOOL, tool_call_id="c1", content="file contents"),
        ],
        tools=[
            ToolDefinition(name="read", description="read a file", input_schema={"type": "object"})
        ],
    )


def test_upstream_template_resolves_reasoning_and_tools_without_mutating_payload():
    provider = OpenAICompatibleProvider(base_url="https://api.deepseek.com", api_key="unused")
    payload = provider._payload(tool_request())
    saved = deepcopy(payload)
    chat = counting.render_deepseek_input(payload)
    assert payload == saved
    assert "HISTORICAL_REASONING" not in chat
    assert "<tool_result>file contents</tool_result>" in chat
    assert 'invoke name="read"' in chat
    assert "### Available Tool Schemas" in chat
    assert chat.endswith("<｜Assistant｜></think>")
    thinking = counting.render_deepseek_input(provider._payload(tool_request("enabled")))
    assert "HISTORICAL_REASONING" in thinking
    assert thinking.endswith("<｜Assistant｜><think>")


@pytest.mark.parametrize("model", ["deepseek-v4-flash", "deepseek-v4-pro"])
@pytest.mark.parametrize("mode", [None, "enabled", "disabled"])
def test_counter_uses_effective_mode_and_never_calls_it_exact(monkeypatch, model, mode):
    prompts = []

    def encode(prompt, *, add_special_tokens):
        assert add_special_tokens is False
        prompts.append(prompt)
        return SimpleNamespace(ids=range(10_000))

    monkeypatch.setattr(counting, "load_tokenizer", lambda _: SimpleNamespace(encode=encode))
    provider = OpenAICompatibleProvider(base_url="https://api.deepseek.com", api_key="unused")
    request = tool_request(mode)
    request.model = model
    request.messages[1].reasoning_content = None  # Empty history must not select non-thinking.
    estimate = provider.estimate_input_tokens(request)
    assert estimate.tokens == 10_000
    assert estimate.budget_tokens == 10_500
    assert estimate.effective_thinking == (mode or "provider_default")
    assert estimate.source.startswith(model + ":")
    assert provider.count_tokens(request) is None
    assert prompts[0].endswith("</think>" if mode == "disabled" else "<think>")
    assert provider.estimate_input_tokens(request) == estimate
    assert len(prompts) == 1  # Hash-only request cache.
    request.messages[-1].content = "new result"
    provider.estimate_input_tokens(request)
    assert len(prompts) == 2


def test_missing_tokenizer_uses_visible_conservative_fallback_and_can_recover(monkeypatch):
    def unavailable(_):
        raise FileNotFoundError

    monkeypatch.setattr(counting, "load_tokenizer", unavailable)
    provider = OpenAICompatibleProvider(base_url="https://api.deepseek.com", api_key="unused")
    first = provider.estimate_input_tokens(tool_request())
    assert first.source == "heuristic:deepseek_tokenizer_unavailable"
    assert first.budget_tokens >= first.tokens + 2048
    monkeypatch.setattr(
        counting,
        "load_tokenizer",
        lambda _: SimpleNamespace(encode=lambda *a, **kw: SimpleNamespace(ids=range(100))),
    )
    assert provider.estimate_input_tokens(tool_request()).source.startswith("deepseek-v4-flash:")


def test_tokenizer_verifies_asset_and_unknown_models_do_not_use_it(tmp_path, monkeypatch):
    path = tmp_path / "tokenizer.json"
    path.write_text("{}")
    with pytest.raises(ValueError, match="checksum"):
        counting.load_tokenizer(path)

    def must_not_load(_):
        pytest.fail("An unrelated model/endpoint must not use the Flash tokenizer")

    monkeypatch.setattr(counting, "load_tokenizer", must_not_load)
    provider = OpenAICompatibleProvider(base_url="https://example.test", api_key="unused")
    assert provider.estimate_input_tokens(tool_request()) is None
    provider = OpenAICompatibleProvider(base_url="https://api.deepseek.com", api_key="unused")
    request = tool_request()
    request.model = "unverified-model"
    assert provider.estimate_input_tokens(request) is None


def test_missing_tokenizer_counts_replayed_plain_answer_reasoning(monkeypatch):
    def unavailable(_):
        raise FileNotFoundError

    monkeypatch.setattr(counting, "load_tokenizer", unavailable)
    provider = OpenAICompatibleProvider(base_url="https://api.deepseek.com", api_key="unused")
    request = tool_request("enabled")
    answer = ChatMessage(role=Role.ASSISTANT, content="answer", reasoning_content="reason " * 2000)
    request.messages.extend([answer, ChatMessage(role=Role.USER, content="continue")])
    with_reasoning = provider.estimate_input_tokens(request)
    wire = provider.serialized_messages(request)
    assert wire[-2]["reasoning_content"] == answer.reasoning_content
    answer.reasoning_content = None
    without_reasoning = provider.estimate_input_tokens(request)
    assert with_reasoning.tokens >= without_reasoning.tokens + 3000


@pytest.mark.asyncio
@pytest.mark.parametrize("actual", [500, None])
async def test_usage_records_input_error_without_changing_usage(monkeypatch, actual):
    monkeypatch.setattr(
        counting,
        "load_tokenizer",
        lambda _: SimpleNamespace(encode=lambda *a, **kw: SimpleNamespace(ids=range(100))),
    )
    import json

    raw_usage = {"prompt_tokens": actual, "completion_tokens": 4}
    chunk = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": raw_usage}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n")
        )
    ) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://api.deepseek.com", api_key="unused", client=client
        )
        events = [event async for event in provider.stream(tool_request())]
    metadata = next(e.provider_metadata for e in events if e.kind == ModelEventKind.USAGE)
    assert metadata["raw_usage"] == raw_usage
    assert metadata["input_token_estimate"]["tokens"] == 100
    assert metadata["input_token_error"] == (400 if actual is not None else None)
    assert metadata["input_budget_exceeded"] is (True if actual is not None else None)
