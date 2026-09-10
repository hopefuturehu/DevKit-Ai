from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from enum import StrEnum

from bot.core.models import InputTokenEstimate, ModelCapabilities, ModelEvent, ModelRequest


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

    def estimate_input_tokens(self, request: ModelRequest) -> InputTokenEstimate | None:
        try:
            exact = self.count_tokens(request)
        except Exception:
            return None
        if exact is None:
            return None
        return InputTokenEstimate(tokens=exact, budget_tokens=exact, source="provider_counter")


def estimate_input_tokens(provider: object, request: ModelRequest) -> InputTokenEstimate | None:
    """Preserve compatibility with duck-typed providers lacking the optional API."""
    estimate = getattr(provider, "estimate_input_tokens", None)
    if estimate is not None:
        return estimate(request)
    counter = getattr(provider, "count_tokens", None)
    try:
        exact = counter(request) if counter is not None else None
    except Exception:
        return None
    if exact is None:
        return None
    return InputTokenEstimate(tokens=exact, budget_tokens=exact, source="provider_counter")
