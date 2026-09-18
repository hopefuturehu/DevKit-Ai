"""Read-only discovery and de-duplication of bot history databases.

The workbench can analyse more than the live workspace database. Benchmark runs
and archived artifacts each keep their own ``state.db``, and the same run id can
appear in several of them (a snapshot copied between segments, for example).

Rules enforced here:

* Every source is opened with a read-only SQLite URI. Nothing is migrated and
  nothing is written back to the source database.
* Duplicates are detected by **run id plus content**, never by file name. Two
  files with the same name are not assumed to be the same database, and two
  databases holding the same run id are not assumed to be identical.
* When copies of one run disagree, the conflict is reported instead of being
  silently resolved. The chosen copy is named together with the reason.
* A database that is corrupt or has an incompatible schema is reported as a
  problem and skipped; it never prevents the other sources from loading.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Columns the analysis layer needs from ``runs``. A database missing any of them
# is treated as schema-incompatible rather than being partially read.
REQUIRED_RUN_COLUMNS = {
    "id",
    "session_id",
    "status",
    "started_at",
    "completed_at",
    "error",
}

# Source kinds, ordered by how authoritative they are when the same run appears
# in more than one place. The live workspace database wins, then benchmarks,
# then archived artifacts.
SOURCE_KINDS = ("main", "benchmark", "artifact")
SOURCE_LABELS = {
    "main": "主会话库",
    "benchmark": "评测库",
    "artifact": "归档产物",
}


@dataclass
class SourceProblem:
    """A source that could not be used, with a human-readable reason."""

    path: str
    kind: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "kind": self.kind, "reason": self.reason}


@dataclass
class RunCopy:
    """One physical copy of a run inside one database."""

    run_id: str
    source_path: str
    source_kind: str
    run: dict[str, Any]
    events: list[dict[str, Any]]
    fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "source_path": self.source_path,
            "source_kind": self.source_kind,
            "fingerprint": self.fingerprint,
        }


@dataclass
class DedupedRun:
    """The copy chosen for a run id, plus every other copy that was seen."""

    run_id: str
    chosen: RunCopy
    duplicates: list[RunCopy] = field(default_factory=list)
    conflict: bool = False
    conflict_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "source_path": self.chosen.source_path,
            "source_kind": self.chosen.source_kind,
            "fingerprint": self.chosen.fingerprint,
            "duplicate_count": len(self.duplicates),
            "duplicate_sources": [copy.source_path for copy in self.duplicates],
            "conflict": self.conflict,
            "conflict_reason": self.conflict_reason,
        }


def _fingerprint(run: dict[str, Any], events: list[dict[str, Any]]) -> str:
    """Content hash of a run copy, used to tell identical copies from conflicts.

    The ``runs`` row and the event stream are both hashed. Timestamps are part of
    the row, so two copies recorded at different moments hash differently even
    when their event streams match: that difference is exactly what makes them a
    conflict worth reporting rather than a silent duplicate.

    The digest is built by streaming into the hash instead of materialising one
    large JSON document: the real workspace holds hundreds of thousands of
    events, and the intermediate string dominated discovery time.
    """
    digest = hashlib.sha256()
    for key in sorted(REQUIRED_RUN_COLUMNS):
        digest.update(str(key).encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(run.get(key)).encode("utf-8"))
        digest.update(b"\x1f")
    for event in events:
        digest.update(str(event.get("type")).encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(event.get("timestamp")).encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(event.get("payload_json")).encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()[:16]


def _read_only_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


def inspect_database(path: Path, kind: str) -> tuple[list[RunCopy], SourceProblem | None]:
    """Read every run from one database, or explain why it cannot be read.

    The connection is opened read-only and closed immediately; no transaction is
    ever started, so the source file is never modified.
    """
    try:
        connection = sqlite3.connect(_read_only_uri(path), uri=True)
    except sqlite3.Error as exc:
        return [], SourceProblem(str(path), kind, f"无法打开数据库: {exc}")

    try:
        connection.row_factory = sqlite3.Row
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        except sqlite3.DatabaseError as exc:
            return [], SourceProblem(str(path), kind, f"数据库损坏或无法读取: {exc}")

        if "runs" not in tables:
            return [], SourceProblem(str(path), kind, "缺少 runs 表，schema 不兼容")

        columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
        missing = REQUIRED_RUN_COLUMNS - columns
        if missing:
            return [], SourceProblem(
                str(path), kind, f"runs 表缺少字段: {', '.join(sorted(missing))}"
            )

        try:
            run_rows = [dict(row) for row in connection.execute("SELECT * FROM runs")]
        except sqlite3.DatabaseError as exc:
            return [], SourceProblem(str(path), kind, f"读取 runs 失败: {exc}")

        has_events = "events" in tables
        events_by_run: dict[str, list[dict[str, Any]]] = {}
        if has_events:
            # One query for the whole database instead of one per run: the
            # per-run form issued hundreds of round trips and dominated the
            # cold-load time.
            try:
                for row in connection.execute(
                    "SELECT run_id,type,timestamp,payload_json FROM events ORDER BY rowid"
                ):
                    events_by_run.setdefault(row["run_id"], []).append(
                        {
                            "type": row["type"],
                            "timestamp": row["timestamp"],
                            "payload_json": row["payload_json"],
                        }
                    )
            except sqlite3.DatabaseError as exc:
                return [], SourceProblem(str(path), kind, f"读取 events 失败: {exc}")

        copies: list[RunCopy] = []
        for run in run_rows:
            events = events_by_run.get(run["id"], [])
            copies.append(
                RunCopy(
                    run_id=run["id"],
                    source_path=str(path),
                    source_kind=kind,
                    run=run,
                    events=events,
                    fingerprint=_fingerprint(run, events),
                )
            )
        return copies, None
    finally:
        connection.close()


def discover_sources(workspace: Path) -> list[tuple[Path, str]]:
    """Find every candidate history database under the workspace.

    The live database comes first, then ``.bot/benchmarks`` and ``artifacts``.
    Results are sorted so the same workspace always yields the same order.
    """
    workspace = workspace.resolve()
    found: list[tuple[Path, str]] = []

    main = workspace / ".bot" / "state.db"
    if main.is_file():
        found.append((main, "main"))

    for kind, root in (
        ("benchmark", workspace / ".bot" / "benchmarks"),
        ("artifact", workspace / "artifacts"),
    ):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("state.db")):
            if path.is_file():
                found.append((path, kind))
    return found


def _copy_rank(copy: RunCopy) -> tuple[int, int, str]:
    """Order copies so the most authoritative one is chosen deterministically.

    Preference: source kind (main > benchmark > artifact), then the copy with
    more events (a fuller record beats a truncated snapshot), then the path so
    the result never depends on filesystem iteration order.
    """
    kind_rank = (
        SOURCE_KINDS.index(copy.source_kind)
        if copy.source_kind in SOURCE_KINDS
        else len(SOURCE_KINDS)
    )
    return (kind_rank, -len(copy.events), copy.source_path)


def collect_runs(
    workspace: Path,
) -> tuple[list[DedupedRun], list[SourceProblem], dict[str, Any]]:
    """Load, de-duplicate and report on every history database in the workspace.

    Returns the de-duplicated runs, the sources that could not be read, and a
    summary describing how many raw records were seen and how many were excluded.
    """
    sources = discover_sources(workspace)
    problems: list[SourceProblem] = []
    by_run: dict[str, list[RunCopy]] = {}
    raw_records = 0
    loaded_sources = 0

    for path, kind in sources:
        copies, problem = inspect_database(path, kind)
        if problem is not None:
            problems.append(problem)
            continue
        loaded_sources += 1
        raw_records += len(copies)
        for copy in copies:
            by_run.setdefault(copy.run_id, []).append(copy)

    deduped: list[DedupedRun] = []
    for run_id, copies in by_run.items():
        ordered = sorted(copies, key=_copy_rank)
        chosen = ordered[0]
        duplicates = ordered[1:]
        conflict = False
        conflict_reason = None
        if duplicates:
            differing = [copy for copy in duplicates if copy.fingerprint != chosen.fingerprint]
            if differing:
                conflict = True
                label = SOURCE_LABELS.get(chosen.source_kind, chosen.source_kind)
                conflict_reason = (
                    f"同一 Run 在 {len(copies)} 个来源中内容不一致，"
                    f"已选用 {chosen.source_path}（{label}）"
                )
        deduped.append(
            DedupedRun(
                run_id=run_id,
                chosen=chosen,
                duplicates=duplicates,
                conflict=conflict,
                conflict_reason=conflict_reason,
            )
        )

    deduped.sort(key=lambda item: item.run_id)
    summary = {
        "sources_discovered": len(sources),
        "sources_loaded": loaded_sources,
        "sources_failed": len(problems),
        "raw_records": raw_records,
        "deduped_records": len(deduped),
        "excluded_duplicates": raw_records - len(deduped),
        "conflicts": sum(1 for item in deduped if item.conflict),
        "by_kind": {
            kind: sum(1 for item in deduped if item.chosen.source_kind == kind)
            for kind in SOURCE_KINDS
        },
    }
    return deduped, problems, summary
