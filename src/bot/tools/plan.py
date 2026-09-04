from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import ValidationError

from bot.core.plan import PlanStatus, PlanUpdate
from bot.core.progress import ProgressKind, ProgressSignal
from bot.tools.base import Tool, ToolAnnotations, ToolContext, ToolResult


class UpdatePlanTool(Tool):
    name = "update_plan"
    description = (
        "为复杂、多步骤任务创建或更新当前会话的 TODO list。每次必须提交完整当前列表，"
        "而不是只提交变化项；简单任务不要使用。开始某项前将其设为 in_progress，验证完成后"
        "立即设为 completed，并保持最多一个 in_progress。传空列表可清空 TODO。"
    )
    input_schema = PlanUpdate.model_json_schema()
    annotations = ToolAnnotations(read_only=True, idempotent=True)

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        del context
        try:
            update = PlanUpdate.model_validate(arguments)
        except ValidationError as exc:
            return ToolResult(success=False, error=f"计划校验失败: {exc.errors()[0]['msg']}")

        payload = update.model_dump(mode="json")
        counts = {
            status.value: sum(item.status == status for item in update.items)
            for status in PlanStatus
        }
        output = json.dumps(
            {"message": "TODO list 已更新", **payload, "counts": counts},
            ensure_ascii=False,
        )
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        return ToolResult(
            success=True,
            output=output,
            metadata={"plan_update": payload, "counts": counts},
            progress=ProgressSignal(
                kind=ProgressKind.WEAK,
                summary="更新了会话 TODO list",
                evidence_key=f"plan:{digest}",
            ),
        )
