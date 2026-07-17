from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from time import monotonic
from uuid import uuid4

import jsonschema

from bot.config.models import AppConfig
from bot.core.approval import ApprovalHandler, ApprovalScope, DenyApprovalHandler
from bot.core.context import ContextAssembler, compact_messages
from bot.core.events import EventBus, EventType
from bot.core.models import (
    ChatMessage,
    ModelEventKind,
    ModelRequest,
    Role,
    RunRequest,
    RunResult,
    ToolCall,
)
from bot.execution import ExecutionTarget
from bot.observability import Redactor
from bot.policy import DefaultPolicyEngine, PolicyDecisionKind, ToolAction
from bot.providers import ModelProvider, ProviderError
from bot.sessions import SQLiteSessionStore
from bot.skills import SkillManager
from bot.tools import ToolContext, ToolRegistry, ToolResult


@dataclass
class _ToolCallBuffer:
    index: int
    id: str = ""
    name: str = ""
    arguments: str = ""


class AgentRunner:
    def __init__(
        self,
        *,
        config: AppConfig,
        workspace,
        provider: ModelProvider,
        tool_registry: ToolRegistry,
        policy: DefaultPolicyEngine,
        execution_target: ExecutionTarget,
        skills: SkillManager,
        context: ContextAssembler,
        store: SQLiteSessionStore,
        event_bus: EventBus,
        approval_handler: ApprovalHandler | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self.config = config
        self.workspace = workspace.resolve()
        self.provider = provider
        self.tool_registry = tool_registry
        self.policy = policy
        self.execution_target = execution_target
        self.skills = skills
        self.context = context
        self.store = store
        self.event_bus = event_bus
        self.approval_handler = approval_handler or DenyApprovalHandler()
        self.redactor = redactor or Redactor()
        self._session_approvals: set[tuple[str, str]] = set()
        self._force_compact_sessions: set[str] = set()

    def request_compaction(self, session_id: str) -> None:
        self._force_compact_sessions.add(session_id)

    async def run(self, request: RunRequest) -> RunResult:
        session_id = request.session_id or self.store.create_session(self.workspace)
        self.store.ensure_session(session_id, self.workspace)
        run_id = uuid4().hex
        self.store.start_run(session_id, run_id)
        await self.event_bus.emit(
            EventType.RUN_STARTED,
            session_id=session_id,
            run_id=run_id,
            payload={"prompt": request.prompt, "workspace": str(self.workspace)},
        )
        try:
            async with asyncio.timeout(self.config.agent.max_wall_time_seconds):
                result = await self._run_loop(request, session_id=session_id, run_id=run_id)
        except TimeoutError:
            error = f"运行超过 {self.config.agent.max_wall_time_seconds:g} 秒限制"
            await self._fail_event(session_id, run_id, error)
            result = RunResult(
                session_id=session_id,
                status="limit_reached",
                error=error,
            )
        except asyncio.CancelledError:
            error = "运行已取消"
            await self._fail_event(session_id, run_id, error)
            result = RunResult(session_id=session_id, status="cancelled", error=error)
        except Exception as exc:
            error = str(exc)
            await self._fail_event(session_id, run_id, error)
            result = RunResult(session_id=session_id, status="failed", error=error)
        self.store.finish_run(run_id, result.status, result.error)
        return result

    async def _run_loop(self, request: RunRequest, *, session_id: str, run_id: str) -> RunResult:
        environment = await self.execution_target.probe(["ksys", "tuner"])
        messages = self.context.system_messages(environment)
        memories = self.store.list_memories()
        if memories:
            memory_text = "\n".join(f"- [{item['id']}] {item['content']}" for item in memories)
            messages.append(
                ChatMessage(
                    role=Role.SYSTEM,
                    content=f"用户显式确认的长期记忆：\n{memory_text}",
                )
            )
        history = self.store.load_messages(session_id)
        messages.extend(history)

        for skill in self.skills.catalog.skills.values():
            await self.event_bus.emit(
                EventType.SKILL_DISCOVERED,
                session_id=session_id,
                run_id=run_id,
                payload={
                    "name": skill.name,
                    "description": skill.description,
                    "path": str(skill.path),
                },
            )

        for name in request.explicit_skills:
            skill, content = self.skills.activate(name, "用户显式指定", explicit=True)
            event_type = EventType.SKILL_ACTIVATED if skill else EventType.SKILL_SKIPPED
            await self.event_bus.emit(
                event_type,
                session_id=session_id,
                run_id=run_id,
                payload={"name": name, "explicit": True, "message": content},
            )
            if skill:
                skill_message = ChatMessage(role=Role.SYSTEM, content=content)
                messages.append(skill_message)
                self.store.append_message(session_id, run_id, skill_message)

        user_message = self.redactor.redact_message(
            ChatMessage(role=Role.USER, content=request.prompt)
        )
        messages.append(user_message)
        self.store.append_message(session_id, run_id, user_message)

        failures = 0
        failed_fingerprints: dict[str, int] = {}
        started_at = monotonic()
        for step in range(1, self.config.agent.max_steps + 1):
            if monotonic() - started_at > self.config.agent.max_wall_time_seconds:
                raise TimeoutError
            request_tools = self.tool_registry.definitions()
            if self.config.skills.auto_activate and self.skills.catalog.skills:
                request_tools.append(self.skills.catalog.activation_tool_definition())
            for active_name in self.skills.active:
                marker = f"已激活 Skill: {active_name}\n"
                if any(
                    message.role == Role.SYSTEM and (message.content or "").startswith(marker)
                    for message in messages
                ):
                    continue
                active_skill = self.skills.catalog.get(active_name)
                if active_skill:
                    messages.append(
                        ChatMessage(
                            role=Role.SYSTEM,
                            content=self.skills.render(active_skill),
                        )
                    )
            tool_schema_chars = sum(
                len(json.dumps(tool.model_dump(mode="json"), ensure_ascii=False))
                for tool in request_tools
            )
            force_compact = session_id in self._force_compact_sessions
            messages, compacted = compact_messages(
                messages,
                max_tokens=self.config.context.max_input_tokens,
                threshold=0 if force_compact else self.config.context.auto_compact_threshold,
                tool_schema_chars=tool_schema_chars,
            )
            self._force_compact_sessions.discard(session_id)
            if compacted:
                await self.event_bus.emit(
                    EventType.CONTEXT_COMPACTED,
                    session_id=session_id,
                    run_id=run_id,
                    payload=compacted,
                )
            model_request = ModelRequest(
                model=self.config.model.name,
                messages=messages,
                tools=request_tools,
                temperature=self.config.model.temperature,
                max_output_tokens=self.config.model.max_output_tokens,
            )
            text_parts: list[str] = []
            call_buffers: dict[int, _ToolCallBuffer] = {}
            finish_reason: str | None = None
            try:
                async for event in self.provider.stream(model_request):
                    if event.kind == ModelEventKind.TEXT_DELTA and event.text:
                        text_parts.append(event.text)
                        await self.event_bus.emit(
                            EventType.ASSISTANT_DELTA,
                            session_id=session_id,
                            run_id=run_id,
                            payload={"text": event.text},
                        )
                    elif event.kind == ModelEventKind.TOOL_CALL_DELTA:
                        index = event.tool_index or 0
                        buffer = call_buffers.setdefault(index, _ToolCallBuffer(index=index))
                        if event.tool_call_id:
                            buffer.id = event.tool_call_id
                        if event.tool_name:
                            buffer.name += event.tool_name
                        if event.arguments_delta:
                            buffer.arguments += event.arguments_delta
                    elif event.kind == ModelEventKind.FINISH:
                        finish_reason = event.finish_reason
            except ProviderError:
                raise

            assistant_text = "".join(text_parts)
            tool_calls = [self._parse_tool_call(buffer) for buffer in call_buffers.values()]
            assistant_message = self.redactor.redact_message(
                ChatMessage(
                    role=Role.ASSISTANT,
                    content=assistant_text or None,
                    tool_calls=tool_calls,
                )
            )
            messages.append(assistant_message)
            self.store.append_message(session_id, run_id, assistant_message)

            if not tool_calls:
                await self.event_bus.emit(
                    EventType.ASSISTANT_MESSAGE,
                    session_id=session_id,
                    run_id=run_id,
                    payload={"text": assistant_text, "finish_reason": finish_reason},
                )
                await self.event_bus.emit(
                    EventType.RUN_COMPLETED,
                    session_id=session_id,
                    run_id=run_id,
                    payload={"steps": step, "final_text": assistant_text},
                )
                return RunResult(
                    session_id=session_id,
                    status="completed",
                    final_text=assistant_text,
                    steps=step,
                )

            for tool_call in tool_calls:
                await self.event_bus.emit(
                    EventType.TOOL_REQUESTED,
                    session_id=session_id,
                    run_id=run_id,
                    payload={
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "arguments": tool_call.arguments,
                    },
                )
                if tool_call.name == "activate_skill":
                    result = await self._activate_skill(tool_call, session_id, run_id)
                else:
                    result = await self._execute_tool(tool_call, session_id, run_id)

                result_message = ChatMessage(
                    role=Role.TOOL,
                    name=tool_call.name,
                    tool_call_id=tool_call.id,
                    content=result.model_content(),
                )
                messages.append(result_message)
                self.store.append_message(session_id, run_id, result_message)

                fingerprint = self._fingerprint(tool_call)
                if result.success:
                    failures = 0
                    failed_fingerprints.pop(fingerprint, None)
                else:
                    failures += 1
                    failed_fingerprints[fingerprint] = failed_fingerprints.get(fingerprint, 0) + 1
                    if failed_fingerprints[fingerprint] >= 2:
                        warning = ChatMessage(
                            role=Role.SYSTEM,
                            content=(
                                f"相同 Tool Call 已连续失败 {failed_fingerprints[fingerprint]} 次。"
                                "禁止原样重试；请改变方案或向用户说明阻塞。"
                            ),
                        )
                        messages.append(warning)
                    if failures >= self.config.agent.max_consecutive_failures:
                        error = f"连续 {failures} 次 Tool 执行失败，运行已熔断"
                        await self._fail_event(session_id, run_id, error)
                        return RunResult(
                            session_id=session_id,
                            status="failed",
                            final_text=assistant_text,
                            steps=step,
                            error=error,
                        )

        error = f"达到最大步骤数 {self.config.agent.max_steps}"
        await self._fail_event(session_id, run_id, error)
        return RunResult(
            session_id=session_id,
            status="limit_reached",
            steps=self.config.agent.max_steps,
            error=error,
        )

    async def _activate_skill(
        self, tool_call: ToolCall, session_id: str, run_id: str
    ) -> ToolResult:
        name = tool_call.arguments.get("name")
        reason = tool_call.arguments.get("reason")
        if not isinstance(name, str) or not isinstance(reason, str):
            return ToolResult(success=False, error="activate_skill 需要字符串 name 和 reason")
        skill, content = self.skills.activate(name, reason, explicit=False)
        event_type = EventType.SKILL_ACTIVATED if skill else EventType.SKILL_SKIPPED
        await self.event_bus.emit(
            event_type,
            session_id=session_id,
            run_id=run_id,
            payload={"name": name, "reason": reason, "explicit": False, "message": content},
        )
        return ToolResult(
            success=skill is not None,
            output=content if skill else "",
            error=None if skill else content,
            metadata={"skill": name},
        )

    async def _execute_tool(self, tool_call: ToolCall, session_id: str, run_id: str) -> ToolResult:
        tool = self.tool_registry.get(tool_call.name)
        if tool is None:
            return ToolResult(success=False, error=f"未知 Tool: {tool_call.name}")
        try:
            jsonschema.validate(tool_call.arguments, tool.input_schema)
        except jsonschema.ValidationError as exc:
            return ToolResult(success=False, error=f"Tool 参数校验失败: {exc.message}")

        action = ToolAction(
            tool_name=tool.name,
            arguments=tool_call.arguments,
            annotations=tool.annotations,
        )
        decision = self.policy.evaluate(action)
        if decision.kind == PolicyDecisionKind.DENY:
            return ToolResult(success=False, error=f"策略拒绝: {decision.reason}")
        if decision.kind == PolicyDecisionKind.ASK:
            fingerprint = self._fingerprint(tool_call)
            session_preapproved = (session_id, fingerprint) in self._session_approvals
            always_preapproved = self.store.has_approval_rule(fingerprint)
            preapproved = session_preapproved or always_preapproved
            if preapproved:
                approved = True
                scope = ApprovalScope.ALWAYS if always_preapproved else ApprovalScope.SESSION
            else:
                await self.event_bus.emit(
                    EventType.APPROVAL_REQUESTED,
                    session_id=session_id,
                    run_id=run_id,
                    payload={
                        "tool_call_id": tool_call.id,
                        "name": tool.name,
                        "arguments": tool_call.arguments,
                        "reason": decision.reason,
                    },
                )
                response = await self.approval_handler.approve(action, decision)
                approved = response.approved
                scope = response.scope
                if approved and scope == ApprovalScope.SESSION:
                    self._session_approvals.add((session_id, fingerprint))
                elif approved and scope == ApprovalScope.ALWAYS:
                    self.store.save_approval_rule(
                        tool_name=tool.name,
                        action_fingerprint=fingerprint,
                        arguments=tool_call.arguments,
                    )
            self.store.record_approval(
                session_id=session_id,
                run_id=run_id,
                tool_call_id=tool_call.id,
                decision="allow" if approved else "deny",
                scope=scope.value,
                reason=decision.reason,
            )
            await self.event_bus.emit(
                EventType.APPROVAL_RESOLVED,
                session_id=session_id,
                run_id=run_id,
                payload={
                    "tool_call_id": tool_call.id,
                    "approved": approved,
                    "scope": scope.value,
                    "preapproved": preapproved,
                },
            )
            if not approved:
                return ToolResult(success=False, error="用户未批准该操作")

        self.store.record_tool_run(
            session_id=session_id,
            run_id=run_id,
            tool_call_id=tool_call.id,
            tool_name=tool.name,
            arguments=tool_call.arguments,
            status="running",
        )
        await self.event_bus.emit(
            EventType.TOOL_STARTED,
            session_id=session_id,
            run_id=run_id,
            payload={"tool_call_id": tool_call.id, "name": tool.name},
        )
        context = ToolContext(
            workspace=self.workspace,
            execution_target=self.execution_target,
            workspace_only=self.config.permissions.workspace_only,
            max_output_bytes=self.config.agent.max_tool_output_bytes,
        )
        try:
            result = await tool.execute(context, tool_call.arguments)
        except Exception as exc:
            result = ToolResult(success=False, error=f"Tool 未处理异常: {exc}")
        result = self.redactor.redact_tool_result(result)
        await self.event_bus.emit(
            EventType.TOOL_COMPLETED,
            session_id=session_id,
            run_id=run_id,
            payload={
                "tool_call_id": tool_call.id,
                "name": tool.name,
                "success": result.success,
                "output": result.output,
                "error": result.error,
                "truncated": result.truncated,
            },
        )
        self.store.record_tool_run(
            session_id=session_id,
            run_id=run_id,
            tool_call_id=tool_call.id,
            tool_name=tool.name,
            arguments=tool_call.arguments,
            status="completed" if result.success else "failed",
            result=result.model_dump(mode="json"),
        )
        return result

    async def _fail_event(self, session_id: str, run_id: str, error: str) -> None:
        await self.event_bus.emit(
            EventType.RUN_FAILED,
            session_id=session_id,
            run_id=run_id,
            payload={"error": error},
        )

    @staticmethod
    def _parse_tool_call(buffer: _ToolCallBuffer) -> ToolCall:
        call_id = buffer.id or f"call_{uuid4().hex}"
        try:
            arguments = json.loads(buffer.arguments or "{}")
        except json.JSONDecodeError:
            arguments = {"_invalid_json": buffer.arguments}
        if not isinstance(arguments, dict):
            arguments = {"_invalid_arguments": arguments}
        return ToolCall(id=call_id, name=buffer.name, arguments=arguments)

    @staticmethod
    def _fingerprint(tool_call: ToolCall) -> str:
        payload = json.dumps(
            {"name": tool_call.name, "arguments": tool_call.arguments},
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode()).hexdigest()
