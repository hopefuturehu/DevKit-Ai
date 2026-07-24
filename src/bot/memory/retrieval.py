from __future__ import annotations

import re
from typing import Any

_ASCII_TERM = re.compile(r"[a-z0-9_./:-]+")
_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")

_KIND_WEIGHT = {
    "constraint": 12.0,
    "preference": 10.0,
    "task": 8.0,
    "project": 7.0,
    "decision": 7.0,
    "verification": 6.0,
    "artifact": 5.0,
    "error": 5.0,
    "lesson": 4.0,
}


def _terms(value: str) -> set[str]:
    lowered = value.casefold()
    terms = set(_ASCII_TERM.findall(lowered))
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            terms.add(run)
            continue
        terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return terms


def rank_memory_cards(
    cards: list[dict[str, Any]],
    query: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    query_terms = _terms(query)
    ranked: list[tuple[float, str, dict[str, Any]]] = []
    for card in cards:
        content_terms = _terms(str(card.get("content", "")))
        overlap = len(query_terms & content_terms)
        coverage = overlap / max(1, len(query_terms))
        score = _KIND_WEIGHT.get(str(card.get("kind", "")), 1.0)
        score += float(card.get("confidence") or 0) * 5
        if card.get("scope") == "session":
            score += 3
        score += overlap * 2 + coverage * 8
        ranked.append((score, str(card.get("updated_at", "")), card))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in ranked[: max(0, limit)]]
