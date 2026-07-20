from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from time import monotonic

from bot.cli.runtime import Runtime, build_runtime
from bot.core.events import EventType, MemoryEventSink
from bot.core.models import RunRequest
from bot.evals.models import EvalCase, EvalResult


def load_eval_cases(path: Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            cases.append(EvalCase.model_validate_json(stripped))
        except Exception as exc:
            raise ValueError(f"{path}:{line_number}: Eval case 无效: {exc}") from exc
    identifiers = [case.id for case in cases]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"{path}: Eval case id 重复")
    return cases


async def run_eval_case(
    case: EvalCase,
    *,
    base: Path,
    config_path: Path | None,
    disable_skills: bool = False,
    runtime_builder: Callable[..., Runtime] = build_runtime,
) -> EvalResult:
    workspace = case.workspace_path(base)
    if not workspace.is_dir():
        return EvalResult(
            id=case.id,
            passed=False,
            status="invalid",
            failures=[f"工作区不存在: {workspace}"],
            duration_seconds=0,
            steps=0,
            tool_calls=0,
            approval_requests=0,
            input_tokens=0,
            output_tokens=0,
        )
    events = MemoryEventSink()
    runtime = runtime_builder(
        workspace=workspace,
        config_path=config_path,
        event_sinks=[events],
        approval_handler=None,
    )
    if disable_skills:
        runtime.catalog.skills.clear()
        runtime.skills.reset()
    started = monotonic()
    try:
        explicit_skills = [] if disable_skills else case.explicit_skills
        result = await runtime.runner.run(
            RunRequest(prompt=case.prompt, explicit_skills=explicit_skills)
        )
    finally:
        duration = monotonic() - started
        async_close = getattr(runtime, "aclose", None)
        if async_close is not None:
            await async_close()
        else:
            runtime.close()

    failures: list[str] = []
    if result.status != case.expected_status:
        failures.append(f"状态期望 {case.expected_status}，实际 {result.status}")
    for expected in case.final_contains:
        if expected not in result.final_text:
            failures.append(f"最终答案缺少: {expected}")
    for forbidden in case.final_not_contains:
        if forbidden in result.final_text:
            failures.append(f"最终答案包含禁止内容: {forbidden}")
    for relative_path, expected_parts in case.files_contain.items():
        target = (workspace / relative_path).resolve()
        try:
            target.relative_to(workspace)
        except ValueError:
            failures.append(f"校验文件超出工作区: {relative_path}")
            continue
        if not target.is_file():
            failures.append(f"预期文件不存在: {relative_path}")
            continue
        content = target.read_text(encoding="utf-8", errors="replace")
        for expected in expected_parts:
            if expected not in content:
                failures.append(f"{relative_path} 缺少: {expected}")

    tool_names = [
        str(event.payload.get("name", ""))
        for event in events.events
        if event.type == EventType.TOOL_REQUESTED
    ]
    activated_skills = [
        str(event.payload.get("name", ""))
        for event in events.events
        if event.type == EventType.SKILL_ACTIVATED
    ]
    approval_requests = sum(event.type == EventType.APPROVAL_REQUESTED for event in events.events)
    for expected in case.expected_tools:
        if expected not in tool_names:
            failures.append(f"未调用预期 Tool: {expected}")
    for forbidden in case.forbidden_tools:
        if forbidden in tool_names:
            failures.append(f"调用了禁止 Tool: {forbidden}")
    if not disable_skills:
        for expected in case.expected_skills:
            if expected not in activated_skills:
                failures.append(f"未激活预期 Skill: {expected}")
    if case.max_tool_calls is not None and len(tool_names) > case.max_tool_calls:
        failures.append(f"Tool Call 超限: {len(tool_names)} > {case.max_tool_calls}")
    if case.max_approval_requests is not None and approval_requests > case.max_approval_requests:
        failures.append(f"审批请求超限: {approval_requests} > {case.max_approval_requests}")

    return EvalResult(
        id=case.id,
        passed=not failures,
        status=result.status,
        failures=failures,
        duration_seconds=duration,
        steps=result.steps,
        tool_calls=len(tool_names),
        tool_names=tool_names,
        activated_skills=activated_skills,
        approval_requests=approval_requests,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
        final_text=result.final_text,
    )


def write_eval_results(path: Path, results: list[EvalResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False) + "\n" for result in results
    )
    path.write_text(content, encoding="utf-8")
