"""FastAPI workbench for live tasks, durable history and workspace artifacts."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote, urlparse
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bot.cli.runtime import build_runtime
from bot.core.events import CallbackEventSink
from bot.web.analysis import summarize as summarize_analyses
from bot.web.queries import CursorExpired
from bot.web.sink import WebSocketEventSink
from bot.web.sorting import sort_analyses
from bot.web.workbench import WebApprovalHandler, WebWorkbench

try:
    from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse, JSONResponse, Response
    from fastapi.staticfiles import StaticFiles
except ImportError:
    raise ImportError("Web UI 需要额外依赖。请安装: pip install 'kunpeng-cli-agent[web]'") from None

_STATIC_DIR = Path(__file__).resolve().parent / "static"
PageSize = Annotated[int, Query(ge=1, le=500)]
Offset = Annotated[int, Query(ge=0)]
OutputLimit = Annotated[int, Query(ge=1, le=200000)]

# Shown in the UI and embedded in exports so the numbers are never ambiguous.
ANALYSIS_METHODOLOGY = {
    "total_seconds": "总耗时 = runs.completed_at - runs.started_at；缺少结束时间时记为未知。",
    "approval_seconds": (
        "审批等待 = approval.requested 到配对 approval.resolved 的区间并集；"
        "重叠区间合并，同一秒不会重复扣除。"
    ),
    "net_seconds": "净耗时 = 总耗时 - 审批等待并集，下限为 0。",
    "unpaired": "没有配对结果的审批请求只计数，不扣除：等待时长未知，不等于 0。",
    "duplicates": "重复的审批事件按 approval_id 去重，只保留首个请求与首个后续结果。",
    "unknown_gaps": (
        "事件之间超过阈值的空档标记为“未知”，仅提示排查，不从净耗时中扣除。"
    ),
    "stop_reason": (
        "停止原因优先取 run.cancelled/blocked/limit_reached/failed 的 termination_reason，"
        "否则取 run.finished 的 termination_reason；都没有时记为 unknown。"
    ),
    "scope": "统计范围限定当前工作区，且只统计主会话运行，不含子任务运行。",
}


def _delta(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return right - left


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=200000)
    skills: list[str] = Field(default_factory=list, max_length=100)
    request_id: str = Field(min_length=1, max_length=128)


class SteerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=200000)
    message_id: str = Field(min_length=1, max_length=128)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    run_id: str
    approved: bool = Field(strict=True)


def create_app(workspace: Path | None = None, config_path: Path | None = None) -> FastAPI:
    workspace = (workspace or Path.cwd()).resolve()
    sink = WebSocketEventSink()
    approvals = WebApprovalHandler()
    workbench: WebWorkbench | None = None

    async def observe(event):
        if workbench is not None:
            await workbench.publish(event)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal workbench
        runtime = build_runtime(
            workspace=workspace,
            config_path=config_path,
            event_sinks=[sink, CallbackEventSink(observe)],
            approval_handler=approvals,
        )
        workbench = WebWorkbench(runtime, sink, approvals)
        app.state.workbench = workbench
        if runtime.config.subagents.enabled:
            await runtime.subagents.start()
        try:
            yield
        finally:
            await workbench.close()
            await runtime.aclose()

    app = FastAPI(title="Bot 实时任务工作台", version="0.2.0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    def wb() -> WebWorkbench:
        if workbench is None:
            raise HTTPException(503, "Runtime not ready")
        return workbench

    def same_origin(origin: str | None, host: str | None) -> bool:
        return origin is None or urlparse(origin).netloc == host

    @app.middleware("http")
    async def browser_boundary(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not same_origin(
            request.headers.get("origin"), request.headers.get("host")
        ):
            return JSONResponse({"detail": "拒绝跨来源任务控制请求"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' blob: data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @app.exception_handler(CursorExpired)
    async def expired(request, exc):
        return JSONResponse({"detail": str(exc), "code": "cursor_expired"}, status_code=409)

    @app.exception_handler(LookupError)
    async def missing(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse((_STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    @app.get("/api/status")
    async def status(session_id: str | None = None):
        service = wb()
        rt = service.runtime
        return {
            "version": "0.2.0",
            "workspace": str(rt.workspace),
            "model": rt.config.model.name,
            "base_url": rt.config.model.base_url,
            "permission_mode": rt.config.permissions.mode,
            "auto_approve": rt.config.permissions.auto_approve,
            "active_skills": rt.runner.active_skill_names(session_id),
            "subagents_enabled": rt.config.subagents.enabled,
            "epoch": service.queries.epoch,
        }

    @app.get("/api/sessions")
    async def sessions(limit: PageSize = 50, offset: Offset = 0):
        service = wb()
        result = service.queries.sessions(limit=limit, offset=offset)
        for item in result:
            active = service.tasks.get(item["id"])
            item["active_run_id"] = active[0] if active and not active[1].done() else None
        return result

    @app.post("/api/sessions")
    async def create_session():
        return {"session_id": wb().runtime.store.create_session(workspace)}

    @app.get("/api/sessions/{session_id}")
    async def session_detail(session_id: str):
        service = wb()
        session = service.queries.session(session_id)
        return {
            **session,
            "session_id": session_id,
            "messages": [
                m.model_dump(mode="json") for m in service.runtime.store.load_messages(session_id)
            ],
            "usage": service.runtime.store.session_usage(session_id),
            "plan": service.runtime.store.load_plan(session_id),
        }

    @app.get("/api/sessions/{session_id}/runs")
    async def runs(session_id: str, limit: PageSize = 50, offset: Offset = 0):
        service = wb()
        return [
            service.describe(run["id"])
            for run in service.queries.runs(session_id, limit=limit, offset=offset)
        ]

    @app.post("/api/sessions/{session_id}/runs", status_code=202)
    async def start_run(session_id: str, request: StartRequest):
        if not request.prompt.strip():
            raise ValueError("任务要求不能为空")
        return await wb().start(
            session_id, request.prompt.strip(), request.skills, request.request_id
        )

    @app.get("/api/analysis/runs")
    async def analysis_runs(
        limit: PageSize = 200,
        offset: Offset = 0,
        status: str | None = None,
        stop_reason: str | None = None,
        session_id: str | None = None,
        search: str | None = None,
        since: str | None = None,
        until: str | None = None,
        source_kind: Literal["main", "benchmark", "artifact"] | None = None,
        sort: Literal["net", "total", "approval", "started", "events"] = "started",
        order: Literal["asc", "desc"] = "desc",
        gap_threshold_seconds: Annotated[float, Query(ge=0, le=86400)] = 120.0,
    ):
        """Cross-session run list with net duration, filters and stop reasons.

        Covers every discovered history database (main, benchmarks, artifacts),
        de-duplicated by run id. Sorting is applied to the whole filtered set
        before pagination, so an early long run cannot be dropped by the page
        window.
        """
        service = wb()
        items = service.queries.analysis_all_runs(
            status=status,
            stop_reason=stop_reason,
            session_id=session_id,
            search=search,
            since=since,
            until=until,
            source_kind=source_kind,
            gap_threshold_seconds=gap_threshold_seconds,
        )
        ordered = sort_analyses(items, sort=sort, order=order)
        total = len(ordered)
        page = ordered[offset : offset + limit]
        sources = service.queries.analysis_sources()
        return {
            "runs": page,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < total,
            "sort": sort,
            "order": order,
            "summary": summarize_analyses(ordered),
            "summary_scope": "all_filtered",
            "sources": {
                **sources["summary"],
                "problems": [problem.as_dict() for problem in sources["problems"]],
            },
            "methodology": ANALYSIS_METHODOLOGY,
        }

    @app.get("/api/analysis/runs/{run_id}")
    async def analysis_run_detail(
        run_id: str,
        gap_threshold_seconds: Annotated[float, Query(ge=0, le=86400)] = 120.0,
    ):
        """Timing breakdown for one run, with approval intervals and unknown gaps.

        The run is looked up across every discovered source, so a benchmark or
        archived run can be inspected even though it is not in the live database.
        """
        service = wb()
        detail = service.queries.analysis_run_any(
            run_id, gap_threshold_seconds=gap_threshold_seconds
        )
        detail["methodology"] = ANALYSIS_METHODOLOGY
        return detail

    @app.get("/api/analysis/compare")
    async def analysis_compare(
        left: str,
        right: str,
        gap_threshold_seconds: Annotated[float, Query(ge=0, le=86400)] = 120.0,
    ):
        """Compare two runs on total/net/approval duration and stop reason."""
        service = wb()
        left_run = service.queries.analysis_run_any(
            left, gap_threshold_seconds=gap_threshold_seconds
        )
        right_run = service.queries.analysis_run_any(
            right, gap_threshold_seconds=gap_threshold_seconds
        )
        return {
            "left": left_run,
            "right": right_run,
            "delta": {
                "total_seconds": _delta(left_run["total_seconds"], right_run["total_seconds"]),
                "net_seconds": _delta(left_run["net_seconds"], right_run["net_seconds"]),
                "approval_seconds": _delta(
                    left_run["approval_seconds"], right_run["approval_seconds"]
                ),
                "event_count": right_run["event_count"] - left_run["event_count"],
            },
            "same_stop_reason": left_run["stop_reason"] == right_run["stop_reason"],
            "methodology": ANALYSIS_METHODOLOGY,
        }

    @app.get("/api/analysis/export")
    async def analysis_export(
        status: str | None = None,
        stop_reason: str | None = None,
        session_id: str | None = None,
        search: str | None = None,
        since: str | None = None,
        until: str | None = None,
        source_kind: Literal["main", "benchmark", "artifact"] | None = None,
        sort: Literal["net", "total", "approval", "started", "events"] = "started",
        order: Literal["asc", "desc"] = "desc",
        gap_threshold_seconds: Annotated[float, Query(ge=0, le=86400)] = 120.0,
    ):
        """Download the current analysis view as a JSON report.

        The export uses the same filter set and the same whole-set ordering as the
        list endpoint, and records the source, de-duplication and scope metadata
        so the numbers can be reproduced.
        """
        service = wb()
        items = service.queries.analysis_all_runs(
            status=status,
            stop_reason=stop_reason,
            session_id=session_id,
            search=search,
            since=since,
            until=until,
            source_kind=source_kind,
            gap_threshold_seconds=gap_threshold_seconds,
        )
        runs = sort_analyses(items, sort=sort, order=order)
        sources = service.queries.analysis_sources()
        report = {
            "schema": "bot.run-analysis.v1",
            "generated_at": datetime.now(UTC).isoformat(),
            "workspace": str(service.runtime.workspace),
            "filters": {
                "status": status,
                "stop_reason": stop_reason,
                "session_id": session_id,
                "search": search,
                "since": since,
                "until": until,
                "source_kind": source_kind,
                "sort": sort,
                "order": order,
                "gap_threshold_seconds": gap_threshold_seconds,
            },
            "scope": {
                "summary_scope": "all_filtered",
                "exported_runs": len(runs),
                "note": "汇总与导出均覆盖全部筛选结果，不受分页影响。",
            },
            "sources": {
                **sources["summary"],
                "problems": [problem.as_dict() for problem in sources["problems"]],
            },
            "summary": summarize_analyses(runs),
            "methodology": ANALYSIS_METHODOLOGY,
            "runs": runs,
        }
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        return Response(
            content=json.dumps(report, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="run-analysis-{stamp}.json"'
            },
        )

    @app.get("/api/runs/{run_id}")
    async def run_detail(run_id: str):
        return wb().describe(run_id)

    @app.get("/api/runs/{run_id}/events")
    async def events(
        run_id: str,
        cursor: str | None = None,
        through: str | None = None,
        limit: PageSize = 200,
        children: bool = True,
    ):
        queries = wb().queries
        run = queries.run(run_id)
        return queries.events(
            run["session_id"],
            run_id=run_id,
            cursor=cursor,
            through=through,
            limit=limit,
            children=children,
        )

    @app.get("/api/sessions/{session_id}/events")
    async def session_events(
        session_id: str,
        cursor: str | None = None,
        through: str | None = None,
        limit: PageSize = 200,
    ):
        return wb().queries.events(session_id, cursor=cursor, through=through, limit=limit)

    @app.get("/api/runs/{run_id}/tools/{call_id}")
    async def tool(run_id: str, call_id: str, offset: Offset = 0, limit: OutputLimit = 16000):
        return wb().queries.tool(run_id, call_id, offset=offset, limit=limit)

    @app.get("/api/runs/{run_id}/tools/{call_id}/events")
    async def tool_events(
        run_id: str,
        call_id: str,
        cursor: str | None = None,
        through: str | None = None,
        limit: PageSize = 50,
    ):
        queries = wb().queries
        run = queries.run(run_id)
        return queries.events(
            run["session_id"],
            run_id=run_id,
            cursor=cursor,
            through=through,
            limit=limit,
            children=False,
            tool_call_id=call_id,
        )

    @app.get("/api/runs/{run_id}/blobs/{blob_id}")
    async def blob(run_id: str, blob_id: str, offset: Offset = 0, limit: OutputLimit = 16000):
        return wb().queries.blob(run_id, blob_id, offset=offset, limit=limit)

    @app.get("/api/runs/{run_id}/children")
    async def children(run_id: str):
        return wb().queries.children(run_id)

    @app.get("/api/runs/{run_id}/processes")
    async def processes(run_id: str):
        service = wb()
        run = service.queries.run(run_id)
        return [
            service.runtime.runner.redactor.redact(p.model_dump(mode="json"))
            for p in await service.runtime.target.list_processes()
            if p.run_id == run_id and p.session_id == run["session_id"]
        ]

    @app.post("/api/runs/{run_id}/steer", status_code=202)
    async def steer(run_id: str, request: SteerRequest):
        service = wb()
        run = service.queries.run(run_id)
        if not request.text.strip():
            raise ValueError("补充要求不能为空")
        accepted = await service.steer(
            run["session_id"], run_id, request.text.strip(), request.message_id
        )
        if not accepted:
            raise HTTPException(409, "任务已结束或正在停止，补充要求未接收")
        return {"accepted": True, "message_id": request.message_id}

    @app.post("/api/runs/{run_id}/cancel", status_code=202)
    async def cancel(run_id: str):
        service = wb()
        run = service.queries.run(run_id)
        if not await service.cancel(run["session_id"], run_id):
            raise HTTPException(409, "任务已经结束或不由此服务器控制")
        return {"accepted": True, "status": "cancelling"}

    @app.get("/api/sessions/{session_id}/approvals")
    async def pending_approvals(session_id: str):
        service = wb()
        service.queries.session(session_id)
        result = []
        for approval_id, pending in approvals.pending.items():
            action = pending.action
            task = service.runtime.store.get_agent_task_by_child_session(action.session_id)
            if action.session_id == session_id or task and task["parent_session_id"] == session_id:
                result.append(
                    service.runtime.runner.redactor.redact(
                        {
                            "approval_id": approval_id,
                            "session_id": action.session_id,
                            "run_id": action.run_id,
                            "tool_call_id": action.tool_call_id,
                            "name": action.tool_name,
                            "arguments": action.arguments,
                            "reason": pending.decision.reason,
                        }
                    )
                )
        return result

    @app.post("/api/approvals/{approval_id}")
    async def resolve_approval(approval_id: str, request: ApprovalRequest):
        service = wb()
        run = service.queries.run(request.run_id)
        if run["session_id"] != request.session_id or not approvals.resolve(
            approval_id,
            session_id=request.session_id,
            run_id=request.run_id,
            approved=request.approved,
        ):
            raise HTTPException(409, "审批已处理、已失效或与该调用不匹配")
        return {"accepted": True}

    @app.post("/api/sessions/{session_id}/compact")
    async def compact(session_id: str):
        service = wb()
        service.queries.session(session_id)
        return await service.runtime.runner.compact_session(session_id)

    @app.get("/api/runs/{run_id}/context")
    async def context(run_id: str):
        service = wb()
        run = service.queries.run(run_id)
        rows = service.queries.rows(
            "SELECT id,type,timestamp,payload_json FROM events WHERE run_id=? "
            "AND (type LIKE 'context.%' OR type LIKE 'memory.%' OR type='model.response') "
            "ORDER BY rowid DESC LIMIT 200",
            (run_id,),
        )
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json"))
        return {
            "events": list(reversed(rows)),
            "session_id": run["session_id"],
            "compactions": service.runtime.store.list_context_compactions(run["session_id"]),
        }

    @app.get("/api/sessions/{session_id}/compactions/{compaction_id}")
    async def compaction_source(
        session_id: str, compaction_id: str, after: Offset = 0, limit: PageSize = 50
    ):
        service = wb()
        service.queries.session(session_id)
        record = service.runtime.store.get_context_compaction(session_id, compaction_id)
        if record is None:
            raise LookupError("上下文版本不存在")
        start = max(record["covered_start_position"], after + 1)
        end = min(record["covered_end_position"], start + limit - 1)
        if start > end:
            return {"compaction": record, "messages": [], "eof": True}
        result = service.runtime.store.read_context_compaction_source(
            requesting_session_id=session_id,
            compaction_id=compaction_id,
            start_position=start,
            end_position=end,
        )
        return {**result, "eof": end >= record["covered_end_position"], "next_position": end}

    @app.get("/api/runs/{run_id}/artifacts")
    async def artifacts(run_id: str):
        service = wb()
        service.queries.run(run_id)
        return await asyncio.to_thread(service.artifacts.listing, run_id)

    @app.get("/api/runs/{run_id}/artifacts/{artifact_id}")
    async def artifact(run_id: str, artifact_id: str):
        service = wb()
        service.queries.run(run_id)
        return await asyncio.to_thread(service.artifacts.detail, run_id, artifact_id)

    @app.get("/api/runs/{run_id}/artifacts/{artifact_id}/content")
    async def artifact_content(
        run_id: str,
        artifact_id: str,
        side: Literal["before", "after"] = "after",
        download: bool = False,
    ):
        service = wb()
        service.queries.run(run_id)
        record, content = await asyncio.to_thread(
            service.artifacts.content, run_id, artifact_id, side
        )
        item = record[side]
        media_type = (
            item["media_type"]
            if item["media_type"] in {"image/png", "image/jpeg", "image/gif", "image/webp"}
            else "text/plain"
            if item["text"]
            else "application/octet-stream"
        )
        disposition = (
            "attachment" if download or media_type == "application/octet-stream" else "inline"
        )
        filename = quote(Path(record["path"]).name, safe="")
        return Response(
            content,
            media_type=media_type,
            headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{filename}"},
        )

    @app.get("/api/skills")
    async def skills():
        return [
            {"name": skill.name, "description": skill.description}
            for skill in wb().runtime.catalog.skills.values()
        ]

    @app.get("/api/tools")
    async def tools():
        return wb().runtime.tools.names()

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        if not same_origin(ws.headers.get("origin"), ws.headers.get("host")):
            await ws.close(code=1008)
            return
        await ws.accept()
        service = wb()
        queue = sink.subscribe()
        drainer = None
        session_id = None
        write_lock = asyncio.Lock()

        async def send(data):
            async with write_lock:
                await ws.send_json(data)

        async def drain(subscription_id):
            while True:
                event = json.loads(await queue.get())
                await send({"type": "event", "subscription_id": subscription_id, "event": event})

        async def subscribe(message):
            nonlocal drainer, session_id
            selected = message.get("session_id")
            run_id = message.get("run_id") or None
            service.queries.scope(selected, run_id)
            service.queries.position(message.get("cursor"))
            if drainer:
                drainer.cancel()
                with suppress(asyncio.CancelledError, WebSocketDisconnect):
                    await drainer
            session_id = selected
            subscription_id = str(message.get("subscription_id") or uuid4().hex)
            sink.configure(queue, lambda event: service.related(event, selected, run_id))
            cursor = message.get("cursor")
            through = service.queries.cursor(service.queries.watermark())
            await send(
                {
                    "type": "subscribed",
                    "subscription_id": subscription_id,
                    "session_id": selected,
                    "run_id": run_id,
                    "epoch": service.queries.epoch,
                }
            )
            while True:
                page = service.queries.events(
                    selected, run_id=run_id, cursor=cursor, through=through, limit=500
                )
                await send({"type": "snapshot", "subscription_id": subscription_id, **page})
                cursor = page["cursor"]
                if not page["has_more"]:
                    break
            await send(
                {"type": "snapshot_complete", "subscription_id": subscription_id, "cursor": cursor}
            )
            drainer = asyncio.create_task(drain(subscription_id))

        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                    if not isinstance(msg, dict):
                        raise ValueError("消息必须是 JSON 对象")
                    kind = msg.get("type")
                    if kind == "ping":
                        await send({"type": "pong"})
                    elif kind in {"create_session", "new_session"}:
                        session_id = service.runtime.store.create_session(workspace)
                        await send(
                            {"type": "session_created", "session_id": session_id, "plan": None}
                        )
                    elif kind in {"subscribe", "resume_session"}:
                        await subscribe(msg)
                    else:
                        raise ValueError("未知消息类型；任务控制请使用对应 API")
                except (ValueError, LookupError, ValidationError) as exc:
                    await send(
                        {
                            "type": "error",
                            "message": str(exc),
                            "code": "cursor_expired"
                            if isinstance(exc, CursorExpired)
                            else "invalid_request",
                        }
                    )
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            sink.unsubscribe(queue)
            if drainer:
                drainer.cancel()
                with suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                    await drainer
            # A browser connection does not own (or cancel) any running task.

    return app
