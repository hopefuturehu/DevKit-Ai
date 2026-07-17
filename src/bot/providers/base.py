from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from bot.core.models import ModelCapabilities, ModelEvent, ModelRequest


class ProviderError(RuntimeError):
    pass


class ModelProvider(ABC):
    @abstractmethod
    def capabilities(self, model: str) -> ModelCapabilities:
        raise NotImplementedError

    @abstractmethod
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        raise NotImplementedError

    def count_tokens(self, request: ModelRequest) -> int | None:
        return None
