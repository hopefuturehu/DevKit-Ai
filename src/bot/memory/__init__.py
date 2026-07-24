from bot.memory.models import (
    ConsolidationPayload,
    ConsolidationResult,
    ConsolidationStatus,
    EpisodeStatus,
    EpisodeSummary,
    MemoryCandidate,
    MemoryCardStatus,
    MemoryKind,
    MemoryOperation,
    MemoryScope,
    VerifiedMemoryCandidate,
)

__all__ = [
    "ConsolidationPayload",
    "ConsolidationResult",
    "ConsolidationStatus",
    "EpisodeStatus",
    "EpisodeSummary",
    "MemoryCandidate",
    "MemoryCardStatus",
    "MemoryConsolidator",
    "MemoryKind",
    "MemoryOperation",
    "MemoryScope",
    "VerifiedMemoryCandidate",
]


def __getattr__(name: str):
    if name == "MemoryConsolidator":
        from bot.memory.service import MemoryConsolidator

        return MemoryConsolidator
    raise AttributeError(name)
