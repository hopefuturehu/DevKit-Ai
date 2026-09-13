#!/usr/bin/env python3
"""Read-only, offline audit of reasoning input for the two Flash compactions.

Compare the current renderer with a counterfactual ordinary JSON data field.
Token estimates are not API usage or evidence of summary quality. No model calls
are made and no historical reasoning text is printed. Run from the project venv.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from bot.compaction.service import ContextCompactor
from bot.config.models import AppConfig
from bot.core.context import TokenEstimator
from bot.core.models import ChatMessage, Role
from bot.providers.openai_compatible import OpenAICompatibleProvider

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def main():
    metrics_path = ROOT / "docs/data/public-benchmark-context-metrics.json"
    metrics = json.loads(metrics_path.read_text())
    # Only pure rendering/request-building methods are used: no session store,
    # event bus, provider counting or provider execution is initialized here.
    compactor = object.__new__(ContextCompactor)
    compactor.config = AppConfig()
    compactor.model_name = "deepseek-v4-flash"
    compactor.config.model.thinking = "disabled"
    estimator = TokenEstimator()
    provider = OpenAICompatibleProvider(
        base_url="https://api.deepseek.com/v1", api_key="offline-unused"
    )

    def request(source, start, end):
        return compactor._summary_request(
            previous_summary=None,
            source_payload=source,
            covered_start=start,
            covered_end=end,
            rebuilt_from_raw=True,
        )

    marker = ChatMessage(
        role=Role.ASSISTANT,
        content="<think>INLINE_INPUT_SENTINEL</think>Visible answer",
        reasoning_content="NATIVE_INPUT_SENTINEL",
    )
    rendered = compactor._render_message(1, marker)
    wire = provider._payload(request([rendered], 1, 1))
    serialized = json.dumps(wire, ensure_ascii=False, sort_keys=True)
    probe = {
        "native_marker_in_http_payload": "NATIVE_INPUT_SENTINEL" in serialized,
        "inline_marker_in_http_payload": "INLINE_INPUT_SENTINEL" in serialized,
        "tools_in_http_payload": "tools" in wire,
        "http_roles": [message["role"] for message in wire["messages"]],
        "thinking": wire["thinking"]["type"],
    }
    assert probe["native_marker_in_http_payload"] is False
    assert probe["inline_marker_in_http_payload"] is True
    assert probe["tools_in_http_payload"] is False
    assert probe["http_roles"] == ["system", "user"]

    records = []
    for run in metrics["runs"]:
        for cp in run["compactions"]:
            assert cp["rebuilt_from_raw"] and cp["parent_id"] is None
            db_path = ROOT / run["state_path"]
            wal_path = db_path.with_name(db_path.name + "-wal")
            before = (digest(db_path), digest(wal_path))
            assert before == (run["state_sha256"], run["state_wal_sha256"])
            start, end = cp["covered_start_position"], cp["covered_end_position"]
            with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as db:
                rows = db.execute(
                    "SELECT position, message_json FROM messages "
                    "WHERE position BETWEEN ? AND ? ORDER BY position",
                    (start, end),
                ).fetchall()
            assert [position for position, _ in rows] == list(range(start, end + 1))
            messages = [(p, ChatMessage.model_validate_json(raw)) for p, raw in rows]
            baseline = [compactor._render_message(p, message) for p, message in messages]
            full = [
                dict(item, historical_reasoning=message.reasoning_content)
                if message.reasoning_content
                else dict(item)
                for item, (_, message) in zip(baseline, messages, strict=True)
            ]
            chars = len(json.dumps(baseline, ensure_ascii=False, sort_keys=True))
            assert chars == cp["source_chars"]
            requests = [request(source, start, end) for source in (baseline, full)]
            estimates = [estimator.request(req.messages, req.tools) for req in requests]
            assert estimates[0] == cp["planned_input_tokens"]
            assert before == (digest(db_path), digest(wal_path))
            records.append(
                {
                    "task": run["task"],
                    "scope": run["scope"],
                    "compaction_id": cp["compaction_id"],
                    "covered_range": [start, end],
                    "state_path": run["state_path"],
                    "state_sha256": before[0],
                    "state_wal_sha256": before[1],
                    "source_messages": len(messages),
                    "messages_with_nonempty_reasoning": sum(
                        bool(message.reasoning_content) for _, message in messages
                    ),
                    "historical_reasoning_chars": sum(
                        len(message.reasoning_content or "") for _, message in messages
                    ),
                    "rendered_source_chars": chars,
                    "actual_api_input_tokens": cp["input_tokens"],
                    "baseline_estimated_input_tokens": estimates[0],
                    "full_reasoning_estimated_input_tokens": estimates[1],
                    "added_estimated_input_tokens": estimates[1] - estimates[0],
                    "estimated_input_increase_ratio": estimates[1] / estimates[0] - 1,
                    "configured_input_limit": cp["input_limit"],
                    "planning_limit": compactor._compaction_planning_limit(),
                }
            )
    assert len(records) == 2
    print(
        json.dumps(
            {
                "schema_version": 1,
                "snapshot_metrics_sha256": digest(metrics_path),
                "source_files_sha256": {
                    name: digest(ROOT / name)
                    for name in (
                        "src/bot/compaction/service.py",
                        "src/bot/config/models.py",
                        "src/bot/core/context.py",
                        "src/bot/core/models.py",
                        "src/bot/providers/openai_compatible.py",
                    )
                },
                "measurement": "Fixed source ranges; current default renderer; estimated full "
                "input adds nonempty reasoning as historical_reasoning inside user JSON; "
                "no model calls; no claims about quality or measured API savings.",
                "serialization_probe": probe,
                "runs": records,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        end="",
    )


if __name__ == "__main__":
    main()
