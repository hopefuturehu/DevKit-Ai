from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from bot.memory.models import MemoryStatus


class MemoryRetrievalDecision(StrEnum):
    NONE = "none"
    SUGGEST_SEARCH = "suggest_search"
    REQUIRE_SEARCH = "require_search"
    REQUIRE_EVIDENCE = "require_evidence"


@dataclass(frozen=True)
class MemoryRoutingResult:
    decision: MemoryRetrievalDecision
    query: str
    reasons: tuple[str, ...] = ()
    candidate_keys: tuple[str, ...] = ()


_BARE_CONTINUATIONS = {
    "继续",
    "接着",
    "接着来",
    "继续吧",
    "continue",
    "go on",
    "resume",
}

_EVIDENCE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"我.{0,8}(?:之前|此前|上次|曾经|是否|有没有).{0,12}(?:说过|发过|贴过|否认|同意|授权)",
        r"(?:我说过|我发过|我贴过).{0,10}(?:吗|没有|什么)",
        r"(?:谁说|是谁说|原话|逐字|历史证据|聊天证据|出处)",
        r"did\s+i\s+(?:previously\s+|ever\s+)?(?:say|post|paste|deny|agree|authorize)",
        r"have\s+i\s+(?:previously\s+|ever\s+)?(?:said|posted|pasted|denied|agreed|authorized)",
        r"(?:exact quote|verbatim|who said|transcript evidence)",
    )
)

_REQUIRED_SEARCH_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"按(?:上次|之前|此前|过去)",
        r"(?:沿用|恢复|接着|继续).{0,10}(?:上次|之前|此前|原来)",
        r"(?:还记得|记得).{0,8}(?:我|之前|上次|偏好|约定)",
        r"我的.{0,6}(?:偏好|习惯|约定)",
        r"(?:上次|之前|此前).{0,12}(?:决定|约定|流程|命令|配置|方案)",
        r"\b(?:use|follow|resume)\b.{0,12}\b(?:last time|previously|earlier)\b",
        r"\bmy\s+(?:saved\s+)?(?:preference|convention)s?\b",
    )
)


class MemoryRouter:
    """Deterministic gate for deciding whether automatic memory is relevant."""

    def __init__(
        self,
        store,
        *,
        min_confidence: float,
        min_score: float,
        min_term_coverage: float,
        max_candidates: int,
    ) -> None:
        self.store = store
        self.min_confidence = min_confidence
        self.min_score = min_score
        self.min_term_coverage = min_term_coverage
        self.max_candidates = max_candidates

    def route(self, prompt: str, *, has_prior_conversation: bool) -> MemoryRoutingResult:
        query = " ".join(prompt.split()).strip()[:2_000]
        if not query:
            return MemoryRoutingResult(
                decision=MemoryRetrievalDecision.NONE,
                query="",
                reasons=("empty_prompt",),
            )

        if any(pattern.search(query) for pattern in _EVIDENCE_PATTERNS):
            return MemoryRoutingResult(
                decision=MemoryRetrievalDecision.REQUIRE_EVIDENCE,
                query=query,
                reasons=("historical_attribution_requires_transcript_evidence",),
            )

        if any(pattern.search(query) for pattern in _REQUIRED_SEARCH_PATTERNS):
            return MemoryRoutingResult(
                decision=MemoryRetrievalDecision.REQUIRE_SEARCH,
                query=query,
                reasons=("explicit_cross_turn_dependency",),
            )

        if query.casefold() in _BARE_CONTINUATIONS:
            if has_prior_conversation:
                return MemoryRoutingResult(
                    decision=MemoryRetrievalDecision.NONE,
                    query=query,
                    reasons=("continuation_satisfied_by_session_transcript",),
                )
            return MemoryRoutingResult(
                decision=MemoryRetrievalDecision.REQUIRE_SEARCH,
                query=query,
                reasons=("continuation_without_session_context",),
            )

        hits = self.store.search_scored(query, limit=self.max_candidates)
        candidates = [
            hit
            for hit in hits
            if hit.record.origin == "auto"
            and hit.record.status == MemoryStatus.ACTIVE
            and hit.record.confidence >= self.min_confidence
            and hit.score >= self.min_score
            and hit.term_coverage >= self.min_term_coverage
        ]
        if candidates:
            return MemoryRoutingResult(
                decision=MemoryRetrievalDecision.SUGGEST_SEARCH,
                query=query,
                reasons=("relevant_active_automatic_memory",),
                candidate_keys=tuple(hit.record.key for hit in candidates),
            )
        return MemoryRoutingResult(
            decision=MemoryRetrievalDecision.NONE,
            query=query,
            reasons=("self_contained_or_no_relevant_memory",),
        )
