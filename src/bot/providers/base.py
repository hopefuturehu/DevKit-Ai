from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from enum import StrEnum

from bot.core.models import ModelCapabilities, ModelEvent, ModelRequest


class ProviderErrorKind(StrEnum):
    UNKNOWN = "unknown"
    CONFIGURATION = "configuration"
    AUTHENTICATION = "authentication"
    PAYMENT = "payment"
    RATE_LIMIT = "rate_limit"
    CONTEXT_LENGTH = "context_length"
    SERVER = "server"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    PROTOCOL = "protocol"


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        kind: ProviderErrorKind = ProviderErrorKind.UNKNOWN,
        status_code: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.retryable = (
            retryable
            if retryable is not None
            else kind
            in {
                ProviderErrorKind.RATE_LIMIT,
                ProviderErrorKind.SERVER,
                ProviderErrorKind.TIMEOUT,
                ProviderErrorKind.TRANSPORT,
            }
        )


class ModelProvider(ABC):
    @abstractmethod
    def capabilities(self, model: str) -> ModelCapabilities:
        raise NotImplementedError

    @abstractmethod
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        raise NotImplementedError

    def count_tokens(self, request: ModelRequest) -> int | None:
        return None
