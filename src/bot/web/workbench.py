"""Application-owned task control, independent of any browser connection."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot.core.approval import ApprovalResponse
from bot.core.events import AgentEvent, EventType
from bot.core.models import RunRequest, RunResult
from bot.web.artifacts import ArtifactStore
from bot.web.queries import WebQueries
from bot.web.sink import WebSocketEventSink

logger = logging.getLogger(__name__)


@dataclass
class PendingApproval:
    action: Any
    decision: Any
    future: asyncio.Future[ApprovalResponse]


class WebApprovalHandler:
    def __init__(self) -> None:
        self.pending: dict[str, PendingApproval] = {}

    async def approve(self, action, decision) -> ApprovalResponse:
        if not action.approval_id or not action.run_id or not action.session_id:
            raise ValueError("Web 审批缺少调用关联信息")
        future = asyncio.get_running_loop().create_future()
        self.pending[action.approval_id] = PendingApproval(action, decision, future)
        try:
            return await future
        finally:
            self.pending.pop(action.approval_id, None)

    def resolve(self, approval_id: str, *, session_id: str, run_id: str, approved: bool) -> bool:
        pending = self.pending.get(approval_id)
        if pending is None or pending.future.done():
            return False
        action = pending.action
        if action.session_id != session_id or action.run_id != run_id:
            return False
        pending.future.set_result(ApprovalResponse(approved=approved))
        return True


class WebWorkbench:
    def __init__(self, runtime, sink: WebSocketEventSink, approvals: WebApprovalHandler):
        self.runtime = runtime
        self.sink = sink
        self.approvals = approvals
        self.queries = WebQueries(runtime.store, runtime.workspace)
        self.artifacts = ArtifactStore(
            runtime.store.path.parent / "web-artifacts",
            runtime.runner.redactor,
            runtime.runner.denied_tool_paths,
        )
        self.tasks: dict[str, tuple[str, asyncio.Task]] = {}
        self.cancelling: set[str] = set()
        self.refreshes: dict[str, asyncio.Task] = {}
        self.monitors: dict[str, asyncio.Task] = {}
        self.artifact_locks: dict[str, asyncio.Lock] = {}
        self.sink.enrich = self.enrich
        self.closed = False

    def enrich(self, event: AgentEvent):
        position = self.queries.event_position(event.id)
        return {
            **event.model_dump(mode="json"),
            "position": position,
            "cursor": self.queries.cursor(position),
        }

    def related(self, event: AgentEvent, session_id: str, run_id: str | None = None):
        if event.session_id == session_id:
            if not run_id or event.run_id == run_id:
                return True
            if event.run_id.startswith("subagent:"):
                task = self.runtime.store.get_agent_task(event.run_id.removeprefix("subagent:"))
                return bool(task and task["parent_run_id"] == run_id)
            return False
        task = self.runtime.store.get_agent_task_by_child_session(event.session_id)
        return bool(
            task
            and task["parent_session_id"] == session_id
            and (not run_id or task["parent_run_id"] == run_id)
        )

    def describe(self, run_id: str):
        run = self.queries.run(run_id)
        active = self.tasks.get(run["session_id"])
        run["controllable"] = bool(active and active[0] == run_id and not active[1].done())
        run["cancelling"] = run_id in self.cancelling
        run["live"] = self.runtime.runner.is_session_running(run["session_id"])
        return run

    async def start(self, session_id: str, prompt: str, skills: list[str], request_id: str):
        self.queries.session(session_id)
        run_id = hashlib.sha256(f"{session_id}:{request_id}".encode()).hexdigest()[:32]
        previous = self.queries.rows("SELECT id FROM runs WHERE id=?", (run_id,))
        active = self.tasks.get(session_id)
        if previous or (active and active[0] == run_id):
            return {"run_id": run_id, "session_id": session_id, "duplicate": True}
        if active and not active[1].done() or self.runtime.runner.is_session_running(session_id):
            raise ValueError("该会话已有运行中的任务，请补充要求或等待结束")
        unknown = set(skills) - self.runtime.catalog.skills.keys()
        if unknown:
            raise ValueError("未知 Skill: " + ", ".join(sorted(unknown)))
        request = RunRequest(
            prompt=prompt, session_id=session_id, run_id=run_id, explicit_skills=skills
        )
        task = asyncio.create_task(self._execute(request), name=f"web-run:{run_id}")
        self.tasks[session_id] = (run_id, task)
        return {"run_id": run_id, "session_id": session_id, "duplicate": False}

    async def _capture(self, run_id: str, workspace: Path, *, initial: bool = False):
        lock = self.artifact_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            capture = asyncio.create_task(
                asyncio.to_thread(self.artifacts.capture, run_id, workspace, initial=initial)
            )
            try:
                await asyncio.shield(capture)
                return True
            except asyncio.CancelledError:
                # Keep the lock until the file writer actually stops. Cancelling
                # to_thread alone leaves a live writer racing the final snapshot.
                await capture
                raise
            except Exception:
                logger.exception("Cannot capture workspace artifacts for %s", run_id)
                self.artifacts.failed(run_id)
                return False

    async def _execute(self, request: RunRequest):
        try:
            await self._capture(request.run_id, self.runtime.workspace, initial=True)
            await self.runtime.runner.run(request)
        except BaseException as exc:
            # Also settle failures/cancellation during startup, before AgentRunner
            # reaches its normal exception boundary.
            cancelled = isinstance(exc, asyncio.CancelledError)
            if not self.queries.rows("SELECT id FROM runs WHERE id=?", (request.run_id,)):
                self.runtime.store.start_run(request.session_id, request.run_id)
            result = RunResult(
                session_id=request.session_id,
                status="cancelled" if cancelled else "failed",
                error="任务在启动时已停止" if cancelled else str(exc),
            )
            self.runtime.store.finish_run(request.run_id, result.status, result.error)
            await self.runtime.runner.event_bus.emit(
                EventType.RUN_FINISHED,
                session_id=request.session_id,
                run_id=request.run_id,
                payload=result.model_dump(mode="json"),
            )
            if not cancelled:
                logger.exception("Web task failed")
        finally:
            captured = await self._capture(request.run_id, self.runtime.workspace)
            await self.runtime.runner.event_bus.emit(
                EventType.RUN_ARTIFACTS_UPDATED,
                session_id=request.session_id,
                run_id=request.run_id,
                payload={"available": bool(captured)},
            )
            self.cancelling.discard(request.run_id)
            active = self.tasks.get(request.session_id)
            if active and active[0] == request.run_id:
                self.tasks.pop(request.session_id, None)

    async def steer(self, session_id: str, run_id: str, text: str, message_id: str):
        run = self.queries.run(run_id)
        if run["session_id"] != session_id:
            raise LookupError("任务不属于当前会话")
        previous = self.queries.rows(
            "SELECT id FROM events WHERE run_id=? AND type='run.steer.queued' "
            "AND json_extract(payload_json,'$.message_id')=?",
            (run_id, message_id),
        )
        if previous:
            return True
        if not self.runtime.runner.is_session_running(session_id) or run_id in self.cancelling:
            return False
        # The acknowledgement is durable and can be recovered on another device.
        await self.runtime.runner.event_bus.emit(
            EventType.RUN_STEER_QUEUED,
            session_id=session_id,
            run_id=run_id,
            payload={"text": text, "message_id": message_id},
        )
        return await self.runtime.runner.steer(session_id, text, message_id=message_id)

    async def cancel(self, session_id: str, run_id: str):
        self.queries.session(session_id)
        active = self.tasks.get(session_id)
        if not active or active[0] != run_id or active[1].done():
            return False
        if run_id in self.cancelling:
            return True
        self.cancelling.add(run_id)
        # Startup may not yet have a runs row, but the session already exists.
        await self.runtime.runner.event_bus.emit(
            EventType.RUN_CANCEL_REQUESTED,
            session_id=session_id,
            run_id=run_id,
            payload={"scope": "run_processes_and_children"},
        )
        active[1].cancel()
        return True

    async def publish(self, event: AgentEvent):
        process_id = event.payload.get("process_id")
        if event.type == EventType.TOOL_RESULT and process_id and process_id not in self.monitors:
            self.monitors[process_id] = asyncio.create_task(
                self._monitor_process(event, process_id)
            )
        if event.type == EventType.RUN_STARTED:
            session = self.runtime.store.get_session(event.session_id)
            if session and event.session_id not in self.tasks:
                await self._capture(event.run_id, Path(session["workspace"]), initial=True)
        elif event.type in {
            EventType.TOOL_COMPLETED,
            EventType.TOOL_RESULT,
            EventType.RUN_FINISHED,
        }:
            # Debounce full workspace scans while retaining the initial snapshot.
            previous = self.refreshes.get(event.run_id)
            if previous and not previous.done():
                return

            async def refresh():
                await asyncio.sleep(0.2)
                session = self.runtime.store.get_session(event.session_id)
                if session:
                    captured = await self._capture(event.run_id, Path(session["workspace"]))
                    await self.runtime.runner.event_bus.emit(
                        EventType.RUN_ARTIFACTS_UPDATED,
                        session_id=event.session_id,
                        run_id=event.run_id,
                        payload={"available": bool(captured)},
                    )

            self.refreshes[event.run_id] = asyncio.create_task(refresh())

    async def _monitor_process(self, event: AgentEvent, process_id: str):
        offsets = {"stdout": 0, "stderr": 0}
        previous_status = None
        try:
            while not self.closed:
                page = await self.runtime.target.read_process_output(
                    process_id,
                    stdout_offset=offsets["stdout"],
                    stderr_offset=offsets["stderr"],
                )
                snapshot = page["snapshot"]
                if snapshot["run_id"] != event.run_id or snapshot["session_id"] != event.session_id:
                    return
                for stream in ("stdout", "stderr"):
                    if page[stream]:
                        await self.runtime.runner.event_bus.emit(
                            EventType.PROCESS_OUTPUT,
                            session_id=event.session_id,
                            run_id=event.run_id,
                            payload={
                                "process_id": process_id,
                                "stream": stream,
                                "offset": offsets[stream],
                                "data": page[stream],
                            },
                        )
                        offsets[stream] += len(page[stream])
                if snapshot["status"] != previous_status:
                    await self.runtime.runner.event_bus.emit(
                        EventType.PROCESS_UPDATED,
                        session_id=event.session_id,
                        run_id=event.run_id,
                        payload=snapshot,
                    )
                    previous_status = snapshot["status"]
                caught_up = all(offsets[s] >= page[s + "_length"] for s in offsets)
                if caught_up and snapshot["status"] not in {"running", "terminating"}:
                    return
                await asyncio.sleep(0.3 if caught_up else 0)
        except (ValueError, KeyError, NotImplementedError):
            # Legacy/external targets may have no managed-process log facility.
            return

    async def close(self):
        self.closed = True
        tasks = [task for _, task in self.tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        monitors = [task for task in self.monitors.values() if not task.done()]
        for task in monitors:
            task.cancel()
        if monitors:
            await asyncio.gather(*monitors, return_exceptions=True)
        refreshes = [task for task in self.refreshes.values() if not task.done()]
        if refreshes:
            await asyncio.gather(*refreshes, return_exceptions=True)
