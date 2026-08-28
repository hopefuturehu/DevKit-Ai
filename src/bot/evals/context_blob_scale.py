from __future__ import annotations

import csv
import hashlib
import json
import math
import sqlite3
import statistics
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

from bot.core.models import ChatMessage, Role
from bot.sessions import SQLiteSessionStore

BlobScaleSuite = Literal["fast", "soak"]


@dataclass(frozen=True)
class ContextBlobScaleProfile:
    """Reproducible workload for the externalized context-blob store."""

    name: BlobScaleSuite
    blob_count: int
    blob_sizes: tuple[int, ...]
    read_repeats: int
    query_repeats: int
    concurrency: int

    def with_overrides(
        self,
        *,
        blob_count: int | None = None,
        blob_sizes: tuple[int, ...] | None = None,
        read_repeats: int | None = None,
        query_repeats: int | None = None,
        concurrency: int | None = None,
    ) -> ContextBlobScaleProfile:
        return replace(
            self,
            blob_count=self.blob_count if blob_count is None else blob_count,
            blob_sizes=self.blob_sizes if blob_sizes is None else blob_sizes,
            read_repeats=self.read_repeats if read_repeats is None else read_repeats,
            query_repeats=self.query_repeats if query_repeats is None else query_repeats,
            concurrency=self.concurrency if concurrency is None else concurrency,
        )


FAST_CONTEXT_BLOB_SCALE_PROFILE = ContextBlobScaleProfile(
    name="fast",
    blob_count=24,
    blob_sizes=(4 * 1024, 64 * 1024, 256 * 1024),
    read_repeats=2,
    query_repeats=2,
    concurrency=4,
)

SOAK_CONTEXT_BLOB_SCALE_PROFILE = ContextBlobScaleProfile(
    name="soak",
    blob_count=120,
    blob_sizes=(64 * 1024, 1024 * 1024, 8 * 1024 * 1024),
    read_repeats=3,
    query_repeats=3,
    concurrency=8,
)


def context_blob_scale_profile(name: str) -> ContextBlobScaleProfile:
    profiles = {
        FAST_CONTEXT_BLOB_SCALE_PROFILE.name: FAST_CONTEXT_BLOB_SCALE_PROFILE,
        SOAK_CONTEXT_BLOB_SCALE_PROFILE.name: SOAK_CONTEXT_BLOB_SCALE_PROFILE,
    }
    try:
        return profiles[name]
    except KeyError as exc:
        raise ValueError(f"未知 context-blob-scale suite: {name}") from exc


@dataclass(frozen=True)
class ContextBlobScaleOperation:
    operation: str
    blob_index: int
    blob_bytes: int
    elapsed_seconds: float
    success: bool
    returned_chars: int = 0


@dataclass(frozen=True)
class ContextBlobScaleResult:
    workspace: Path
    summary: dict[str, Any]
    operations: tuple[ContextBlobScaleOperation, ...]


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return ordered[index]


def _latency_summary(operations: list[ContextBlobScaleOperation], kind: str) -> dict[str, float]:
    values = [item.elapsed_seconds for item in operations if item.operation == kind]
    return {
        "count": len(values),
        "p50_seconds": statistics.median(values) if values else 0.0,
        "p95_seconds": _percentile(values, 0.95),
        "max_seconds": max(values, default=0.0),
    }


def _payload(blob_index: int, byte_count: int) -> tuple[str, dict[str, tuple[str, int]]]:
    if byte_count < 512:
        raise ValueError("blob size 至少为 512 bytes")
    raw = bytearray(b"x" * byte_count)
    markers = {
        "head": f"BLOB-{blob_index:06d}-HEAD",
        "middle": f"BLOB-{blob_index:06d}-MIDDLE",
        "tail": f"BLOB-{blob_index:06d}-TAIL",
    }
    offsets = {
        "head": 32,
        "middle": byte_count // 2,
        "tail": byte_count - len(markers["tail"].encode()) - 32,
    }
    result: dict[str, tuple[str, int]] = {}
    for position, marker in markers.items():
        encoded = marker.encode("ascii")
        offset = offsets[position]
        raw[offset : offset + len(encoded)] = encoded
        result[position] = (marker, offset)
    return raw.decode("ascii"), result


def _database_stats(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as connection:
        blob_count, logical_bytes = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(byte_count), 0) FROM context_blobs"
        ).fetchone()
        access_count = connection.execute("SELECT COUNT(*) FROM context_blob_access").fetchone()[0]
    disk_bytes = sum(
        candidate.stat().st_size
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if candidate.exists()
    )
    return {
        "unique_blobs": int(blob_count),
        "logical_blob_bytes": int(logical_bytes),
        "access_grants": int(access_count),
        "database_disk_bytes": disk_bytes,
    }


def run_context_blob_scale_benchmark(
    *,
    profile: ContextBlobScaleProfile,
    workspace: Path,
) -> ContextBlobScaleResult:
    """Exercise blob correctness and collect scale observations without an LLM."""

    if profile.blob_count < 1:
        raise ValueError("blob_count 必须大于 0")
    if not profile.blob_sizes or any(size < 512 for size in profile.blob_sizes):
        raise ValueError("blob_sizes 必须包含至少一个不小于 512 的值")
    if min(profile.read_repeats, profile.query_repeats, profile.concurrency) < 1:
        raise ValueError("repeat 和 concurrency 必须大于 0")

    workspace = workspace.resolve()
    artifacts = workspace / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    database = workspace / "state.db"
    store = SQLiteSessionStore(database)
    operations: list[ContextBlobScaleOperation] = []
    references: list[str] = []
    payloads: list[str] = []
    markers_by_blob: list[dict[str, tuple[str, int]]] = []
    sizes: list[int] = []
    session_id = store.create_session(workspace, session_id="blob-scale-source")
    unauthorized_session = store.create_session(workspace, session_id="blob-scale-unauthorized")
    started = time.perf_counter()
    peak_python_bytes = 0

    try:
        for blob_index in range(profile.blob_count):
            size = profile.blob_sizes[blob_index % len(profile.blob_sizes)]
            payload, markers = _payload(blob_index, size)
            before = time.perf_counter()
            reference = store.put_context_blob(
                session_id=session_id,
                run_id=f"store-{blob_index}",
                content=payload,
            )
            operations.append(
                ContextBlobScaleOperation(
                    operation="put",
                    blob_index=blob_index,
                    blob_bytes=size,
                    elapsed_seconds=time.perf_counter() - before,
                    success=True,
                )
            )
            references.append(reference)
            payloads.append(payload)
            markers_by_blob.append(markers)
            sizes.append(size)

        before = time.perf_counter()
        duplicate_reference = store.put_context_blob(
            session_id=session_id,
            run_id="duplicate",
            content=payloads[0],
        )
        operations.append(
            ContextBlobScaleOperation(
                operation="put_duplicate",
                blob_index=0,
                blob_bytes=sizes[0],
                elapsed_seconds=time.perf_counter() - before,
                success=duplicate_reference == references[0],
            )
        )

        selected = sorted({0, len(references) // 2, len(references) - 1})
        for blob_index in selected:
            reference = references[blob_index]
            for position in ("head", "middle", "tail"):
                marker, expected_offset = markers_by_blob[blob_index][position]
                for _ in range(profile.query_repeats):
                    if sizes[blob_index] == max(sizes):
                        tracemalloc.start()
                    before = time.perf_counter()
                    result = store.search_context_blob(
                        session_id,
                        reference,
                        query=marker,
                        max_matches=2,
                        context_chars=64,
                        case_sensitive=True,
                    )
                    elapsed = time.perf_counter() - before
                    if tracemalloc.is_tracing():
                        _, observed_peak = tracemalloc.get_traced_memory()
                        peak_python_bytes = max(peak_python_bytes, observed_peak)
                        tracemalloc.stop()
                    matches = [] if result is None else result["matches"]
                    success = (
                        len(matches) == 1
                        and int(matches[0]["byte_offset"]) == expected_offset
                        and marker in str(matches[0]["preview"])
                    )
                    returned_chars = sum(len(str(match["preview"])) for match in matches)
                    operations.append(
                        ContextBlobScaleOperation(
                            operation="query",
                            blob_index=blob_index,
                            blob_bytes=sizes[blob_index],
                            elapsed_seconds=elapsed,
                            success=success and returned_chars <= 160,
                            returned_chars=returned_chars,
                        )
                    )

                for _ in range(profile.read_repeats):
                    before = time.perf_counter()
                    result = store.read_context_blob(
                        session_id,
                        reference,
                        offset=max(0, expected_offset - 16),
                        limit=len(marker) + 32,
                    )
                    operations.append(
                        ContextBlobScaleOperation(
                            operation="range_read",
                            blob_index=blob_index,
                            blob_bytes=sizes[blob_index],
                            elapsed_seconds=time.perf_counter() - before,
                            success=result is not None and marker in result["content"],
                            returned_chars=0 if result is None else len(result["content"]),
                        )
                    )

        missing_query = store.search_context_blob(
            session_id,
            references[-1],
            query="BLOB-MARKER-THAT-DOES-NOT-EXIST",
        )
        missing_query_ok = missing_query is not None and missing_query["matches"] == []
        unauthorized_blocked = (
            store.read_context_blob(unauthorized_session, references[0], limit=32) is None
        )

        store.append_message(
            session_id,
            "blob-scale-fork-source",
            ChatMessage(role=Role.USER, content=f"Preserve {references[-1]} for the fork."),
        )
        fork_session = store.fork_session(session_id)
        fork_access_ok = store.read_context_blob(fork_session, references[-1], limit=32) is not None

        concurrent_inputs = [
            (index, references[index], markers_by_blob[index]["middle"][0])
            for index in range(len(references))
        ]

        def concurrent_query(item: tuple[int, str, str]) -> ContextBlobScaleOperation:
            blob_index, reference, marker = item
            before = time.perf_counter()
            result = store.search_context_blob(
                session_id,
                reference,
                query=marker,
                max_matches=1,
                context_chars=32,
                case_sensitive=True,
            )
            matches = [] if result is None else result["matches"]
            return ContextBlobScaleOperation(
                operation="concurrent_query",
                blob_index=blob_index,
                blob_bytes=sizes[blob_index],
                elapsed_seconds=time.perf_counter() - before,
                success=len(matches) == 1 and marker in str(matches[0]["preview"]),
                returned_chars=sum(len(str(match["preview"])) for match in matches),
            )

        with ThreadPoolExecutor(max_workers=profile.concurrency) as executor:
            operations.extend(executor.map(concurrent_query, concurrent_inputs))

        database_stats = _database_stats(database)
    finally:
        store.close()

    elapsed_seconds = time.perf_counter() - started
    expected_logical_bytes = sum(sizes)
    operation_failures = [item for item in operations if not item.success]
    quality_gates = {
        "all_operations_correct": not operation_failures,
        "content_addressed_deduplication": duplicate_reference == references[0]
        and database_stats["unique_blobs"] == profile.blob_count,
        "logical_bytes_match": database_stats["logical_blob_bytes"] == expected_logical_bytes,
        "missing_query_is_empty": missing_query_ok,
        "unauthorized_session_is_blocked": unauthorized_blocked,
        "fork_inherits_referenced_blob": fork_access_ok,
        "query_output_is_bounded": all(
            item.returned_chars <= 160 for item in operations if item.operation == "query"
        ),
    }
    summary: dict[str, Any] = {
        "schema_version": 1,
        "benchmark": "context-blob-scale",
        "profile": asdict(profile),
        "workload_sha256": hashlib.sha256(
            json.dumps(
                {"sizes": sizes, "markers": markers_by_blob},
                ensure_ascii=True,
                sort_keys=True,
            ).encode()
        ).hexdigest(),
        "storage": {
            **database_stats,
            "attempted_puts": profile.blob_count + 1,
            "duplicate_bytes_avoided": sizes[0],
            "disk_to_logical_ratio": database_stats["database_disk_bytes"]
            / max(1, database_stats["logical_blob_bytes"]),
        },
        "latency": {
            kind: _latency_summary(operations, kind)
            for kind in ("put", "put_duplicate", "query", "range_read", "concurrent_query")
        },
        "resources": {
            "peak_python_bytes_during_largest_query": peak_python_bytes,
            "wall_time_seconds": elapsed_seconds,
        },
        "quality": {
            "passed": all(quality_gates.values()),
            "gates": quality_gates,
            "operation_failure_count": len(operation_failures),
        },
    }

    with (artifacts / "operations.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(operations[0])))
        writer.writeheader()
        writer.writerows(asdict(operation) for operation in operations)
    (artifacts / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return ContextBlobScaleResult(
        workspace=workspace,
        summary=summary,
        operations=tuple(operations),
    )
