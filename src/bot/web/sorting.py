"""Shared ordering rules for analysed runs.

Both the query layer (which sorts the full filtered set before paginating) and
the HTTP layer (which re-sorts a page for the compare/export paths) must agree
on ordering, otherwise the list, the summary and the export can disagree about
which run is "slowest". Keeping one implementation here removes that risk.
"""

from __future__ import annotations

from typing import Any

# Sort keys accepted by the analysis API, mapped to the analysed field name.
SORT_KEYS = {
    "net": "net_seconds",
    "total": "total_seconds",
    "approval": "approval_seconds",
    "started": "started_at",
    "events": "event_count",
}


def sort_analyses(
    runs: list[dict[str, Any]], *, sort: str = "started", order: str = "desc"
) -> list[dict[str, Any]]:
    """Sort analysed runs, keeping unknown durations last in both directions.

    Unknown values must not masquerade as the fastest or slowest run, so they are
    always pushed to the end of the list. Ties fall back to ``run_id`` so the
    order is stable across pages: without a deterministic tie-break, two runs
    with the same net duration could swap places between requests and appear
    twice (or not at all) while paging.
    """
    key = SORT_KEYS.get(sort)
    if key is None:
        raise ValueError(f"不支持的排序字段: {sort}")
    reverse = order == "desc"

    def sort_key(item: dict[str, Any]):
        value = item.get(key)
        if value is None:
            # Unknown always sorts last: the leading flag dominates comparison.
            return (1, 0, "", item.get("run_id") or "")
        if isinstance(value, str):
            return (0, 0, value, item.get("run_id") or "")
        return (0, value, "", item.get("run_id") or "")

    ordered = sorted(runs, key=sort_key, reverse=reverse)
    if reverse:
        # ``reverse`` also flips the unknown flag, so re-partition explicitly.
        known = [item for item in ordered if item.get(key) is not None]
        unknown = [item for item in ordered if item.get(key) is None]
        return known + unknown
    return ordered
