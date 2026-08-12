from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import jsonschema

from bot.config.models import AppConfig
from bot.core.approval import ApprovalHandler, ApprovalResponse, DenyApprovalHandler
from bot.core.events import EventBus, EventType
from bot.core.models import RunRequest, ToolCall, ToolDefinition
from bot.execution import ExecutionTarget, ProcessEventKind, ProcessSpec
from bot.sessions import SQLiteSessionStore
from bot.subagents.models import (
    AgentResult,
    AgentSpec,
    AgentTask,
    WorkerIsolation,
    WorkerStatus,
    agent_task_from_record,
)
from bot.tools import ToolRegistry, ToolResult


class _PermitLease:
    def __init__(self, semaphore: asyncio.Semaphore) -> None:
        self._semaphore = semaphore
        self.held = False

    async def acquire(self) -> None:
        if not self.held:
            await self._semaphore.acquire()
            self.held = True

    def release(self) -> None:
        if self.held:
            self.held = False
            self._semaphore.release()


class _TaskApprovalHandler:
    def __init__(
        self,
        *,
        task: AgentTask,
        delegate: ApprovalHandler,
        store: SQLiteSessionStore,
        event_bus: EventBus,
        lease: _PermitLease,
        notify: Callable[[str], None],
    ) -> None:
        self.task = task
        self.delegate = delegate
        self.store = store
        self.event_bus = event_bus
        self.lease = lease
        self.notify = notify

    async def approve(self, action, decision) -> ApprovalResponse:
        parked = self.store.set_agent_task_waiting_approval(self.task.id)
        if parked:
            await self.event_bus.emit(
                EventType.SUBAGENT_WAITING_APPROVAL,
                session_id=self.task.parent_session_id,
                run_id=f"subagent:{self.task.id}",
                payload={
                    "task_id": self.task.id,
                    "child_session_id": self.task.child_session_id,
                    "tool": action.tool_name,
                    "reason": decision.reason,
                },
            )
            self.lease.release()
            self.notify(self.task.id)
        response = await self.delegate.approve(action, decision)
        if parked:
            await self.lease.acquire()
            resumed = self.store.resume_agent_task_after_approval(self.task.id)
            if resumed:
                await self.event_bus.emit(
                    EventType.SUBAGENT_RESUMED,
                    session_id=self.task.parent_session_id,
                    run_id=f"subagent:{self.task.id}",
                    payload={"task_id": self.task.id},
                )
                self.notify(self.task.id)
        return response


class BackgroundAgentPool:
    """Durable depth-one background subagent scheduler.

    Queued work is persisted before scheduling. Each task uses an isolated child
    session and a fresh AgentRunner supplied by ``runner_factory``.
    """

    _CONTROL_NAMES = {
        "spawn_agent",
        "get_agent_status",
        "await_agents",
        "cancel_agent",
    }

    def __init__(
        self,
        *,
        config: AppConfig,
        workspace: Path,
        store: SQLiteSessionStore,
        event_bus: EventBus,
        execution_target: ExecutionTarget,
        specs: list[AgentSpec],
        runner_factory: Callable[[AgentSpec, Path, ApprovalHandler], Any],
        approval_handler: ApprovalHandler | None = None,
    ) -> None:
        self.config = config
        self.workspace = workspace.resolve()
        self.store = store
        self.event_bus = event_bus
        self.execution_target = execution_target
        self.specs = {spec.name: spec for spec in specs}
        if not self.specs:
            raise ValueError("至少需要一个子 Agent profile")
        self.runner_factory = runner_factory
        self.approval_handler = approval_handler or DenyApprovalHandler()
        self.owner_id = uuid4().hex
        self._semaphore = asyncio.Semaphore(config.subagents.max_concurrent)
        self._scheduled: dict[str, asyncio.Task[None]] = {}
        self._created_task_ids: set[str] = set()
        self._changed: dict[str, asyncio.Event] = {}
        self._start_lock = asyncio.Lock()
        self._started = False
        self._closing = False
        self._definitions = self._build_definitions()

    def definitions(self) -> list[ToolDefinition]:
        return list(self._definitions.values())

    @property
    def has_live_tasks(self) -> bool:
        return any(not task.done() for task in self._scheduled.values())

    async def start(self) -> None:
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            if self._closing:
                raise RuntimeError("BackgroundAgentPool 已关闭")
            interrupted = self.store.interrupt_recoverable_agent_tasks(workspace=self.workspace)
            for task_id in interrupted:
                record = self.store.get_agent_task(task_id)
                if record is not None:
                    await self.event_bus.emit(
                        EventType.SUBAGENT_INTERRUPTED,
                        session_id=str(record["parent_session_id"]),
                        run_id=f"subagent:{task_id}",
                        payload={
                            "task_id": task_id,
                            "child_session_id": record["child_session_id"],
                            "error": record["error"],
                        },
                    )
                self._notify(task_id)
            queued = self.store.list_agent_tasks(
                workspace=self.workspace,
                statuses=[WorkerStatus.QUEUED.value],
                limit=10_000,
            )
            for record in queued:
                self._schedule(str(record["id"]))
            self._started = True

    async def execute(
        self,
        tool_call: ToolCall,
        *,
        parent_session_id: str,
        parent_run_id: str,
    ) -> ToolResult:
        definition = self._definitions.get(tool_call.name)
        if definition is None:
            return ToolResult(success=False, error=f"未知子 Agent 控制 Tool: {tool_call.name}")
        try:
            jsonschema.validate(tool_call.arguments, definition.input_schema)
        except jsonschema.ValidationError as exc:
            return ToolResult(success=False, error=f"子 Agent Tool 参数校验失败: {exc.message}")
        try:
            if tool_call.name == "spawn_agent":
                payload = await self._spawn(
                    tool_call,
                    parent_session_id=parent_session_id,
                    parent_run_id=parent_run_id,
                )
            elif tool_call.name == "get_agent_status":
                payload = self._status(parent_session_id, tool_call.arguments.get("task_ids"))
            elif tool_call.name == "await_agents":
                payload = await self._await_agents(
                    parent_session_id,
                    list(tool_call.arguments["task_ids"]),
                    timeout_seconds=float(tool_call.arguments.get("timeout_seconds", 300)),
                )
            else:
                payload = await self._cancel(
                    parent_session_id,
                    str(tool_call.arguments["task_id"]),
                    str(tool_call.arguments.get("reason") or "父 Agent 请求取消"),
                )
        except (RuntimeError, ValueError) as exc:
            return ToolResult(success=False, error=str(exc))
        metadata: dict[str, Any] = {"subagent_control": tool_call.name}
        if tool_call.name == "await_agents":
            metadata["reported_task_ids"] = payload.get("terminal_task_ids", [])
        return ToolResult(
            success=True,
            output=json.dumps(payload, ensure_ascii=False),
            metadata=metadata,
        )

    async def collect_required_results(self, parent_session_id: str) -> list[dict]:
        tasks = self.store.list_agent_tasks(
            parent_session_id,
            unreported_required_only=True,
            limit=self.config.subagents.max_tasks_per_session,
        )
        if not tasks:
            return []
        task_ids = [str(task["id"]) for task in tasks]
        payload = await self._await_agents(
            parent_session_id,
            task_ids,
            timeout_seconds=None,
        )
        return list(payload["tasks"])

    async def cancel_required(self, parent_session_id: str, reason: str) -> None:
        active = self.store.list_agent_tasks(
            parent_session_id,
            statuses=[
                WorkerStatus.QUEUED.value,
                WorkerStatus.RUNNING.value,
                WorkerStatus.WAITING_APPROVAL.value,
                WorkerStatus.CANCELLING.value,
            ],
            limit=self.config.subagents.max_tasks_per_session,
        )
        await asyncio.gather(
            *(
                self._cancel(parent_session_id, str(task["id"]), reason)
                for task in active
                if task["required"]
            ),
            return_exceptions=True,
        )

    def list_tasks(self, parent_session_id: str) -> list[dict]:
        return [
            self._public_task(record)
            for record in self.store.list_agent_tasks(
                parent_session_id,
                limit=self.config.subagents.max_tasks_per_session,
            )
        ]

    async def shutdown(self) -> None:
        if self._closing:
            if self.has_live_tasks:
                raise RuntimeError("后台子 Agent 尚未停止，不能关闭 Runtime")
            return
        self._closing = True
        if not self._started:
            return
        for task_id in self._created_task_ids:
            record = self.store.get_agent_task(task_id)
            if record is not None:
                self.store.request_agent_task_cancel(
                    task_id,
                    parent_session_id=str(record["parent_session_id"]),
                    reason="Worker Pool 已关闭",
                )
        scheduled = list(self._scheduled.values())
        pending: set[asyncio.Task[None]] = set()
        for task in scheduled:
            if not task.done():
                task.cancel()
        if scheduled:
            _, pending = await asyncio.wait(
                scheduled,
                timeout=self.config.subagents.shutdown_grace_seconds,
            )
            for task in pending:
                task.cancel()
            if pending:
                _, pending = await asyncio.wait(pending, timeout=1)
        self.store.interrupt_owned_agent_tasks(
            self.owner_id,
            workspace=self.workspace,
        )
        if pending:
            raise RuntimeError(
                f"{len(pending)} 个后台子 Agent 在取消后仍未停止；Runtime 保持打开以避免状态库竞态"
            )
        for task_id in list(self._changed):
            self._notify(task_id)
        self._scheduled.clear()

    async def _spawn(
        self,
        tool_call: ToolCall,
        *,
        parent_session_id: str,
        parent_run_id: str,
    ) -> dict:
        await self.start()
        arguments = tool_call.arguments
        agent_name = str(arguments["agent"])
        spec = self.specs.get(agent_name)
        if spec is None:
            raise ValueError(f"未知子 Agent profile: {agent_name}")
        if spec.isolation == WorkerIsolation.WORKTREE:
            if not self.config.subagents.allow_worktree_writes:
                raise ValueError("配置已禁止 worktree 写入型子 Agent")
            if self.config.permissions.mode == "read-only":
                raise ValueError("父运行处于 read-only 权限模式，不能创建写入型子 Agent")
        if (
            self.store.count_agent_tasks(workspace=self.workspace)
            >= self.config.subagents.max_queued
        ):
            raise RuntimeError("后台子 Agent 全局队列已满")
        if (
            self.store.count_agent_tasks(parent_session_id)
            >= self.config.subagents.max_tasks_per_session
        ):
            raise RuntimeError("当前父会话的后台子 Agent 数量已达到上限")
        usage = self.store.agent_task_usage(parent_session_id)
        max_cost = self.config.subagents.max_total_cost_usd_per_session
        if max_cost is not None:
            active = self.store.list_agent_tasks(
                parent_session_id,
                statuses=[
                    WorkerStatus.QUEUED.value,
                    WorkerStatus.RUNNING.value,
                    WorkerStatus.WAITING_APPROVAL.value,
                    WorkerStatus.CANCELLING.value,
                ],
                limit=self.config.subagents.max_tasks_per_session,
            )
            active_reserve = sum(
                float(task["spec"].get("max_cost_usd") or max_cost) for task in active
            )
            new_reserve = float(spec.max_cost_usd or max_cost)
            if float(usage["cost_usd"]) + active_reserve + new_reserve > max_cost:
                raise RuntimeError(f"当前父会话的子 Agent 费用加预留额度将超过 ${max_cost:g}")
        refs = list(dict.fromkeys(arguments.get("context_refs") or []))
        invalid_refs = [
            ref
            for ref in refs
            if self.store.read_context_blob(parent_session_id, ref, offset=0, limit=1) is None
        ]
        if invalid_refs:
            raise ValueError(f"父会话无权访问 context_ref: {', '.join(invalid_refs)}")
        requested_task_id = uuid4().hex
        record = self.store.create_agent_task(
            task_id=requested_task_id,
            parent_session_id=parent_session_id,
            parent_run_id=parent_run_id,
            agent_name=agent_name,
            objective=str(arguments["objective"]).strip(),
            constraints=list(arguments.get("constraints") or []),
            acceptance_criteria=list(arguments.get("acceptance_criteria") or []),
            spec=spec.model_dump(mode="json"),
            context_refs=refs,
            required=bool(arguments.get("required", True)),
            isolation=spec.isolation.value,
            idempotency_key=f"{parent_run_id}:{tool_call.id}",
        )
        task_id = str(record["id"])
        if task_id == requested_task_id:
            self._created_task_ids.add(task_id)
        for reference in refs:
            granted = self.store.grant_context_blob_access(
                source_session_id=parent_session_id,
                target_session_id=str(record["child_session_id"]),
                reference=reference,
            )
            if not granted:
                raise RuntimeError(f"context_ref 授权失败: {reference}")
        self._schedule(task_id)
        if task_id == requested_task_id:
            await self.event_bus.emit(
                EventType.SUBAGENT_QUEUED,
                session_id=parent_session_id,
                run_id=parent_run_id,
                payload={
                    "task_id": task_id,
                    "child_session_id": record["child_session_id"],
                    "agent": agent_name,
                    "required": record["required"],
                    "isolation": record["isolation"],
                },
            )
        return {
            "task": self._public_task(record),
            "message": (
                "任务已持久化并进入后台调度；使用 await_agents 或 get_agent_status 获取结果。"
            ),
        }

    def _schedule(self, task_id: str) -> None:
        existing = self._scheduled.get(task_id)
        if existing and not existing.done():
            return
        scheduled = asyncio.create_task(
            self._run_scheduled(task_id), name=f"subagent-{task_id[:8]}"
        )
        self._scheduled[task_id] = scheduled

        def completed(done: asyncio.Task[None]) -> None:
            if self._scheduled.get(task_id) is done:
                self._scheduled.pop(task_id, None)
            self._notify(task_id)

        scheduled.add_done_callback(completed)

    async def _run_scheduled(self, task_id: str) -> None:
        lease = _PermitLease(self._semaphore)
        try:
            await lease.acquire()
            if not self.store.claim_agent_task(task_id, owner_id=self.owner_id):
                return
            record = self.store.get_agent_task(task_id)
            if record is None:
                return
            task = agent_task_from_record(record)
            await self.event_bus.emit(
                EventType.SUBAGENT_STARTED,
                session_id=task.parent_session_id,
                run_id=f"subagent:{task.id}",
                payload={
                    "task_id": task.id,
                    "child_session_id": task.child_session_id,
                    "agent": task.agent_name,
                },
            )
            child_workspace = self.workspace
            if task.isolation == WorkerIsolation.WORKTREE:
                child_workspace = await self._prepare_worktree(task.id)
                self.store.update_session_workspace(task.child_session_id, child_workspace)
            approval = _TaskApprovalHandler(
                task=task,
                delegate=self.approval_handler,
                store=self.store,
                event_bus=self.event_bus,
                lease=lease,
                notify=self._notify,
            )
            runner = self.runner_factory(task.spec, child_workspace, approval)
            result = await runner.run(
                RunRequest(
                    prompt=self._render_task_prompt(task),
                    session_id=task.child_session_id,
                    explicit_skills=task.spec.explicit_skills,
                )
            )
            agent_result = await self._build_result(task, result, child_workspace)
            stored = self.store.finish_agent_task(
                task.id,
                status=agent_result.status.value,
                result=agent_result.model_dump(mode="json"),
                error=agent_result.error,
            )
            if stored:
                await self._emit_terminal(task, agent_result)
        except asyncio.CancelledError:
            record = self.store.get_agent_task(task_id)
            if record is not None:
                stored = self.store.finish_agent_task(
                    task_id,
                    status=WorkerStatus.CANCELLED.value,
                    result=None,
                    error=record.get("error") or "后台子 Agent 已取消",
                )
                if stored:
                    await self.event_bus.emit(
                        EventType.SUBAGENT_CANCELLED,
                        session_id=str(record["parent_session_id"]),
                        run_id=f"subagent:{task_id}",
                        payload={"task_id": task_id, "error": record.get("error")},
                    )
        except Exception as exc:
            record = self.store.get_agent_task(task_id)
            if record is not None:
                stored = self.store.finish_agent_task(
                    task_id,
                    status=WorkerStatus.FAILED.value,
                    result=None,
                    error=str(exc),
                )
                if stored:
                    await self.event_bus.emit(
                        EventType.SUBAGENT_FAILED,
                        session_id=str(record["parent_session_id"]),
                        run_id=f"subagent:{task_id}",
                        payload={"task_id": task_id, "error": str(exc)},
                    )
        finally:
            lease.release()
            self._notify(task_id)

    async def _build_result(self, task: AgentTask, run_result, workspace: Path) -> AgentResult:
        status = WorkerStatus(run_result.status)
        final_text = run_result.final_text or ""
        structured = self._parse_structured_result(final_text)
        evidence_refs: list[str] = []
        if final_text:
            full_result_ref = self.store.put_context_blob(
                session_id=task.child_session_id,
                run_id=f"subagent-result:{task.id}",
                content=final_text,
                media_type="application/vnd.bot.subagent-result+text",
            )
            evidence_refs.append(full_result_ref)
        declared_refs = structured.get("evidence_refs") or []
        for reference in [*declared_refs, *re.findall(r"blob:[0-9a-f]{64}", final_text)]:
            if reference not in evidence_refs:
                evidence_refs.append(reference)
        granted_refs: list[str] = []
        for reference in evidence_refs:
            if self.store.grant_context_blob_access(
                source_session_id=task.child_session_id,
                target_session_id=task.parent_session_id,
                reference=reference,
            ):
                granted_refs.append(reference)
        files_changed: list[str] = []
        worktree_path: str | None = None
        if task.isolation == WorkerIsolation.WORKTREE:
            worktree_path = str(workspace)
            files_changed, diff = await self._worktree_changes(workspace)
            if diff:
                diff_ref = self.store.put_context_blob(
                    session_id=task.child_session_id,
                    run_id=f"subagent-result:{task.id}",
                    content=diff,
                    media_type="text/x-diff",
                )
                if self.store.grant_context_blob_access(
                    source_session_id=task.child_session_id,
                    target_session_id=task.parent_session_id,
                    reference=diff_ref,
                ):
                    granted_refs.append(diff_ref)
        limit = self.config.subagents.result_inline_chars
        result_summary = structured.get("summary") or final_text
        summary = (
            result_summary
            if len(result_summary) <= limit
            else result_summary[:limit] + "\n…[完整结果见 evidence_refs]"
        )
        return AgentResult(
            task_id=task.id,
            child_session_id=task.child_session_id,
            status=status,
            summary=summary,
            findings=structured.get("findings") or [],
            evidence_refs=list(dict.fromkeys(granted_refs)),
            files_changed=files_changed,
            worktree_path=worktree_path,
            verification=structured.get("verification") or [],
            risks=structured.get("risks") or [],
            unresolved=structured.get("unresolved") or [],
            error=run_result.error,
            input_tokens=run_result.input_tokens,
            output_tokens=run_result.output_tokens,
            cost_usd=run_result.cost_usd,
        )

    async def _emit_terminal(self, task: AgentTask, result: AgentResult) -> None:
        event_type = {
            WorkerStatus.COMPLETED: EventType.SUBAGENT_COMPLETED,
            WorkerStatus.BLOCKED: EventType.SUBAGENT_BLOCKED,
            WorkerStatus.CANCELLED: EventType.SUBAGENT_CANCELLED,
            WorkerStatus.INTERRUPTED: EventType.SUBAGENT_INTERRUPTED,
        }.get(result.status, EventType.SUBAGENT_FAILED)
        await self.event_bus.emit(
            event_type,
            session_id=task.parent_session_id,
            run_id=f"subagent:{task.id}",
            payload={
                "task_id": task.id,
                "child_session_id": task.child_session_id,
                "status": result.status.value,
                "summary": result.summary,
                "findings": result.findings,
                "evidence_refs": result.evidence_refs,
                "files_changed": result.files_changed,
                "error": result.error,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cost_usd": result.cost_usd,
            },
        )

    async def _await_agents(
        self,
        parent_session_id: str,
        task_ids: list[str],
        *,
        timeout_seconds: float | None,
    ) -> dict:
        await self.start()
        task_ids = list(dict.fromkeys(task_ids))
        if not task_ids:
            raise ValueError("task_ids 不能为空")
        unknown = [
            task_id
            for task_id in task_ids
            if self.store.get_agent_task(task_id, parent_session_id=parent_session_id) is None
        ]
        if unknown:
            raise ValueError(f"子 Agent 任务不存在或不属于当前父会话: {', '.join(unknown)}")

        async def wait_all() -> None:
            await asyncio.gather(
                *(self._wait_terminal(parent_session_id, task_id) for task_id in task_ids)
            )

        timed_out = False
        if timeout_seconds is None:
            await wait_all()
        else:
            try:
                async with asyncio.timeout(timeout_seconds):
                    await wait_all()
            except TimeoutError:
                timed_out = True
        records = [
            self.store.get_agent_task(task_id, parent_session_id=parent_session_id)
            for task_id in task_ids
        ]
        terminal_ids = [
            str(record["id"])
            for record in records
            if record is not None and WorkerStatus(record["status"]).terminal
        ]
        return {
            "completed": not timed_out and len(terminal_ids) == len(task_ids),
            "timed_out": timed_out,
            "terminal_task_ids": terminal_ids,
            "tasks": [self._public_task(record) for record in records if record is not None],
        }

    async def _wait_terminal(self, parent_session_id: str, task_id: str) -> None:
        event = self._changed.setdefault(task_id, asyncio.Event())
        while True:
            record = self.store.get_agent_task(task_id, parent_session_id=parent_session_id)
            if record is None or WorkerStatus(record["status"]).terminal:
                return
            event.clear()
            record = self.store.get_agent_task(task_id, parent_session_id=parent_session_id)
            if record is None or WorkerStatus(record["status"]).terminal:
                return
            await event.wait()

    def _status(self, parent_session_id: str, task_ids: list[str] | None) -> dict:
        if task_ids:
            unique_task_ids = list(dict.fromkeys(task_ids))
            records = [
                self.store.get_agent_task(task_id, parent_session_id=parent_session_id)
                for task_id in unique_task_ids
            ]
            unknown = [
                task_id
                for task_id, record in zip(unique_task_ids, records, strict=True)
                if not record
            ]
            if unknown:
                raise ValueError(f"子 Agent 任务不存在或不属于当前父会话: {', '.join(unknown)}")
        else:
            records = self.store.list_agent_tasks(
                parent_session_id,
                limit=self.config.subagents.max_tasks_per_session,
            )
        return {
            "tasks": [self._public_task(record) for record in records if record is not None],
            "usage": self.store.agent_task_usage(parent_session_id),
            "max_concurrent": self.config.subagents.max_concurrent,
        }

    async def _cancel(self, parent_session_id: str, task_id: str, reason: str) -> dict:
        status = self.store.request_agent_task_cancel(
            task_id,
            parent_session_id=parent_session_id,
            reason=reason,
        )
        if status is None:
            raise ValueError("子 Agent 任务不存在或不属于当前父会话")
        scheduled = self._scheduled.get(task_id)
        if status in {WorkerStatus.CANCELLING.value, WorkerStatus.CANCELLED.value}:
            if scheduled and not scheduled.done():
                scheduled.cancel()
            self._notify(task_id)
        record = self.store.get_agent_task(task_id, parent_session_id=parent_session_id)
        return {"task": self._public_task(record), "cancel_status": status}

    async def _prepare_worktree(self, task_id: str) -> Path:
        root_output, error, returncode = await self._run_process(
            ["git", "rev-parse", "--show-toplevel"], self.workspace
        )
        if returncode != 0:
            raise RuntimeError(f"写入型子 Agent 需要 Git 仓库: {error.strip()}")
        git_root = Path(root_output.strip()).resolve()
        if git_root != self.workspace:
            raise RuntimeError("写入型子 Agent 当前要求 workspace 等于 Git 仓库根目录")
        configured = Path(self.config.subagents.worktree_dir)
        if configured.is_absolute():
            raise RuntimeError("subagents.worktree_dir 必须是工作区内相对路径")
        worktree_root = (self.workspace / configured).resolve()
        try:
            worktree_root.relative_to(self.workspace)
        except ValueError as exc:
            raise RuntimeError("subagents.worktree_dir 逃逸工作区") from exc
        destination = worktree_root / task_id
        if destination.exists():
            raise RuntimeError(f"子 Agent worktree 已存在: {destination}")
        worktree_root.mkdir(parents=True, exist_ok=True)
        _, error, returncode = await self._run_process(
            ["git", "worktree", "add", "--detach", str(destination), "HEAD"],
            self.workspace,
        )
        if returncode != 0:
            raise RuntimeError(f"创建子 Agent worktree 失败: {error.strip()}")
        return destination.resolve()

    async def _worktree_changes(self, workspace: Path) -> tuple[list[str], str]:
        tracked_names, _, tracked_names_code = await self._run_process(
            ["git", "diff", "--name-only", "-z", "HEAD", "--"], workspace
        )
        untracked_names, _, untracked_names_code = await self._run_process(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"], workspace
        )
        tracked = tracked_names.split("\0") if tracked_names_code == 0 else []
        untracked = untracked_names.split("\0") if untracked_names_code == 0 else []
        tracked = [path for path in tracked if path]
        untracked = [path for path in untracked if path]
        files = list(dict.fromkeys([*tracked, *untracked]))
        diff, _, diff_code = await self._run_process(
            ["git", "diff", "--no-ext-diff", "--binary", "HEAD", "--"],
            workspace,
            limit=2_000_000,
        )
        if diff_code != 0:
            diff = ""
        for path in untracked:
            remaining = 2_000_000 - len(diff.encode("utf-8"))
            if remaining <= 0:
                break
            untracked_diff, _, returncode = await self._run_process(
                ["git", "diff", "--no-index", "--binary", "--", "/dev/null", path],
                workspace,
                limit=remaining,
            )
            if returncode in {0, 1}:
                diff += untracked_diff
        return files, diff

    async def _run_process(
        self,
        argv: list[str],
        cwd: Path,
        *,
        limit: int = 100_000,
    ) -> tuple[str, str, int]:
        stdout: list[str] = []
        stderr: list[str] = []
        returncode = -1
        async for event in self.execution_target.execute(
            ProcessSpec(argv=argv, cwd=cwd, timeout_seconds=120, output_limit_bytes=limit)
        ):
            if event.kind == ProcessEventKind.STDOUT:
                stdout.append(event.data)
            elif event.kind == ProcessEventKind.STDERR:
                stderr.append(event.data)
            else:
                returncode = int(event.returncode or 0)
        return "".join(stdout), "".join(stderr), returncode

    def _notify(self, task_id: str) -> None:
        event = self._changed.setdefault(task_id, asyncio.Event())
        event.set()

    @staticmethod
    def _render_task_prompt(task: AgentTask) -> str:
        constraints = "\n".join(f"- {item}" for item in task.constraints) or "- 无额外约束"
        acceptance = (
            "\n".join(f"- {item}" for item in task.acceptance_criteria) or "- 提供可核验结论"
        )
        refs = "\n".join(f"- {item}" for item in task.context_refs) or "- 无"
        return (
            f"你是后台子 Agent profile `{task.agent_name}`。\n"
            f"Profile 指令：{task.spec.instructions}\n\n"
            f"任务目标：\n{task.objective}\n\n"
            f"约束：\n{constraints}\n\n"
            f"验收标准：\n{acceptance}\n\n"
            f"显式授权的上下文引用：\n{refs}\n"
            "仅在确有需要时调用 load_context_reference；不要假设你能看到父会话历史。"
            "最终回答优先输出一个 JSON 对象，字段为 summary、findings、evidence_refs、"
            "verification、risks、unresolved；除 summary 为字符串外，其余字段均为字符串数组。"
        )

    @staticmethod
    def _parse_structured_result(content: str) -> dict[str, Any]:
        candidate = content.strip()
        if candidate.startswith("```") and candidate.endswith("```"):
            lines = candidate.splitlines()
            candidate = "\n".join(lines[1:-1]).strip()
        try:
            payload = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        result: dict[str, Any] = {}
        summary = payload.get("summary")
        if isinstance(summary, str):
            result["summary"] = summary
        for key in ("findings", "evidence_refs", "verification", "risks", "unresolved"):
            value = payload.get(key)
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                result[key] = value[:100]
        return result

    @staticmethod
    def _public_task(record: dict | None) -> dict:
        if record is None:
            return {}
        keys = (
            "id",
            "parent_session_id",
            "child_session_id",
            "agent_name",
            "objective",
            "required",
            "isolation",
            "workspace",
            "status",
            "created_at",
            "started_at",
            "completed_at",
            "reported_at",
            "result",
            "error",
        )
        return {key: record.get(key) for key in keys}

    def _build_definitions(self) -> dict[str, ToolDefinition]:
        agent_names = sorted(self.specs)
        ref_schema = {"type": "string", "pattern": "^blob:[0-9a-f]{64}$"}
        task_ids_schema = {
            "type": "array",
            "items": {"type": "string", "minLength": 8},
            "minItems": 1,
            "maxItems": self.config.subagents.max_tasks_per_session,
        }
        definitions = [
            ToolDefinition(
                name="spawn_agent",
                description=(
                    "创建一个持久化后台子 Agent 任务并立即返回 task_id。"
                    "适合把相互独立的检索、审查或 worktree 编码工作并行化。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "agent": {"type": "string", "enum": agent_names},
                        "objective": {"type": "string", "minLength": 1, "maxLength": 20_000},
                        "constraints": {
                            "type": "array",
                            "items": {"type": "string", "maxLength": 2_000},
                            "maxItems": 20,
                        },
                        "acceptance_criteria": {
                            "type": "array",
                            "items": {"type": "string", "maxLength": 2_000},
                            "maxItems": 20,
                        },
                        "context_refs": {
                            "type": "array",
                            "items": ref_schema,
                            "maxItems": 20,
                        },
                        "required": {"type": "boolean", "default": True},
                    },
                    "required": ["agent", "objective"],
                    "additionalProperties": False,
                },
            ),
            ToolDefinition(
                name="get_agent_status",
                description="查询当前父会话创建的后台子 Agent 状态和结构化结果。",
                input_schema={
                    "type": "object",
                    "properties": {"task_ids": task_ids_schema},
                    "additionalProperties": False,
                },
            ),
            ToolDefinition(
                name="await_agents",
                description=(
                    "等待一个或多个后台子 Agent 进入终态；超时只返回当前状态，不会取消任务。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "task_ids": task_ids_schema,
                        "timeout_seconds": {
                            "type": "number",
                            "minimum": 0.1,
                            "maximum": 3600,
                            "default": 300,
                        },
                    },
                    "required": ["task_ids"],
                    "additionalProperties": False,
                },
            ),
            ToolDefinition(
                name="cancel_agent",
                description="幂等取消当前父会话创建的 queued/running 后台子 Agent。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "minLength": 8},
                        "reason": {"type": "string", "maxLength": 2_000},
                    },
                    "required": ["task_id"],
                    "additionalProperties": False,
                },
            ),
        ]
        return {definition.name: definition for definition in definitions}


def default_agent_specs(config: AppConfig, registry: ToolRegistry) -> list[AgentSpec]:
    local_read_tools = [
        tool.name
        for tool in registry
        if tool.annotations.read_only and not tool.annotations.network_access
    ]
    all_tools = registry.names()
    cost_limits = [
        value
        for value in (
            config.agent.max_cost_usd,
            config.subagents.max_cost_usd_per_task,
            config.subagents.max_total_cost_usd_per_session,
        )
        if value is not None
    ]
    task_cost_limit = min(cost_limits) if cost_limits else None
    specs = [
        AgentSpec(
            name="explorer",
            description="只读探索代码、配置和证据，适合独立调查子问题。",
            instructions="只读调查，给出精确文件定位和证据；不要修改工作区。",
            allowed_tools=local_read_tools,
            isolation=WorkerIsolation.READ_ONLY,
            max_steps=config.subagents.max_steps,
            max_wall_time_seconds=config.subagents.max_wall_time_seconds,
            max_cost_usd=task_cost_limit,
        ),
        AgentSpec(
            name="reviewer",
            description="只读审查实现、测试、安全性和边界条件。",
            instructions="以审查者视角寻找缺陷、竞态和遗漏，并按严重度给出可核验证据。",
            allowed_tools=local_read_tools,
            isolation=WorkerIsolation.READ_ONLY,
            max_steps=config.subagents.max_steps,
            max_wall_time_seconds=config.subagents.max_wall_time_seconds,
            max_cost_usd=task_cost_limit,
        ),
    ]
    if config.subagents.allow_worktree_writes and config.permissions.mode != "read-only":
        specs.append(
            AgentSpec(
                name="coder",
                description="在独立 Git worktree 内实现和验证一个边界清晰的编码子任务。",
                instructions=(
                    "只在分配的独立 worktree 中修改文件；运行与改动相称的验证，"
                    "最终列出修改文件、验证结果和未解决风险。"
                ),
                allowed_tools=all_tools,
                isolation=WorkerIsolation.WORKTREE,
                max_steps=config.subagents.max_steps,
                max_wall_time_seconds=config.subagents.max_wall_time_seconds,
                max_cost_usd=task_cost_limit,
            )
        )
    return specs
