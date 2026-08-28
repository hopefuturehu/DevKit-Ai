from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from bot.evals.context_blob_scale import (
    ContextBlobScaleResult,
    context_blob_scale_profile,
    run_context_blob_scale_benchmark,
)


def _byte_size(value: str) -> int:
    suffixes = {"k": 1024, "m": 1024**2, "g": 1024**3}
    normalized = value.strip().lower()
    multiplier = 1
    if normalized[-1:] in suffixes:
        multiplier = suffixes[normalized[-1]]
        normalized = normalized[:-1]
    try:
        result = int(normalized) * multiplier
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"无效大小: {value}") from exc
    if result < 512:
        raise argparse.ArgumentTypeError("blob 大小至少为 512 bytes")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="运行大 Tool 结果外置存储的规模、查询和访问控制 benchmark。"
    )
    parser.add_argument("--suite", choices=("fast", "soak"), default="fast")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".bot/benchmarks/context-blob-scale"),
    )
    parser.add_argument("--blob-count", type=int)
    parser.add_argument("--blob-sizes", nargs="+", type=_byte_size)
    parser.add_argument("--read-repeats", type=int)
    parser.add_argument("--query-repeats", type=int)
    parser.add_argument("--concurrency", type=int)
    return parser


def _run(args: argparse.Namespace) -> ContextBlobScaleResult:
    profile = context_blob_scale_profile(args.suite).with_overrides(
        blob_count=args.blob_count,
        blob_sizes=None if args.blob_sizes is None else tuple(args.blob_sizes),
        read_repeats=args.read_repeats,
        query_repeats=args.query_repeats,
        concurrency=args.concurrency,
    )
    result = run_context_blob_scale_benchmark(profile=profile, workspace=args.output / args.suite)
    print(
        json.dumps(
            {
                "suite": args.suite,
                "passed": result.summary["quality"]["passed"],
                "unique_blobs": result.summary["storage"]["unique_blobs"],
                "logical_blob_bytes": result.summary["storage"]["logical_blob_bytes"],
                "query_p95_seconds": result.summary["latency"]["query"]["p95_seconds"],
                "database_disk_bytes": result.summary["storage"]["database_disk_bytes"],
                "artifacts": str(result.workspace / "artifacts"),
            },
            ensure_ascii=False,
        )
    )
    return result


def main() -> int:
    args = _parser().parse_args()
    if args.suite == "soak" and os.getenv("RUN_CONTEXT_BLOB_SCALE_SOAK") != "1":
        raise SystemExit("soak 需要显式设置 RUN_CONTEXT_BLOB_SCALE_SOAK=1")
    for name in ("blob_count", "read_repeats", "query_repeats", "concurrency"):
        value = getattr(args, name)
        if value is not None and value < 1:
            raise SystemExit(f"--{name.replace('_', '-')} 必须大于 0")
    result = _run(args)
    return 0 if result.summary["quality"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
