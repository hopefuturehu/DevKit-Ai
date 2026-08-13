"""FastAPI web server exposing the bot CLI agent as a chat interface.

Start with::

    bot web                    # default http://0.0.0.0:8080
    bot web --port 9090        # custom port
    bot web --host 127.0.0.1   # localhost only
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from bot.cli.runtime import build_runtime
from bot.core.approval import ApprovalResponse
from bot.core.models import RunRequest
from bot.web.sink import WebSocketEventSink

logger = logging.getLogger(__name__)

try:
    from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse
except ImportError:
    raise ImportError(
        "Web UI 需要额外依赖。请安装: pip install fastapi uvicorn"
    ) from None

_STATIC_DIR = Path(__file__).resolve().parent / "static"


class WebApprovalHandler:
    """Resolves pending tool approvals from browser decisions.

    Implements the ``ApprovalHandler`` protocol: ``approve()`` blocks until a
    decision arrives, while the WebSocket handler calls ``resolve_next()`` when
    the browser sends an ``approval`` message.
    """

    def __init__(self) -> None:
        self._pending: asyncio.Queue[asyncio.Future[ApprovalResponse]] = asyncio.Queue()

    async def approve(self, action: Any, decision: Any) -> ApprovalResponse:
        future: asyncio.Future[ApprovalResponse] = asyncio.get_running_loop().create_future()
        await self._pending.put(future)
        return await future

    def resolve_next(self, approved: bool) -> bool:
        try:
            future = self._pending.get_nowait()
        except asyncio.QueueEmpty:
            return False
        if not future.done():
            future.set_result(ApprovalResponse(approved=approved))
        return True


def create_app(
    workspace: Path | None = None,
    config_path: Path | None = None,
) -> FastAPI:
    """Build the FastAPI application with all routes."""

    workspace = (workspace or Path.cwd()).resolve()

    # -- shared state -----------------------------------------------------------
    # Each web client gets its own session; runtime and event sink are shared.

    _runtime: Any = None
    _event_sink = WebSocketEventSink()
    _approval_handler = WebApprovalHandler()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal _runtime
        # 将 WebSocket 事件 sink 直接注册进 EventBus，而不是事后访问
        # Runtime.event_bus（Runtime 并不暴露 event_bus 字段）。
        _runtime = build_runtime(
            workspace=workspace,
            config_path=config_path,
            event_sinks=[_event_sink],
            approval_handler=_approval_handler,
        )
        if _runtime.config.subagents.enabled:
            await _runtime.subagents.start()
        try:
            yield
        finally:
            if _runtime is not None:
                await _runtime.aclose()

    app = FastAPI(
        title="Bot Web UI",
        description="Web-based interactive interface for the bot CLI agent.",
        version="0.1.0",
        lifespan=lifespan,
    )

    # -- helpers ----------------------------------------------------------------

    def _rt_ok() -> Any:
        if _runtime is None:
            raise HTTPException(503, "Runtime not ready")
        return _runtime

    # -- static ----------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        html_path = _STATIC_DIR / "index.html"
        if html_path.is_file():
            return HTMLResponse(html_path.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>Bot Web UI</h1><p>index.html not found.</p>")

    # -- REST endpoints --------------------------------------------------------

    @app.get("/api/status")
    async def api_status() -> dict[str, Any]:
        rt = _rt_ok()
        return {
            "version": "0.1.0",
            "workspace": str(rt.workspace),
            "model": rt.config.model.name,
            "base_url": rt.config.model.base_url,
            "permission_mode": rt.config.permissions.mode,
            "active_skills": list(rt.skills.active),
            "subagents_enabled": rt.config.subagents.enabled,
        }

    @app.get("/api/sessions")
    async def api_sessions() -> list[dict[str, Any]]:
        rt = _rt_ok()
        return rt.store.list_sessions()

    @app.post("/api/sessions")
    async def api_create_session() -> dict[str, str]:
        rt = _rt_ok()
        sid = rt.store.create_session(rt.workspace)
        return {"session_id": sid}

    @app.get("/api/sessions/{session_id}")
    async def api_session_detail(session_id: str) -> dict[str, Any]:
        rt = _rt_ok()
        if not rt.store.session_exists(session_id):
            raise HTTPException(404, "Session not found")
        messages = rt.store.load_messages(session_id)
        usage = rt.store.session_usage(session_id)
        return {
            "session_id": session_id,
            "messages": [m.model_dump(mode="json") for m in messages],
            "usage": usage,
        }

    @app.get("/api/skills")
    async def api_skills() -> list[dict[str, str]]:
        rt = _rt_ok()
        return [
            {"name": s.name, "description": s.description}
            for s in rt.catalog.skills.values()
        ]

    @app.get("/api/tools")
    async def api_tools() -> list[str]:
        rt = _rt_ok()
        return rt.tools.names()

    # -- WebSocket --------------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        rt = _rt_ok()

        event_queue = _event_sink.subscribe()
        session_id: str | None = None
        _run_task: asyncio.Task | None = None
        _stop_event = asyncio.Event()

        async def send_json(data: dict[str, Any]) -> None:
            try:
                await ws.send_text(json.dumps(data, ensure_ascii=False))
            except Exception:
                _stop_event.set()

        async def drain_events() -> None:
            """Continuously forward events from the shared sink to this client."""
            while not _stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(event_queue.get(), timeout=1.0)
                    await ws.send_text(raw)
                except TimeoutError:
                    continue
                except asyncio.QueueEmpty:
                    break
                except Exception:
                    break

        def run_done_cb(task: asyncio.Task) -> None:
            nonlocal _run_task
            _run_task = None

        # Start event drainer
        drainer = asyncio.ensure_future(drain_events())

        try:
            while not _stop_event.is_set():
                try:
                    raw = await ws.receive_text()
                except WebSocketDisconnect:
                    break
                except RuntimeError:
                    break

                msg: dict[str, Any] = json.loads(raw)
                msg_type = msg.get("type", "")

                if msg_type == "ping":
                    await send_json({"type": "pong"})

                elif msg_type == "create_session":
                    session_id = rt.store.create_session(rt.workspace)
                    await send_json({
                        "type": "session_created",
                        "session_id": session_id,
                    })

                elif msg_type == "resume_session":
                    requested = msg.get("session_id", "")
                    if rt.store.session_exists(requested):
                        session_id = requested
                        await send_json({
                            "type": "session_resumed",
                            "session_id": session_id,
                        })
                    else:
                        await send_json({
                            "type": "error",
                            "message": f"Session '{requested[:16]}...' not found",
                        })

                elif msg_type == "chat":
                    if not session_id:
                        session_id = rt.store.create_session(rt.workspace)
                        await send_json({
                            "type": "session_created",
                            "session_id": session_id,
                        })

                    prompt = msg.get("prompt", "").strip()
                    if not prompt:
                        await send_json({"type": "error", "message": "Empty prompt"})
                        continue

                    explicit_skills: list[str] = msg.get("skills", [])
                    if isinstance(explicit_skills, str):
                        explicit_skills = [
                            s.strip() for s in explicit_skills.split(",") if s.strip()
                        ]

                    request = RunRequest(
                        prompt=prompt,
                        session_id=session_id,
                        explicit_skills=explicit_skills,
                    )

                    async def run_and_report(req: RunRequest = request) -> None:
                        try:
                            result = await rt.runner.run(req)
                            await send_json({
                                "type": "run_result",
                                "session_id": result.session_id,
                                "status": result.status,
                                "final_text": result.final_text,
                                "steps": result.steps,
                                "error": result.error,
                                "input_tokens": result.input_tokens,
                                "output_tokens": result.output_tokens,
                                "cost_usd": result.cost_usd,
                            })
                        except Exception as exc:
                            logger.exception("Run failed")
                            await send_json({
                                "type": "error",
                                "message": str(exc),
                            })

                    _run_task = asyncio.ensure_future(run_and_report())
                    _run_task.add_done_callback(run_done_cb)

                elif msg_type == "cancel":
                    if session_id:
                        await rt.runner.steer(session_id, "/cancel")
                        await send_json({
                            "type": "cancelled",
                            "session_id": session_id,
                        })

                elif msg_type == "steer":
                    if session_id:
                        text = msg.get("text", "")
                        ok = await rt.runner.steer(session_id, text)
                        await send_json({
                            "type": "steered",
                            "accepted": ok,
                        })

                elif msg_type == "new_session":
                    session_id = rt.store.create_session(rt.workspace)
                    rt.skills.reset()
                    await send_json({
                        "type": "session_created",
                        "session_id": session_id,
                    })

                elif msg_type == "compact":
                    if session_id:
                        try:
                            result = await rt.runner.compact_session(session_id)
                            await send_json({
                                "type": "compacted",
                                "result": result,
                            })
                        except Exception as exc:
                            await send_json({
                                "type": "error",
                                "message": str(exc),
                            })

                elif msg_type == "approval":
                    decision = msg.get("decision", "deny")
                    _approval_handler.resolve_next(decision == "approve")
                    await send_json({
                        "type": "approval_resolved",
                        "decision": decision,
                    })

                else:
                    await send_json({
                        "type": "error",
                        "message": f"Unknown message type: {msg_type}",
                    })

        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("WebSocket error")
        finally:
            _stop_event.set()
            _event_sink.unsubscribe(event_queue)
            if not drainer.done():
                drainer.cancel()
                try:
                    await drainer
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            if _run_task and not _run_task.done():
                _run_task.cancel()
                try:
                    await _run_task
                except Exception:
                    pass

    return app
