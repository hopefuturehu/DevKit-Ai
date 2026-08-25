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
