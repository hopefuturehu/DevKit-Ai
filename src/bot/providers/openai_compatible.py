from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import httpx

from bot.core.models import (
    InputTokenEstimate,
    ModelCapabilities,
    ModelEvent,
    ModelEventKind,
    ModelRequest,
)
from bot.providers.base import ModelProvider, ProviderError, ProviderErrorKind
from bot.providers.token_counting import MODEL as TOKENIZER_MODEL
from bot.providers.token_counting import DeepSeekInputCounter

_CONTEXT_ERROR_MARKERS = (
    "context length",
    "context_length",
    "context window",
    "maximum context",
    "max context",
    "too many tokens",
    "token limit",
)


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
            raise ProviderError(
                "model.base_url 未配置",
                kind=ProviderErrorKind.CONFIGURATION,
            )
        if not api_key:
            raise ProviderError(
                "模型 API Key 为空",
                kind=ProviderErrorKind.CONFIGURATION,
            )
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._input_counter = DeepSeekInputCounter()

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(
            text_generation=True,
            streaming=True,
            structured_tool_calling=True,
            named_tool_choice=not self._is_official_deepseek_endpoint(),
            usage_reporting=False,
        )

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _payload(self, request: ModelRequest) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        for index, message in enumerate(request.messages):
            try:
                messages.append(message.to_openai())
            except ValueError as exc:
                raise ProviderError(
                    f"模型请求中的第 {index} 条消息无效: {exc}",
                    kind=ProviderErrorKind.CONFIGURATION,
                ) from exc
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "stream": True,
            "temperature": request.temperature,
        }
        if request.tools:
            payload["tools"] = [tool.to_openai() for tool in request.tools]
            payload["tool_choice"] = request.tool_choice or "auto"
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        requires_non_thinking = self._requires_non_thinking_tool_chain(request)
        if requires_non_thinking and request.thinking == "enabled":
            raise ProviderError(
                "DeepSeek thinking mode 与命名 tool_choice 或缺少 reasoning_content "
                "的 Tool 链不兼容",
                kind=ProviderErrorKind.CONFIGURATION,
            )
        if requires_non_thinking:
            # The official DeepSeek endpoint rejects a named function choice in
            # thinking mode.  A function call created with thinking disabled also
            # has no reasoning_content, so the rest of that Tool chain must stay
            # non-thinking when it is replayed.
            payload["thinking"] = {"type": "disabled"}
        elif request.thinking is not None:
            payload["thinking"] = {"type": request.thinking}
        return payload

    def _requires_non_thinking_tool_chain(
        self,
        request: ModelRequest,
    ) -> bool:
        if not self._is_official_deepseek_endpoint():
            return False
        if isinstance(request.tool_choice, dict):
            return True
        return any(
            message.tool_calls and not message.reasoning_content for message in request.messages
        )

    def _is_official_deepseek_endpoint(self) -> bool:
        hostname = (urlsplit(self.base_url).hostname or "").lower()
        return hostname == "api.deepseek.com" or hostname.endswith(".deepseek.com")

    def estimate_input_tokens(self, request: ModelRequest) -> InputTokenEstimate | None:
        if not self._is_official_deepseek_endpoint() or request.model != TOKENIZER_MODEL:
            return super().estimate_input_tokens(request)
        return self._input_counter.estimate(request, self._payload(request))

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        payload = self._payload(request)
        estimate = (
            self._input_counter.estimate(request, payload)
            if self._is_official_deepseek_endpoint() and request.model == TOKENIZER_MODEL
            else None
        )
        owned_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout_seconds)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        saw_finish = False
        choice_diagnostics: dict[int, dict[str, Any]] = {}
        try:
            async with client.stream(
                "POST", self.endpoint, headers=headers, json=payload
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode(errors="replace")[:4000]
                    raise ProviderError(
                        f"模型 API 返回 HTTP {response.status_code}: "
                        f"{body or response.reason_phrase}",
                        kind=self._http_error_kind(response.status_code, body),
                        status_code=response.status_code,
                    )
                response_metadata = {
                    "provider": "openai_compatible",
                    "request_id": (
                        response.headers.get("x-request-id")
                        or response.headers.get("x-ds-request-id")
                    ),
                }
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
                        raise ProviderError(
                            f"无法解析模型 SSE 数据: {data[:500]}",
                            kind=ProviderErrorKind.PROTOCOL,
                        ) from exc

                    usage = chunk.get("usage")
                    if usage:
                        calibration = {}
                        if estimate is not None:
                            actual = usage.get("prompt_tokens")
                            valid_input = type(actual) is int and actual >= 0
                            calibration = {
                                "input_token_estimate": estimate.model_dump(),
                                "input_token_error": (
                                    actual - estimate.tokens if valid_input else None
                                ),
                                "input_budget_exceeded": (
                                    actual > estimate.budget_tokens if valid_input else None
                                ),
                            }
                        yield ModelEvent(
                            kind=ModelEventKind.USAGE,
                            input_tokens=usage.get("prompt_tokens"),
                            output_tokens=usage.get("completion_tokens"),
                            provider_metadata={
                                **response_metadata,
                                "response_id": chunk.get("id"),
                                "model": chunk.get("model"),
                                "raw_usage": usage,
                                **calibration,
                            },
                        )

                    choices = chunk.get("choices") or []
                    for choice in choices:
                        choice_index = int(choice.get("index", 0) or 0)
                        diagnostics = choice_diagnostics.setdefault(
                            choice_index,
                            {
                                "choice_index": choice_index,
                                "chunk_count": 0,
                                "content_chars": 0,
                                "reasoning_chars": 0,
                                "tool_call_chunks": 0,
                                "observed_delta_fields": set(),
                                "unhandled_delta_samples": [],
                            },
                        )
                        diagnostics["chunk_count"] += 1
                        delta = choice.get("delta") or {}
                        diagnostics["observed_delta_fields"].update(delta)
                        known_delta_fields = {
                            "role",
                            "content",
                            "reasoning_content",
                            "tool_calls",
                        }
                        for field in sorted(set(delta) - known_delta_fields):
                            value = delta.get(field)
                            if value is None or len(diagnostics["unhandled_delta_samples"]) >= 8:
                                continue
                            diagnostics["unhandled_delta_samples"].append(
                                {"field": field, "value": str(value)[:2_000]}
                            )
                        reasoning_content = delta.get("reasoning_content")
                        if reasoning_content:
                            reasoning_text = str(reasoning_content)
                            diagnostics["reasoning_chars"] += len(reasoning_text)
                            yield ModelEvent(
                                kind=ModelEventKind.REASONING_DELTA,
                                text=reasoning_text,
                                provider_metadata={"choice_index": choice_index},
                            )
                        content = delta.get("content")
                        if content:
                            content_text = str(content)
                            diagnostics["content_chars"] += len(content_text)
                            yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text=content_text)
                        tool_deltas = delta.get("tool_calls") or []
                        diagnostics["tool_call_chunks"] += len(tool_deltas)
                        for tool_delta in tool_deltas:
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
                            finish_metadata = {
                                **response_metadata,
                                "response_id": chunk.get("id"),
                                "model": chunk.get("model"),
                                "system_fingerprint": chunk.get("system_fingerprint"),
                                **diagnostics,
                                "observed_delta_fields": sorted(
                                    diagnostics["observed_delta_fields"]
                                ),
                                "finish_choice_fields": sorted(choice),
                            }
                            yield ModelEvent(
                                kind=ModelEventKind.FINISH,
                                finish_reason=str(finish_reason),
                                provider_metadata=finish_metadata,
                            )
            if not saw_finish:
                serializable_diagnostics = []
                for diagnostics in choice_diagnostics.values():
                    serializable_diagnostics.append(
                        {
                            **diagnostics,
                            "observed_delta_fields": sorted(diagnostics["observed_delta_fields"]),
                        }
                    )
                yield ModelEvent(
                    kind=ModelEventKind.FINISH,
                    finish_reason="eof",
                    provider_metadata={
                        "provider": "openai_compatible",
                        "choices": serializable_diagnostics,
                    },
                )
        except ProviderError:
            raise
        except httpx.TimeoutException as exc:
            detail = str(exc).strip() or type(exc).__name__
            raise ProviderError(
                f"模型 API 请求超时: {detail}",
                kind=ProviderErrorKind.TIMEOUT,
            ) from exc
        except httpx.TransportError as exc:
            detail = str(exc).strip() or type(exc).__name__
            raise ProviderError(
                f"模型 API 请求失败: {detail}",
                kind=ProviderErrorKind.TRANSPORT,
            ) from exc
        except httpx.HTTPError as exc:
            detail = str(exc).strip() or type(exc).__name__
            raise ProviderError(
                f"模型 API 请求失败: {detail}",
                kind=ProviderErrorKind.PROTOCOL,
            ) from exc
        finally:
            if owned_client:
                await client.aclose()

    @staticmethod
    def _http_error_kind(status_code: int, body: str) -> ProviderErrorKind:
        normalized = body.casefold()
        if status_code in {401, 403}:
            return ProviderErrorKind.AUTHENTICATION
        if status_code == 402:
            return ProviderErrorKind.PAYMENT
        if status_code == 429:
            return ProviderErrorKind.RATE_LIMIT
        if any(marker in normalized for marker in _CONTEXT_ERROR_MARKERS):
            return ProviderErrorKind.CONTEXT_LENGTH
        if status_code >= 500:
            return ProviderErrorKind.SERVER
        return ProviderErrorKind.PROTOCOL
