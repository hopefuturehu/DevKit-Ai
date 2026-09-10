from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from typing import Literal

from bot.core.context import (
    PositionedMessage,
    SkillDelivery,
    TokenEstimator,
    synthetic_user_context_message,
)
from bot.core.models import ChatMessage, ModelRequest
from bot.observability import Redactor
from bot.sessions import SQLiteSessionStore
from bot.skills.catalog import SkillManager
from bot.skills.models import ActiveSkill


class SkillContextError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass
class SkillBinding:
    name: str
    reason: str
    explicit: bool
    version_hash: str
    body_ref: str
    body_bytes: int
    tokens: int
    delivery_position: int | None = None

    def delivery(self, kind: Literal["auto_body", "explicit_body", "restored_body"]):
        return SkillDelivery(
            kind=kind,
            skill_name=self.name,
            version_hash=self.version_hash,
            body_ref=self.body_ref,
            body_bytes=self.body_bytes,
        )


@dataclass
class PreparedSkill:
    binding: SkillBinding
    body: str
    reused: PositionedMessage | None


class RunSkillState(SkillManager):
    """Run-owned binding and request dependencies, independent of body placement."""

    def __init__(
        self,
        template: SkillManager,
        *,
        session_id: str,
        run_id: str,
        mode: str,
        store: SQLiteSessionStore,
        body_budget: int,
        redactor: Redactor,
    ) -> None:
        super().__init__(template.catalog.snapshot(), template.max_auto_activated)
        self.session_id = session_id
        self.run_id = run_id
        self.mode = mode
        self.store = store
        self.body_budget = body_budget
        self.redactor = redactor
        self.status: Literal["open", "finalizing", "closed"] = "open"
        self.bindings: dict[str, SkillBinding] = {}
        self.prepared: dict[str, PreparedSkill] = {}
        self.pending_resources: dict[int, PositionedMessage] = {}
        self.required: dict[int, ChatMessage] = {}
        self.request_ready = False
        self._estimator = TokenEstimator()

    def prepare(
        self,
        name: str,
        reason: str,
        *,
        explicit: bool,
        history: list[PositionedMessage],
    ) -> PreparedSkill:
        if self.status != "open":
            raise SkillContextError("skill_scope_closed", "当前 Run 不允许新增 Skill")
        skill = self.catalog.get(name)
        if skill is None:
            raise SkillContextError("skill_unavailable", f"Skill 不存在或不可用: {name}")
        existing = self.bindings.get(name)
        if (
            existing is None
            and not explicit
            and sum(not item.explicit for item in self.bindings.values()) >= self.max_auto_activated
        ):
            raise SkillContextError("skill_activation_limit", "自动激活 Skill 已达到数量上限")
        body = self.redactor.redact_text(self.render(skill))
        tokens = self._estimator.text(body)
        used = sum(item.tokens for key, item in self.bindings.items() if key != name)
        if used + tokens > self.body_budget:
            raise SkillContextError(
                "skill_context_budget_exceeded",
                f"活动正文需要 {used + tokens} tokens，预算为 {self.body_budget}",
            )
        reference = self.store.put_context_blob(
            session_id=self.session_id,
            run_id=self.run_id,
            content=body,
            media_type="text/vnd.bot.skill",
        )
        # Use the stored representation: the store may apply additional redaction.
        record = self.store.read_context_blob(
            self.session_id, reference, limit=len(body.encode()) + 1
        )
        if record is None or not record["eof"]:
            raise SkillContextError("skill_body_unavailable", name)
        body = record["content"]
        tokens = self._estimator.text(body)
        if used + tokens > self.body_budget:
            raise SkillContextError("skill_context_budget_exceeded", name)
        binding = SkillBinding(
            name=name,
            reason=reason,
            explicit=explicit or bool(existing and existing.explicit),
            version_hash=hashlib.sha256(body.encode()).hexdigest(),
            body_ref=reference,
            body_bytes=len(body.encode()),
            tokens=tokens,
        )
        return PreparedSkill(binding, body, self._find_body(history, binding))

    def commit(self, prepared: PreparedSkill, position: int | None) -> None:
        self.request_ready = False
        binding = prepared.binding
        binding.delivery_position = position
        self.bindings[binding.name] = binding
        self.active[binding.name] = ActiveSkill(
            name=binding.name,
            reason=binding.reason,
            explicit=binding.explicit,
        )

    @staticmethod
    def _find_body(history: list[PositionedMessage], binding: SkillBinding):
        for entry in reversed(history):
            delivery = entry.skill_delivery
            if (
                delivery is not None
                and delivery.is_body
                and delivery.skill_name == binding.name
                and delivery.version_hash == binding.version_hash
                and delivery.body_ref == binding.body_ref
            ):
                if not delivery.matches(entry.message):
                    raise SkillContextError("skill_body_version_mismatch", binding.name)
                return entry
        return None

    def append_body(
        self,
        prepared: PreparedSkill,
        *,
        kind: Literal["explicit_body", "restored_body"],
        key: str,
    ) -> PositionedMessage:
        binding = prepared.binding
        message = synthetic_user_context_message(
            name="skill_body",
            kind=kind,
            source=f"skill:{binding.name}",
            scope="run",
            content=prepared.body,
        )
        return self.store.append_skill_message(
            self.session_id,
            self.run_id,
            message,
            binding.delivery(kind),
            delivery_key=key,
        )

    def load_resource(self, skill_name: str, relative_path: str, *, max_chars: int = 100_000):
        if self.status == "closed":
            return None, "Skill Run 已关闭"
        return super().load_resource(skill_name, relative_path, max_chars=max_chars)

    def _read_body(self, reference: str, byte_count: int, digest: str) -> str:
        record = self.store.read_context_blob(self.session_id, reference, limit=byte_count + 1)
        if record is None:
            raise SkillContextError("skill_body_unavailable", reference)
        body = record["content"]
        if (
            not record["eof"]
            or len(body.encode()) != byte_count
            or hashlib.sha256(body.encode()).hexdigest() != digest
        ):
            raise SkillContextError("skill_body_version_mismatch", reference)
        return body

    def prepare_history(
        self,
        history: list[PositionedMessage],
        *,
        cursor: int,
    ) -> list[PositionedMessage]:
        """Restore only missing deliveries; keep the published history unchanged otherwise."""
        self.required.clear()
        self.request_ready = False
        if self.mode != "history":
            self.request_ready = True
            return history
        history = list(history)
        for binding in self.bindings.values():
            entry = self._find_body(history, binding)
            if entry is None:
                body = self._read_body(binding.body_ref, binding.body_bytes, binding.version_hash)
                entry = self.append_body(
                    PreparedSkill(binding, body, None),
                    kind="restored_body",
                    key=f"restore:{self.run_id}:{binding.name}:{binding.version_hash}:{cursor}",
                )
                if entry.position <= cursor:
                    raise SkillContextError("skill_body_not_visible", "恢复位置已被压缩覆盖")
                history.append(entry)
            binding.delivery_position = entry.position
            self.required[entry.position] = entry.message
        for position, resource in list(self.pending_resources.items()):
            entry = next((item for item in history if item.position == position), None)
            delivery = resource.skill_delivery
            if delivery is None:
                raise SkillContextError("skill_body_unavailable", "资源缺少交付来源")
            if entry is None:
                body = self._read_body(
                    delivery.body_ref, delivery.body_bytes, delivery.version_hash
                )
                message = synthetic_user_context_message(
                    name="skill_resource",
                    kind="skill_resource",
                    scope="run",
                    source=f"skill:{delivery.skill_name}",
                    content=body,
                )
                entry = self.store.append_skill_message(
                    self.session_id,
                    self.run_id,
                    message,
                    delivery,
                    delivery_key=f"resource:{self.run_id}:{position}:{cursor}",
                )
                history.append(entry)
                del self.pending_resources[position]
                self.pending_resources[entry.position] = entry
            if entry.skill_delivery is None or not entry.skill_delivery.matches(entry.message):
                raise SkillContextError("skill_body_version_mismatch", delivery.skill_name)
            self.required[entry.position] = entry.message
        self.request_ready = True
        return sorted(history, key=lambda entry: entry.position)

    def check_request(self, request: ModelRequest, provider: object) -> None:
        if self.status == "closed":
            raise SkillContextError("skill_scope_closed", "当前 Run 已关闭")
        if (
            self.mode == "history"
            and (self.bindings or self.pending_resources)
            and not self.request_ready
        ):
            raise SkillContextError("skill_body_not_visible", "Skill 请求准备未成功")
        if not self.required:
            return
        serialize = getattr(provider, "serialized_messages", None)
        actual = (
            serialize(request)
            if serialize is not None
            else [message.to_openai() for message in request.messages]
        )

        def key(message):
            return message.get("role"), message.get("tool_call_id"), message.get("content")

        available = Counter(key(message) for message in actual)
        needed = Counter(key(message.to_openai()) for message in self.required.values())
        if needed - available:
            raise SkillContextError("skill_body_not_visible", "最终请求缺少必要的完整 Skill 内容")
        pending: set[str] = set()
        for message in actual:
            if message.get("role") == "tool":
                call_id = message.get("tool_call_id")
                if call_id not in pending:
                    raise SkillContextError(
                        "skill_body_not_visible", "最终请求存在孤立 Tool Result"
                    )
                pending.remove(call_id)
                continue
            if pending:
                raise SkillContextError("skill_body_not_visible", "最终请求的工具调用组不完整")
            call_ids = [call.get("id") for call in message.get("tool_calls", [])]
            if any(not isinstance(call_id, str) or not call_id for call_id in call_ids):
                raise SkillContextError("skill_body_not_visible", "最终请求的工具调用缺少 ID")
            if len(set(call_ids)) != len(call_ids):
                raise SkillContextError("skill_body_not_visible", "最终请求的工具调用 ID 重复")
            pending.update(call_ids)
        if pending:
            raise SkillContextError("skill_body_not_visible", "最终请求缺少 Tool Result")

    def acknowledge_response(self) -> None:
        self.pending_resources.clear()

    def close(self) -> None:
        self.status = "closed"
        self.request_ready = False
        self.active.clear()
        self.bindings.clear()
        self.prepared.clear()
        self.pending_resources.clear()
        self.required.clear()
