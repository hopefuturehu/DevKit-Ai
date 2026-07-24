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
        score += min(5.0, float(card.get("access_count") or 0) * 0.25)
        score += overlap * 2 + coverage * 8
        ranked.append((score, str(card.get("updated_at", "")), card))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in ranked[: max(0, limit)]]


def rank_episode_summaries(
    episodes: list[dict[str, Any]],
    query: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    query_terms = _terms(query)
    ranked: list[tuple[float, int, dict[str, Any]]] = []
    newest_end = max((int(item.get("end_position") or 0) for item in episodes), default=0)
    for episode in episodes:
        searchable = " ".join(
            [
                str(episode.get("title") or ""),
                str(episode.get("objective") or ""),
                str(episode.get("summary") or ""),
                " ".join(str(item) for item in episode.get("keywords") or []),
                " ".join(str(item) for item in episode.get("topics") or []),
            ]
        )
        terms = _terms(searchable)
        overlap = len(query_terms & terms)
        coverage = overlap / max(1, len(query_terms))
        end_position = int(episode.get("end_position") or 0)
        recency = end_position / max(1, newest_end)
        depth_bonus = 1.0 if episode.get("depth") == "deep" else 0.0
        score = overlap * 2 + coverage * 10 + recency * 4 + depth_bonus
        ranked.append((score, end_position, episode))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in ranked[: max(0, limit)]]
