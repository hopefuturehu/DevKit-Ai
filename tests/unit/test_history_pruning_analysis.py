"""Check the measurement's safety conditions, not production Skill behavior."""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from bot.core.context import PositionedMessage
from bot.core.models import ChatMessage, Role, ToolCall

_SCRIPT = Path(__file__).parents[2] / "scripts" / "analyze_history_pruning.py"
_SPEC = importlib.util.spec_from_file_location("history_pruning_analysis_test", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
EST, Record = _MODULE.EST, _MODULE.Record
protected_tail, prune = _MODULE.protected_tail, _MODULE.prune


def pair(position, *, body=None, raw=None, path="a.py", run="r", **result_fields):
    body = "source line\n" * 600 if body is None else body
    raw = body.encode() if raw is None else raw
    call = ToolCall(id=f"call-{position}", name="read_file", arguments={"path": path})
    owner = Record(
        PositionedMessage(position, ChatMessage(role=Role.ASSISTANT, tool_calls=[call]), run),
        "2026-09-08",
    )
    result = {"success": True, "status": "completed", "metadata": {"path": path}, **result_fields}
    answer = Record(
        PositionedMessage(
            position + 1,
            ChatMessage(role=Role.TOOL, name="read_file", tool_call_id=call.id, content=body),
            run,
        ),
        "2026-09-08",
        {"tool_name": "read_file", "arguments_json": json.dumps(call.arguments)},
        result,
        "blob:" + hashlib.sha256(raw).hexdigest(),
        raw,
    )
    return [owner, answer]


def test_exact_duplicate_keeps_representative_through_later_tiers():
    records = pair(1) + pair(3)
    replacements, savings, audit = prune(records, protected=set(), old_success=True)
    assert set(replacements) == {2}
    assert audit[0]["retained"] == 4
    assert savings["duplicate"] > 0
    assert savings["old_success"] == 0
    assert records[0].entry.message.tool_calls[0].arguments == {"path": "a.py"}


@pytest.mark.parametrize("change", [{"path": "b.py"}, {"run": "other"}, {"raw": b"different"}])
def test_equal_previews_do_not_prove_same_source_or_full_body(change):
    records = pair(1) + pair(3, **change)
    replacements, _, _ = prune(records, protected=set(), old_success=False)
    assert replacements == {}


@pytest.mark.parametrize(
    "fields",
    [{"success": False, "status": "failed"}, {"status": "running"}, {"truncated": True}],
)
def test_incomplete_or_failed_results_are_not_exact_duplicates(fields):
    replacements, _, _ = prune(
        pair(1, **fields) + pair(3, **fields), protected=set(), old_success=False
    )
    assert replacements == {}


def test_missing_blob_and_orphan_result_cannot_be_pruned():
    records = pair(1)
    records[1].raw = None
    assert prune(records, protected=set(), old_success=True)[0] == {}
    assert prune(pair(1)[1:], protected=set(), old_success=True)[0] == {}


def test_savings_are_bounded_by_visible_preview_not_blob_size():
    records = pair(1, body="visible\n" * 600, raw=b"full output\n" * 100_000)
    replacements, savings, _ = prune(records, protected=set(), old_success=True)
    assert replacements
    assert 0 < sum(savings.values()) < EST.text(records[1].entry.message.content)


def test_same_blob_and_preview_do_not_supply_a_full_visible_representative():
    raw = b"full output\n" * 100_000
    records = pair(1, raw=raw) + pair(3, raw=raw)
    assert prune(records, protected=set(), old_success=False)[0] == {}


def test_an_older_full_visible_copy_can_represent_a_later_partial_copy():
    body = "source line\n" * 1000
    records = pair(1, body=body) + pair(3, body=body[:6000], raw=body.encode())
    replacements, _, audit = prune(records, protected=set(), old_success=False)
    assert set(replacements) == {4}
    assert audit[0]["retained"] == 2


def test_recent_window_protects_whole_tool_group():
    records = pair(1) + pair(3, path="b.py")
    protected = protected_tail(records, 1000)
    assert protected == {3, 4}
    replacements, _, _ = prune(records, protected=protected, old_success=True)
    assert set(replacements) == {2}


def test_receipt_preserves_upstream_truncation_status():
    replacements, _, _ = prune(pair(1, truncated=True), protected=set(), old_success=True)
    assert json.loads(replacements[2])["source_truncated"] is True
