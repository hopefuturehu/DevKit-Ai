import json

import httpx
import pytest

from bot.core.models import ChatMessage, ModelEventKind, ModelRequest, Role, ToolDefinition
from bot.providers import OpenAICompatibleProvider, ProviderError


@pytest.mark.asyncio
async def test_openai_compatible_provider_streams_text_tool_calls_and_usage() -> None:
    chunks = [
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
        ModelEventKind.TEXT_DELTA,
        ModelEventKind.TOOL_CALL_DELTA,
        ModelEventKind.USAGE,
        ModelEventKind.TOOL_CALL_DELTA,
        ModelEventKind.FINISH,
    ]
    assert "".join(event.arguments_delta or "" for event in events) == '{"path":"a"}'


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
