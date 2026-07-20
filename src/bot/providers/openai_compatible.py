from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from bot.core.models import ModelCapabilities, ModelEvent, ModelEventKind, ModelRequest
from bot.providers.base import ModelProvider, ProviderError


class OpenAICompatibleProvider(ModelProvider):
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 120,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url.strip():
            raise ProviderError("model.base_url 未配置")
        if not api_key:
            raise ProviderError("模型 API Key 为空")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._client = client

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(
            text_generation=True,
            streaming=True,
            structured_tool_calling=True,
            usage_reporting=False,
        )

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _payload(self, request: ModelRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [message.to_openai() for message in request.messages],
            "stream": True,
            "temperature": request.temperature,
        }
        if request.tools:
            payload["tools"] = [tool.to_openai() for tool in request.tools]
            payload["tool_choice"] = "auto"
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        return payload

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        owned_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout_seconds)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        saw_finish = False
        try:
            async with client.stream(
                "POST", self.endpoint, headers=headers, json=self._payload(request)
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode(errors="replace")[:4000]
                    raise ProviderError(
                        f"模型 API 返回 HTTP {response.status_code}: "
                        f"{body or response.reason_phrase}"
                    )
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line.removeprefix("data:").strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise ProviderError(f"无法解析模型 SSE 数据: {data[:500]}") from exc

                    usage = chunk.get("usage")
                    if usage:
                        yield ModelEvent(
                            kind=ModelEventKind.USAGE,
                            input_tokens=usage.get("prompt_tokens"),
                            output_tokens=usage.get("completion_tokens"),
                            provider_metadata={"raw_usage": usage},
                        )

                    choices = chunk.get("choices") or []
                    for choice in choices:
                        delta = choice.get("delta") or {}
                        content = delta.get("content")
                        if content:
                            yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text=content)
                        for tool_delta in delta.get("tool_calls") or []:
                            function = tool_delta.get("function") or {}
                            yield ModelEvent(
                                kind=ModelEventKind.TOOL_CALL_DELTA,
                                tool_index=tool_delta.get("index", 0),
                                tool_call_id=tool_delta.get("id"),
                                tool_name=function.get("name"),
                                arguments_delta=function.get("arguments"),
                            )
                        finish_reason = choice.get("finish_reason")
                        if finish_reason is not None:
                            saw_finish = True
                            yield ModelEvent(
                                kind=ModelEventKind.FINISH,
                                finish_reason=str(finish_reason),
                                provider_metadata={"choice_index": choice.get("index", 0)},
                            )
            if not saw_finish:
                yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="eof")
        except httpx.HTTPError as exc:
            detail = str(exc).strip() or type(exc).__name__
            raise ProviderError(f"模型 API 请求失败: {detail}") from exc
        finally:
            if owned_client:
                await client.aclose()
