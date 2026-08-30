from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from bot.cli.runtime import build_runtime
from bot.core.approval import AllowApprovalHandler
from bot.core.events import JsonlEventSink
from bot.core.models import RunRequest
from bot.observability import export_trace_bundle


def _worker_config_overrides(
    *,
    state_path: Path,
    max_steps: int,
    max_wall_time_seconds: float,
    max_cost_usd: float | None,
    subagents_enabled: bool,
) -> dict[str, object]:
    if max_steps < 1:
        raise ValueError("Terminal-Bench max_steps 必须大于 0")
    if max_wall_time_seconds <= 0:
        raise ValueError("Terminal-Bench max_wall_time_seconds 必须大于 0")
    if max_cost_usd is not None and max_cost_usd <= 0:
        raise ValueError("Terminal-Bench max_cost_usd 必须大于 0")
    agent_overrides: dict[str, object] = {
        "max_steps": max_steps,
        "max_wall_time_seconds": max_wall_time_seconds,
    }
    if max_cost_usd is not None:
        agent_overrides["max_cost_usd"] = max_cost_usd
    return {
        "agent": agent_overrides,
        "permissions": {
            "mode": "full-access",
            "workspace_only": False,
            "network": "allow",
        },
        "subagents": {"enabled": subagents_enabled},
        "storage": {"state_path": str(state_path)},
        "skills": {
            "path": "/installed-agent/no-skills",
            "auto_activate": False,
            "max_auto_activated": 0,
        },
    }


def _validate_log_path(path: Path) -> Path:
    resolved = path.resolve()
    log_root = Path("/logs/agent").resolve()
    try:
        resolved.relative_to(log_root)
    except ValueError as exc:
        raise RuntimeError(
            f"Terminal-Bench Worker 产物路径必须位于 /logs/agent: {resolved}"
        ) from exc
    return resolved


async def run_worker(
    instruction_path: Path,
    workspace: Path,
    config_path: Path,
    *,
    events_path: Path,
    result_path: Path,
    state_path: Path,
    trace_path: Path,
    max_steps: int,
    max_wall_time_seconds: float,
    max_cost_usd: float | None,
    subagents_enabled: bool,
) -> int:
    if os.environ.get("HARBOR_CONTAINER") != "1" or not Path("/.dockerenv").exists():
        raise RuntimeError("Terminal-Bench Worker 只允许在 Harbor 显式标记的一次性容器中运行")
    workspace = workspace.resolve()
    if not workspace.is_dir():
        raise RuntimeError(f"Terminal-Bench 工作区不存在: {workspace}")
    instruction_path = instruction_path.resolve()
    config_path = config_path.resolve()
    if not instruction_path.is_file():
        raise RuntimeError(f"Terminal-Bench instruction 不存在: {instruction_path}")
    if not config_path.is_file():
        raise RuntimeError(f"Terminal-Bench 配置不存在: {config_path}")

    events_path = _validate_log_path(events_path)
    result_path = _validate_log_path(result_path)
    state_path = _validate_log_path(state_path)
    trace_path = _validate_log_path(trace_path)
    events_path.parent.mkdir(parents=True, exist_ok=True)

    with events_path.open("w", encoding="utf-8") as event_stream:
        runtime = build_runtime(
            workspace=workspace,
            config_path=config_path,
            event_sinks=[JsonlEventSink(event_stream)],
            approval_handler=AllowApprovalHandler(),
            config_overrides=_worker_config_overrides(
                state_path=state_path,
                max_steps=max_steps,
                max_wall_time_seconds=max_wall_time_seconds,
                max_cost_usd=max_cost_usd,
                subagents_enabled=subagents_enabled,
            ),
        )
        result = None
        try:
            request = RunRequest(
                prompt=instruction_path.read_text(encoding="utf-8"),
                session_id=runtime.store.create_session(workspace),
                json_output=True,
            )
            result = await runtime.runner.run(request)
        finally:
            await runtime.aclose()

    if result is None:
        return 1
    result_path.write_text(result.model_dump_json() + "\n", encoding="utf-8")
    for path in (events_path, result_path, state_path):
        path.chmod(0o600)
    try:
        export_trace_bundle(
            events_path=events_path,
            state_path=state_path,
            output_dir=trace_path,
            result_path=result_path,
        )
    except Exception as exc:
        print(f"trace export failed: {type(exc).__name__}: {exc}", flush=True)
    print(result.model_dump_json(), flush=True)
    # Harbor should still run the task verifier when the internal step/cost/time
    # limit leaves a useful partial solution in the container.
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Harbor-only Terminal-Bench Agent worker")
    parser.add_argument("--instruction", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--events",
        type=Path,
        default=Path("/logs/agent/events.jsonl"),
    )
    parser.add_argument(
        "--result",
        type=Path,
        default=Path("/logs/agent/result.json"),
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=Path("/logs/agent/state.db"),
    )
    parser.add_argument(
        "--trace",
        type=Path,
        default=Path("/logs/agent/trace"),
    )
    parser.add_argument("--max-steps", type=int, default=60)
    parser.add_argument("--max-wall-time-seconds", type=float, default=1800)
    parser.add_argument("--max-cost-usd", type=float)
    parser.add_argument(
        "--subagents-enabled",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    args = parser.parse_args(argv)
    return asyncio.run(
        run_worker(
            args.instruction,
            args.workspace,
            args.config,
            events_path=args.events,
            result_path=args.result,
            state_path=args.state,
            trace_path=args.trace,
            max_steps=args.max_steps,
            max_wall_time_seconds=args.max_wall_time_seconds,
            max_cost_usd=args.max_cost_usd,
            subagents_enabled=args.subagents_enabled,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
