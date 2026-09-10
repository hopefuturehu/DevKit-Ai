"""Recount saved L1 requests offline. No generation or cache warmup is performed."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

from bot.core.models import ModelRequest
from bot.providers import OpenAICompatibleProvider
from bot.providers.token_counting import REVISION


def summarize(rows: list[dict]) -> dict:
    def errors(key: str) -> dict:
        absolute = sorted(abs(row[key] - row["actual"]) for row in rows)
        relative = sorted(abs(row[key] - row["actual"]) / row["actual"] for row in rows)
        return {
            "absolute_error_max": max(absolute),
            "relative_error_p50": relative[math.ceil(len(relative) * 0.5) - 1],
            "relative_error_p95": relative[math.ceil(len(relative) * 0.95) - 1],
            "relative_error_max": max(relative),
            "underestimate_count": sum(row[key] < row["actual"] for row in rows),
            "underestimate_max": max(0, max(row["actual"] - row[key] for row in rows)),
        }

    return {
        "requests": len(rows),
        "unique_request_hashes": len({row["request_sha256"] for row in rows}),
        "legacy": errors("legacy"),
        "tokenizer": errors("tokens"),
        "budget_exceeded_count": sum(row["actual"] > row["budget_tokens"] for row in rows),
    }


def analyze(root: Path) -> dict:
    files = {path.name: path for path in root.rglob("*.request.json")}
    ledger = [json.loads(line) for line in (root / "requests.jsonl").read_text().splitlines()]
    provider = OpenAICompatibleProvider(
        base_url="https://api.deepseek.com/v1", api_key="offline-unused"
    )
    measured: list[dict] = []
    cases: dict[str, list[dict]] = defaultdict(list)
    skipped = 0
    for row in ledger:
        actual = row.get("raw_usage", {}).get("prompt_tokens")
        if not isinstance(actual, int) or isinstance(actual, bool) or actual <= 0:
            skipped += 1
            continue
        content = files[f"{row['request_id']}.request.json"].read_bytes()
        if hashlib.sha256(content).hexdigest() != row["request_sha256"]:
            raise ValueError(f"Saved request changed: {row['request_id']}")
        request = ModelRequest.model_validate_json(content)
        estimate = provider.estimate_input_tokens(request)
        if estimate is None or not estimate.source.startswith("deepseek-v4-flash:"):
            raise ValueError("Install the pinned tokenizer before auditing this model")
        result = {
            "request_sha256": row["request_sha256"],
            "actual": actual,
            "legacy": row["estimated_input_tokens"],
            "tokens": estimate.tokens,
            "budget_tokens": estimate.budget_tokens,
        }
        measured.append(result)
        cases[row["checkpoint"]].append(result)
    if not measured:
        raise ValueError("No requests with measured input usage")
    return {
        "tokenizer_revision": REVISION,
        "ledger_rows": len(ledger),
        "missing_input_usage": skipped,
        "thinking_modes": sorted({row["thinking"] for row in ledger}),
        "overall": summarize(measured),
        "checkpoints": {name: summarize(rows) for name, rows in sorted(cases.items())},
        "scope": (
            "Offline replay of saved payloads; six sources, not independent tasks or new API calls."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = analyze(args.input)
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
