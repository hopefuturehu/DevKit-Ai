"""Audit accounting tests; these synthetic records are not live quality evidence."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from bot.core.models import ChatMessage, ModelRequest

SPEC = importlib.util.spec_from_file_location(
    "handoff_audit", Path(__file__).resolve().parents[2] / "scripts/analyze_handoff_l0_l1.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture
def campaign(tmp_path):
    message = ChatMessage(role="user", content="frozen evidence")
    payload = message.model_dump(mode="json")
    order = [{"checkpoint": "case", "strategy": s, "repeat": 1} for s in ("CURRENT", "A", "D")]
    write_json(
        tmp_path / "manifest.json",
        {
            "order": order,
            "source_hashes": {},
            "checkpoints": [{"name": "case"}],
        },
    )
    write_json(tmp_path / "l0.json", {"source_hashes": {}})
    write_json(tmp_path / "fixtures/case.json", {"messages": [payload]})
    rows = []

    def request(folder, identity, request_id, tokens, *, logged=True):
        path = folder / "requests" / f"{request_id}.request.json"
        model_request = ModelRequest(model="test", messages=[message])
        write_json(path, model_request.model_dump(mode="json"))
        if logged:
            rows.append(
                {
                    **identity,
                    "request_id": request_id,
                    "phase": "continuation",
                    "logical_turn": 1,
                    "request_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "usage_complete": True,
                    "raw_usage": {
                        "prompt_tokens": tokens,
                        "prompt_cache_hit_tokens": 0,
                        "prompt_cache_miss_tokens": tokens,
                        "completion_tokens": 0,
                    },
                    "normalized_peak_cost_usd": tokens * 0.44 / 1e6,
                    "cost_usd_at_start_rate": tokens * 0.22 / 1e6,
                    "budget_charge_usd": tokens * 0.44 / 1e6,
                    "latency_seconds": 1,
                    "error": None,
                    "finish_reason": "stop",
                    "price_boundary_crossed": False,
                }
            )

    for identity in order:
        folder = tmp_path / "segments" / f"case-{identity['strategy']}-1"
        write_json(folder / "started.json", identity)
        request(folder, identity, identity["strategy"], 100)
        with sqlite3.connect(folder / "state.db") as db:
            db.execute("CREATE TABLE messages(position INTEGER, message_json TEXT)")
            db.execute("INSERT INTO messages VALUES(1,?)", (json.dumps(payload),))
            db.execute("CREATE TABLE context_compactions(status TEXT, summary_text TEXT)")
        write_json(
            folder / "result.json",
            {
                **identity,
                "kind": "unit-fixture",
                "published": False,
                "passed_probes": 8,
                "strict_format_probes": 8,
                "all_probes_passed": True,
                "first_tool_roundtrip_ok": False,
                "publication": {},
                "error": None,
                "probes": [
                    {"passed": True, "reads": [], "score": {"format_ok": True}} for _ in range(8)
                ],
            },
        )
    archived = tmp_path / "interrupted_attempts/case-CURRENT-1-old"
    write_json(archived / "started.json", order[0])
    request(archived, order[0], "old", 900)
    request(archived, order[0], "unknown", 900, logged=False)
    (tmp_path / "requests.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    return tmp_path


def test_archive_cost_is_retained_without_contaminating_same_identity_rerun(campaign):
    report = MODULE.audit(campaign)
    assert report["groups"]["CURRENT"]["all"]["input_tokens"] == 100
    assert report["groups"]["CURRENT"]["all_attempts"]["input_tokens"] == 1000
    assert report["balanced_comparison"]["CURRENT"]["all"]["input_tokens"] == 100
    assert report["campaign_metrics"]["input_tokens"] == 1200
    assert report["interrupted_attempt_metrics"]["input_tokens"] == 900
    assert report["interrupted_attempt_count"] == 1
    assert len(report["unlogged_requests"]) == 1
    assert report["unlogged_requests"][0]["archived"]
    assert report["unlogged_requests"][0]["conservative_reserve_usd"] > 0


def test_missing_segment_cannot_be_labeled_complete(campaign):
    path = campaign / "segments/case-A-1/result.json"
    path.rename(path.with_suffix(".pending"))
    with pytest.raises(AssertionError, match="Incomplete campaign"):
        MODULE.audit(campaign)


def test_changed_request_is_rejected(campaign):
    path = campaign / "segments/case-A-1/requests/A.request.json"
    path.write_text(path.read_text() + " ")
    with pytest.raises(AssertionError):
        MODULE.audit(campaign)


def test_changed_source_transcript_is_rejected(campaign):
    with sqlite3.connect(campaign / "segments/case-A-1/state.db") as db:
        changed = ChatMessage(role="user", content="changed").model_dump_json()
        db.execute("UPDATE messages SET message_json=?", (changed,))
    with pytest.raises(AssertionError):
        MODULE.audit(campaign)


def test_unknown_usage_is_not_counted_as_zero_cost(campaign):
    known = json.loads((campaign / "requests.jsonl").read_text().splitlines()[0])
    unknown = {
        **known,
        "usage_complete": False,
        "raw_usage": {},
        "normalized_peak_cost_usd": None,
        "cost_usd_at_start_rate": None,
        "budget_charge_usd": 0.2,
        "error": {"type": "ConnectError"},
    }
    report = MODULE.aggregate_requests([known, unknown])
    assert report["input_tokens"] == 100
    assert report["unknown_cost_requests"] == 1
    assert report["unknown_budget_reserve_usd"] == 0.2
    assert report["budget_reserved_usd"] == pytest.approx(0.2 + known["budget_charge_usd"])
