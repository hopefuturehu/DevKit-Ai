"""Read models for the workbench, backed by the durable event log.

Event rowids are used only inside one server epoch. A server restart forces a
fresh snapshot, so EventBus sequence resets never skip or overwrite events.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from bot.sessions import SQLiteSessionStore
from bot.web.analysis import DEFAULT_GAP_THRESHOLD_SECONDS, analyze_run
from bot.web.sorting import sort_analyses
from bot.web.sources import collect_runs


def _prompt_from_events(events: list[dict[str, Any]]) -> str:
    """Extract the run prompt from its ``run.started`` event, if present."""
    for event in events:
        if event.get("type") != "run.started":
            continue
        try:
            payload = json.loads(event.get("payload_json") or "{}")
        except (TypeError, ValueError):
            return ""
        return payload.get("prompt") or ""
    return ""


class CursorExpired(ValueError):
    pass


class WebQueries:
    def __init__(self, store: SQLiteSessionStore, workspace: Path) -> None:
        self.store = store
        self.workspace = workspace.resolve()
        self.epoch = uuid4().hex

    @contextmanager
    def connection(self):
        with sqlite3.connect(self.store.path.as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            yield connection

    def rows(self, query: str, args=()) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(query, args).fetchall()]

    def session(self, session_id: str) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if session is None:
            raise LookupError("会话不存在")
        root = session
        seen = set()
        while task := self.store.get_agent_task_by_child_session(str(root["id"])):
            if root["id"] in seen:
                raise LookupError("子任务关系无效")
            seen.add(root["id"])
            root = self.store.get_session(task["parent_session_id"])
            if root is None:
                raise LookupError("父会话不存在")
        if Path(root["workspace"]).resolve() != self.workspace:
            raise LookupError("会话不属于当前工作区")
        return session

    def sessions(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        return self.rows(
            """SELECT s.*,
                (SELECT json_extract(e.payload_json, '$.prompt') FROM events e
                 WHERE e.session_id=s.id AND e.type='run.started' ORDER BY e.rowid LIMIT 1)
                    AS title,
                (SELECT r.status FROM runs r WHERE r.session_id=s.id
                 ORDER BY r.rowid DESC LIMIT 1) AS status,
                (SELECT r.id FROM runs r WHERE r.session_id=s.id
                 ORDER BY r.rowid DESC LIMIT 1) AS latest_run_id
               FROM sessions s WHERE s.workspace=? AND NOT EXISTS
                   (SELECT 1 FROM agent_tasks t WHERE t.child_session_id=s.id)
               ORDER BY s.updated_at DESC, s.id LIMIT ? OFFSET ?""",
            (str(self.workspace), limit, offset),
        )

    def run(self, run_id: str) -> dict[str, Any]:
        rows = self.rows("SELECT * FROM runs WHERE id=?", (run_id,))
        if not rows:
            raise LookupError("任务运行不存在")
        result = rows[0]
        self.session(result["session_id"])
        latest = self.rows(
            """SELECT payload_json FROM events WHERE run_id=? AND type IN
               ('model.usage', 'run.finished') ORDER BY rowid DESC LIMIT 1""",
            (run_id,),
        )
        if latest:
            payload = json.loads(latest[0]["payload_json"])
            for key in ("input_tokens", "output_tokens", "cost_usd", "steps", "final_text"):
                if key in payload:
                    result[key] = payload[key]
        start = self.rows(
            "SELECT payload_json FROM events WHERE run_id=? AND type='run.started' "
            "ORDER BY rowid LIMIT 1",
            (run_id,),
        )
        result["prompt"] = json.loads(start[0]["payload_json"]).get("prompt", "") if start else ""
        result["usage_scope"] = "run_including_compaction_excluding_children"
        return result

    def runs(self, session_id: str, *, limit: int = 50, offset: int = 0):
        self.session(session_id)
        rows = self.rows(
            "SELECT id FROM runs WHERE session_id=? ORDER BY rowid DESC LIMIT ? OFFSET ?",
            (session_id, limit, offset),
        )
        return [self.run(row["id"]) for row in rows]

    def scope(self, session_id: str, run_id: str | None = None, children: bool = True):
        self.session(session_id)
        if run_id is not None and self.run(run_id)["session_id"] != session_id:
            raise LookupError("运行不属于所选会话")
        if not children:
            return (
                "e.session_id=?" + (" AND e.run_id=?" if run_id else ""),
                [session_id, *([run_id] if run_id else [])],
            )
        # Child sessions come from durable task ownership, never event timestamps/names.
        if run_id:
            return (
                """(e.run_id=? OR e.session_id IN (
                    SELECT child_session_id FROM agent_tasks WHERE parent_run_id=?)
                    OR e.run_id IN (SELECT 'subagent:' || id FROM agent_tasks
                                   WHERE parent_run_id=?))""",
                [run_id, run_id, run_id],
            )
        return (
            """e.session_id IN (WITH RECURSIVE tree(id) AS (
                SELECT ? UNION SELECT t.child_session_id FROM agent_tasks t
                JOIN tree p ON t.parent_session_id=p.id) SELECT id FROM tree)""",
            [session_id],
        )

    def analysis_runs(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        status: str | None = None,
        stop_reason: str | None = None,
        session_id: str | None = None,
        search: str | None = None,
        since: str | None = None,
        until: str | None = None,
        sort: str = "started",
        order: str = "desc",
        gap_threshold_seconds: float = DEFAULT_GAP_THRESHOLD_SECONDS,
    ) -> dict[str, Any]:
        """Cross-session run list with net-duration and stop-reason analysis.

        Filtering happens in SQL for the cheap columns; stop-reason filtering is
        applied after analysis because the reason lives in event payloads.

        Sorting is applied to the **whole filtered set** before pagination. Doing
        it the other way round silently drops long runs that started early: they
        fall outside the first page and never get a chance to rank first.
        """
        clauses = ["s.workspace=?"]
        args: list[Any] = [str(self.workspace)]
        if session_id:
            clauses.append("r.session_id=?")
            args.append(session_id)
        if status:
            clauses.append("r.status=?")
            args.append(status)
        if since:
            clauses.append("r.started_at>=?")
            args.append(since)
        if until:
            clauses.append("r.started_at<=?")
            args.append(until)
        if search:
            clauses.append(
                """EXISTS (SELECT 1 FROM events e WHERE e.run_id=r.id
                    AND e.type='run.started'
                    AND json_extract(e.payload_json,'$.prompt') LIKE ?)"""
            )
            args.append(f"%{search}%")
        where = " AND ".join(clauses)
        rows = self.rows(
            f"""SELECT r.*, s.workspace,
                (SELECT json_extract(e.payload_json,'$.prompt') FROM events e
                 WHERE e.run_id=r.id AND e.type='run.started' ORDER BY e.rowid LIMIT 1)
                    AS prompt
                FROM runs r JOIN sessions s ON s.id=r.session_id
                WHERE {where} ORDER BY r.started_at DESC, r.id""",
            args,
        )
        analyses = []
        for row in rows:
            events = self.rows(
                "SELECT type,timestamp,payload_json FROM events WHERE run_id=? ORDER BY rowid",
                (row["id"],),
            )
            analysis = analyze_run(
                row, events, gap_threshold_seconds=gap_threshold_seconds
            )
            item = analysis.as_dict()
            item["prompt"] = row.get("prompt") or ""
            item["workspace"] = row.get("workspace")
            item["input_tokens"] = row.get("input_tokens") or 0
            item["output_tokens"] = row.get("output_tokens") or 0
            item["cost_usd"] = row.get("cost_usd")
            analyses.append(item)

        if stop_reason:
            analyses = [item for item in analyses if item["stop_reason"] == stop_reason]

        # Sort the full filtered set first; only then slice the requested page.
        ordered = sort_analyses(analyses, sort=sort, order=order)
        total = len(ordered)
        page = ordered[offset : offset + limit]
        return {
            "runs": page,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < total,
            "sort": sort,
            "order": order,
        }

    def analysis_run(
        self, run_id: str, *, gap_threshold_seconds: float = DEFAULT_GAP_THRESHOLD_SECONDS
    ):
        """Full analysis detail for one run, including its approval intervals."""
        run = self.run(run_id)
        events = self.rows(
            "SELECT type,timestamp,payload_json FROM events WHERE run_id=? ORDER BY rowid",
            (run_id,),
        )
        analysis = analyze_run(run, events, gap_threshold_seconds=gap_threshold_seconds)
        result = analysis.as_dict()
        result["prompt"] = run.get("prompt") or ""
        result["input_tokens"] = run.get("input_tokens") or 0
        result["output_tokens"] = run.get("output_tokens") or 0
        result["cost_usd"] = run.get("cost_usd")
        result["session_id"] = run["session_id"]
        return result

    def analysis_sources(self) -> dict[str, Any]:
        """Discover every history database and de-duplicate the runs inside them.

        The result is cached per query object: discovery walks hundreds of files,
        and the analysis endpoints are called repeatedly while the user filters.
        """
        cached = getattr(self, "_analysis_sources", None)
        if cached is None:
            deduped, problems, summary = collect_runs(self.workspace)
            cached = {
                "runs": deduped,
                "problems": problems,
                "summary": summary,
            }
            self._analysis_sources = cached
        return cached

    def analysis_all_runs(
        self,
        *,
        status: str | None = None,
        stop_reason: str | None = None,
        session_id: str | None = None,
        search: str | None = None,
        since: str | None = None,
        until: str | None = None,
        source_kind: str | None = None,
        gap_threshold_seconds: float = DEFAULT_GAP_THRESHOLD_SECONDS,
    ) -> list[dict[str, Any]]:
        """Analyse every de-duplicated run across all discovered sources.

        Filtering happens in Python because the interesting fields (net duration,
        stop reason) only exist after analysis. The caller sorts and paginates the
        returned list, so the whole filtered set is always available.
        """
        sources = self.analysis_sources()
        items: list[dict[str, Any]] = []
        for entry in sources["runs"]:
            copy = entry.chosen
            if source_kind and copy.source_kind != source_kind:
                continue
            run = copy.run
            if status and run.get("status") != status:
                continue
            if session_id and run.get("session_id") != session_id:
                continue
            if since and (run.get("started_at") or "") < since:
                continue
            if until and (run.get("started_at") or "") > until:
                continue
            analysis = analyze_run(
                run, copy.events, gap_threshold_seconds=gap_threshold_seconds
            )
            item = analysis.as_dict()
            item["prompt"] = _prompt_from_events(copy.events)
            item["workspace"] = str(self.workspace)
            item["input_tokens"] = run.get("input_tokens") or 0
            item["output_tokens"] = run.get("output_tokens") or 0
            item["cost_usd"] = run.get("cost_usd")
            item["source_kind"] = copy.source_kind
            item["source_path"] = copy.source_path
            item["duplicate_count"] = len(entry.duplicates)
            item["duplicate_sources"] = [dup.source_path for dup in entry.duplicates]
            item["source_conflict"] = entry.conflict
            item["source_conflict_reason"] = entry.conflict_reason
            items.append(item)

        if stop_reason:
            items = [item for item in items if item["stop_reason"] == stop_reason]
        if search:
            needle = search.lower()
            items = [item for item in items if needle in (item.get("prompt") or "").lower()]
        return items

    def analysis_run_any(
        self, run_id: str, *, gap_threshold_seconds: float = DEFAULT_GAP_THRESHOLD_SECONDS
    ):
        """Analyse one run from whichever source holds it, main database first."""
        sources = self.analysis_sources()
        for entry in sources["runs"]:
            if entry.run_id != run_id:
                continue
            copy = entry.chosen
            analysis = analyze_run(
                copy.run, copy.events, gap_threshold_seconds=gap_threshold_seconds
            )
            result = analysis.as_dict()
            result["prompt"] = _prompt_from_events(copy.events)
            result["input_tokens"] = copy.run.get("input_tokens") or 0
            result["output_tokens"] = copy.run.get("output_tokens") or 0
            result["cost_usd"] = copy.run.get("cost_usd")
            result["session_id"] = copy.run.get("session_id")
            result["source_kind"] = copy.source_kind
            result["source_path"] = copy.source_path
            result["duplicate_count"] = len(entry.duplicates)
            result["duplicate_sources"] = [dup.source_path for dup in entry.duplicates]
            result["source_conflict"] = entry.conflict
            result["source_conflict_reason"] = entry.conflict_reason
            return result
        raise LookupError("任务运行不存在")

    def cursor(self, position: int) -> str:
        return f"{self.epoch}:{position}"

    def position(self, cursor: str | None) -> int:
        if cursor is None:
            return 0
        try:
            epoch, value = cursor.split(":")
            position = int(value)
            if epoch != self.epoch or position < 0:
                raise ValueError
            return position
        except (ValueError, TypeError):
            raise CursorExpired("事件游标已过期，请重新同步") from None

    def watermark(self) -> int:
        return self.rows("SELECT COALESCE(MAX(rowid),0) AS n FROM events")[0]["n"]

    def event_position(self, event_id: str) -> int:
        rows = self.rows("SELECT rowid AS n FROM events WHERE id=?", (event_id,))
        return rows[0]["n"] if rows else 0

    def events(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        cursor: str | None = None,
        through: str | None = None,
        limit: int = 200,
        children: bool = True,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        clause, args = self.scope(session_id, run_id, children)
        if tool_call_id is not None:
            clause = f"({clause}) AND json_extract(e.payload_json,'$.tool_call_id')=?"
            args.append(tool_call_id)
        after = self.position(cursor)
        boundary = self.position(through) if through else self.watermark()
        if after > boundary or boundary > self.watermark():
            raise CursorExpired("事件游标已过期，请重新同步")
        rows = self.rows(
            f"SELECT e.rowid AS position,e.* FROM events e WHERE ({clause}) "
            "AND e.rowid>? AND e.rowid<=? ORDER BY e.rowid LIMIT ?",
            (*args, after, boundary, limit + 1),
        )
        more = len(rows) > limit
        selected = rows[:limit]
        for item in selected:
            item["payload"] = json.loads(item.pop("payload_json"))
            item["cursor"] = self.cursor(item["position"])
        position = selected[-1]["position"] if more else boundary
        return {
            "events": selected,
            "cursor": self.cursor(position),
            "through": self.cursor(boundary),
            "has_more": more,
            "epoch": self.epoch,
        }

    def tool(self, run_id: str, call_id: str, *, offset: int = 0, limit: int = 16000):
        run = self.run(run_id)
        # Output is paged separately; don't load every streaming chunk into Python.
        args = (run["session_id"], run_id, call_id)
        rows = self.rows(
            """SELECT rowid AS position,id,type,timestamp,payload_json FROM events
               WHERE session_id=? AND run_id=? AND json_extract(payload_json,'$.tool_call_id')=?
               AND type!='tool.output' ORDER BY rowid""",
            args,
        )
        if not rows:
            raise LookupError("工具调用不存在")
        events = []
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json"))
            events.append(row)
        records = self.rows(
            """SELECT arguments_json,result_json,status FROM tool_runs WHERE
               session_id=? AND run_id=? AND tool_call_id=? ORDER BY id DESC LIMIT 1""",
            args,
        )
        result = (
            json.loads(records[0]["result_json"]) if records and records[0]["result_json"] else None
        )
        arguments = next(
            (e["payload"].get("arguments", {}) for e in events if e["type"] == "tool.requested"), {}
        )
        completed = next(
            (e["payload"] for e in reversed(events) if e["type"] == "tool.completed"), {}
        )
        uniform = next((e["payload"] for e in reversed(events) if e["type"] == "tool.result"), {})
        completed = {**uniform, **completed}
        reference = completed.get("context_ref")
        output = result.get("output", "") if result else completed.get("output_excerpt", "")
        if reference:
            try:
                output_page = self.blob(run_id, reference, offset=offset, limit=limit)
            except LookupError:
                output_page = None
        else:
            output_page = None
        if output_page is None:
            output_page = {
                "content": output[offset : offset + limit],
                "offset": offset,
                "next_offset": min(len(output), offset + limit),
                "eof": offset + limit >= len(output),
            }
        return {
            "run_id": run_id,
            "tool_call_id": call_id,
            "arguments": arguments,
            "result": result,
            "completed": completed,
            "execution": uniform,
            "events": events,
            "output": output_page,
            "context_ref": reference,
            "full_output_available": bool(result) or bool(output_page.get("reference")),
            "source_truncated": bool(completed.get("truncated") or (result or {}).get("truncated")),
        }

    def blob(self, run_id: str, reference: str, *, offset: int = 0, limit: int = 16000):
        run = self.run(run_id)
        if not reference.startswith("blob:"):
            reference = "blob:" + reference
        # Content-addressed blobs may originate in another Run; require an explicit
        # reference in this Run as well as session access before serving them.
        rows = self.rows(
            """SELECT b.id AS reference,b.media_type,b.byte_count,b.sha256,
                substr(CAST(b.content AS TEXT),?,?) AS content,
                length(CAST(b.content AS TEXT)) AS length
               FROM context_blobs b JOIN context_blob_access a ON a.blob_id=b.id
               WHERE b.id=? AND a.session_id=? AND (b.run_id=? OR EXISTS
                 (SELECT 1 FROM events e WHERE e.run_id=? AND e.session_id=?
                  AND json_extract(e.payload_json,'$.context_ref')=b.id))""",
            (offset + 1, limit, reference, run["session_id"], run_id, run_id, run["session_id"]),
        )
        if not rows:
            raise LookupError("完整输出不可用或不属于该任务")
        result = rows[0]
        result.update(
            offset=offset,
            next_offset=offset + len(result["content"]),
            eof=offset + len(result["content"]) >= result["length"],
        )
        return result

    def children(self, run_id: str):
        self.run(run_id)
        tasks = self.rows(
            "SELECT id FROM agent_tasks WHERE parent_run_id=? ORDER BY created_at", (run_id,)
        )
        result = []
        for item in tasks:
            task = self.store.get_agent_task(item["id"])
            task["runs"] = self.runs(task["child_session_id"], limit=200)
            result.append(task)
        return result
