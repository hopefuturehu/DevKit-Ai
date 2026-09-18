import json

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
from bot.providers import OpenAICompatibleProvider, ProviderError, ProviderErrorKind


@pytest.mark.asyncio
async def test_openai_compatible_provider_streams_text_tool_calls_and_usage() -> None:
    chunks = [
        {"choices": [{"index": 0, "delta": {"reasoning_content": "thinking"}}]},
        {"choices": [{"index": 0, "delta": {"content": "hello"}}]},
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "function": {"name": "read_file", "arguments": '{"path":'},
                            }
                        ]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"a"}'}}]},
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4},
        },
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload["tools"][0]["function"]["name"] == "read_file"
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1", api_key="secret", client=client
    )
    request = ModelRequest(
        model="test",
        messages=[ChatMessage(role=Role.USER, content="hello")],
        tools=[
            ToolDefinition(name="read_file", description="read", input_schema={"type": "object"})
        ],
    )

    events = [event async for event in provider.stream(request)]
    await client.aclose()

    assert [event.kind for event in events] == [
        ModelEventKind.REASONING_DELTA,
        ModelEventKind.TEXT_DELTA,
        ModelEventKind.TOOL_CALL_DELTA,
        ModelEventKind.USAGE,
        ModelEventKind.TOOL_CALL_DELTA,
        ModelEventKind.FINISH,
    ]
    assert (
        "".join(
            event.text or "" for event in events if event.kind == ModelEventKind.REASONING_DELTA
        )
        == "thinking"
    )
    assert "".join(event.arguments_delta or "" for event in events) == '{"path":"a"}'
    finish = next(event for event in events if event.kind == ModelEventKind.FINISH)
    assert finish.provider_metadata["reasoning_chars"] == len("thinking")
    assert finish.provider_metadata["content_chars"] == len("hello")
    assert finish.provider_metadata["observed_delta_fields"] == [
        "content",
        "reasoning_content",
        "tool_calls",
    ]


def test_provider_round_trips_reasoning_for_tool_calls_and_rejects_empty_assistant() -> None:
    provider = OpenAICompatibleProvider(base_url="https://example.test/v1", api_key="secret")
    tool_message = ChatMessage(
        role=Role.ASSISTANT,
        reasoning_content="full reasoning",
        tool_calls=[ToolCall(id="call-1", name="read_file", arguments={"path": "a"})],
    )

    payload = provider._payload(ModelRequest(model="test", messages=[tool_message]))

    assert payload["messages"][0]["reasoning_content"] == "full reasoning"
    assert payload["messages"][0]["tool_calls"][0]["function"]["name"] == "read_file"
    assert "thinking" not in payload
    non_thinking = provider._payload(
        ModelRequest(
            model="test",
            messages=[ChatMessage(role=Role.USER, content="summarize")],
            thinking="disabled",
        )
    )
    assert non_thinking["thinking"] == {"type": "disabled"}
    with pytest.raises(ProviderError, match="第 0 条消息无效"):
        provider._payload(ModelRequest(model="test", messages=[ChatMessage(role=Role.ASSISTANT)]))


@pytest.mark.parametrize("thinking", ["enabled", "disabled", None])
def test_provider_disables_tools_without_removing_their_schema(thinking) -> None:
    provider = OpenAICompatibleProvider(base_url="https://api.deepseek.com", api_key="test")
    request = ModelRequest(
        model="deepseek-v4-pro",
        messages=[ChatMessage(role=Role.USER, content="summarize")],
        tools=[ToolDefinition(name="read_file", description="Read", input_schema={})],
        thinking=thinking,
    )
    original = provider._payload(request)
    request.tool_choice = "none"
    finalizer = provider._payload(request)
    assert original["tool_choice"] == "auto"
    assert finalizer["tool_choice"] == "none"
    assert {k: v for k, v in finalizer.items() if k != "tool_choice"} == {
        k: v for k, v in original.items() if k != "tool_choice"
    }


def test_provider_preserves_named_tool_choice() -> None:
    provider = OpenAICompatibleProvider(base_url="https://example.test/v1", api_key="secret")
    choice = {"type": "function", "function": {"name": "search_memory"}}

    payload = provider._payload(
        ModelRequest(
            model="test",
            messages=[ChatMessage(role=Role.USER, content="按上次的方案继续")],
            tools=[
                ToolDefinition(
                    name="search_memory",
                    description="search",
                    input_schema={"type": "object"},
                )
            ],
            tool_choice=choice,
        )
    )

    assert payload["tool_choice"] == choice
    assert "thinking" not in payload


def test_provider_allows_named_deepseek_tool_choice_when_explicitly_non_thinking() -> None:
    provider = OpenAICompatibleProvider(
        base_url="https://api.deepseek.com/v1",
        api_key="secret",
    )
    choice = {"type": "function", "function": {"name": "search_memory"}}

    payload = provider._payload(
        ModelRequest(
            model="deepseek-test",
            messages=[ChatMessage(role=Role.USER, content="按上次的方案继续")],
            tools=[
                ToolDefinition(
                    name="search_memory",
                    description="search",
                    input_schema={"type": "object"},
                )
            ],
            tool_choice=choice,
            thinking="disabled",
        )
    )

    assert payload["tool_choice"] == choice
    assert payload["thinking"] == {"type": "disabled"}
    assert provider.capabilities("deepseek-test").named_tool_choice is False


@pytest.mark.parametrize("thinking", [None, "enabled", "disabled"])
@pytest.mark.parametrize("reasoning", [None, "", "original reasoning"])
def test_provider_preserves_deepseek_mode_for_followup_tool_chain(thinking, reasoning) -> None:
    provider = OpenAICompatibleProvider(
        base_url="https://api.deepseek.com/v1",
        api_key="secret",
    )

    request = ModelRequest(
        model="deepseek-test",
        thinking=thinking,
        messages=[
            ChatMessage(role=Role.USER, content="按上次的方案继续"),
            ChatMessage(
                role=Role.ASSISTANT,
                reasoning_content=reasoning,
                tool_calls=[
                    ToolCall(
                        id="search-1",
                        name="search_memory",
                        arguments={"query": "上次方案"},
                    )
                ],
            ),
            ChatMessage(
                role=Role.TOOL,
                name="search_memory",
                tool_call_id="search-1",
                content="result",
            ),
        ],
        tools=[
            ToolDefinition(
                name="search_memory",
                description="search",
                input_schema={"type": "object"},
            )
        ],
    )
    original = request.model_dump_json()
    payload = provider._payload(request)

    assert payload["tool_choice"] == "auto"
    assert payload.get("thinking") == ({"type": thinking} if thinking else None)
    if thinking == "disabled":
        assert "reasoning_content" not in payload["messages"][1]
    else:
        assert payload["messages"][1]["reasoning_content"] == (reasoning or "")
    assert request.model_dump_json() == original


@pytest.mark.parametrize("thinking", [None, "enabled"])
def test_provider_rejects_deepseek_thinking_with_named_tool_choice(thinking) -> None:
    provider = OpenAICompatibleProvider(
        base_url="https://api.deepseek.com/v1",
        api_key="secret",
    )

    with pytest.raises(ProviderError, match="thinking mode 与命名 tool_choice"):
        provider._payload(
            ModelRequest(
                model="deepseek-test",
                messages=[ChatMessage(role=Role.USER, content="x")],
                tools=[
                    ToolDefinition(
                        name="search_memory",
                        description="search",
                        input_schema={"type": "object"},
                    )
                ],
                tool_choice={"type": "function", "function": {"name": "search_memory"}},
                thinking=thinking,
            )
        )


@pytest.mark.parametrize("thinking", [None, "enabled", "disabled"])
def test_deepseek_replays_plain_answer_and_checkpoint_reasoning_without_mutating_history(thinking):
    provider = OpenAICompatibleProvider(base_url="https://api.deepseek.com", api_key="unused")
    request = ModelRequest(
        model="deepseek-test",
        thinking=thinking,
        messages=[
            ChatMessage(role=Role.USER, content="first task"),
            ChatMessage(role=Role.ASSISTANT, content="done", reasoning_content="real reasoning"),
            ChatMessage(role=Role.USER, content="continue"),
            ChatMessage(role=Role.ASSISTANT, name="context_compaction", content="derived summary"),
        ],
        tools=[ToolDefinition(name="read", description="read", input_schema={})],
        tool_choice="none",  # Prefix compaction/finalization still carry tool schemas.
    )
    original = request.model_dump_json()
    payload = provider._payload(request)
    assert payload["tool_choice"] == "none"
    assert payload.get("thinking") == ({"type": thinking} if thinking else None)
    if thinking == "disabled":
        assert all("reasoning_content" not in m for m in payload["messages"])
    else:
        assert payload["messages"][1]["reasoning_content"] == "real reasoning"
        assert payload["messages"][3]["reasoning_content"] == ""
    assert request.model_dump_json() == original
    request.tools = []
    assert all("reasoning_content" not in m for m in provider._payload(request)["messages"])


def test_reasoning_placeholders_are_scoped_to_official_deepseek():
    provider = OpenAICompatibleProvider(base_url="https://example.test", api_key="unused")
    request = ModelRequest(
        model="deepseek-test",
        messages=[
            ChatMessage(
                role=Role.ASSISTANT, tool_calls=[ToolCall(id="c1", name="read", arguments={})]
            ),
        ],
        tools=[ToolDefinition(name="read", description="read", input_schema={})],
    )
    payload = provider._payload(request)
    assert "reasoning_content" not in payload["messages"][0]
    assert "thinking" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "deltas,state",
    [
        ([{}, {}], "absent"),
        ([{"reasoning_content": None}], "null"),
        ([{"reasoning_content": ""}, {"reasoning_content": None}], "empty"),
        ([{"reasoning_content": "reason"}, {"reasoning_content": ""}], "nonempty"),
    ],
)
async def test_provider_reports_reasoning_field_state_and_sent_mode(deltas, state):
    chunks = [{"choices": [{"index": 0, "delta": delta}]} for delta in deltas]
    chunks.append(
        {"choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}]}
    )
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))
    ) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://api.deepseek.com", api_key="unused", client=client
        )
        request = ModelRequest(
            model="test",
            messages=[
                ChatMessage(role=Role.ASSISTANT, name="context_compaction", content="summary")
            ],
            tools=[ToolDefinition(name="read", description="read", input_schema={})],
        )
        events = [event async for event in provider.stream(request)]
    metadata = next(e.provider_metadata for e in events if e.kind == ModelEventKind.FINISH)
    assert metadata["reasoning_content_state"] == state
    assert metadata["requested_thinking"] == "provider_default"
    assert metadata["sent_thinking"] == "provider_default"
    assert metadata["reasoning_placeholder_indices"] == [0]


@pytest.mark.asyncio
async def test_provider_surfaces_http_error_without_authorization_value() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "denied"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1", api_key="do-not-leak", client=client
    )
    request = ModelRequest(model="test", messages=[ChatMessage(role=Role.USER, content="hello")])
    with pytest.raises(ProviderError) as captured:
        _ = [event async for event in provider.stream(request)]
    await client.aclose()
    assert "do-not-leak" not in str(captured.value)
    assert captured.value.kind == ProviderErrorKind.AUTHENTICATION
    assert captured.value.status_code == 401
    assert captured.value.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body", "expected_kind", "retryable"),
    [
        (402, '{"error":"payment required"}', ProviderErrorKind.PAYMENT, False),
        (429, '{"error":"slow down"}', ProviderErrorKind.RATE_LIMIT, True),
        (500, '{"error":"upstream"}', ProviderErrorKind.SERVER, True),
        (
            400,
            '{"error":"maximum context length exceeded"}',
            ProviderErrorKind.CONTEXT_LENGTH,
            False,
        ),
    ],
)
async def test_provider_classifies_http_failures(
    status_code: int,
    body: str,
    expected_kind: ProviderErrorKind,
    retryable: bool,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1", api_key="secret", client=client
    )
    request = ModelRequest(model="test", messages=[ChatMessage(role=Role.USER, content="hello")])

    with pytest.raises(ProviderError) as captured:
        _ = [event async for event in provider.stream(request)]
    await client.aclose()

    assert captured.value.kind == expected_kind
    assert captured.value.status_code == status_code
    assert captured.value.retryable is retryable


@pytest.mark.asyncio
async def test_provider_names_http_error_when_exception_message_is_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ProxyError("")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1", api_key="secret", client=client
    )
    request = ModelRequest(model="test", messages=[ChatMessage(role=Role.USER, content="hello")])

    with pytest.raises(ProviderError, match="ProxyError"):
        _ = [event async for event in provider.stream(request)]
    await client.aclose()
