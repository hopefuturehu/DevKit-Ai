from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from bot.cli.runtime import build_runtime
from bot.core.approval import AllowApprovalHandler
from bot.core.events import JsonlEventSink
from bot.core.models import RunRequest
from bot.evals.swebench import (
    DEFAULT_SWEBENCH_MAX_COST_USD,
    DEFAULT_SWEBENCH_MAX_STEPS,
    DEFAULT_SWEBENCH_MAX_WALL_TIME_SECONDS,
    build_agent_prompt,
    load_instance,
)
from bot.observability import backup_sqlite_database


def _validate_container_artifact_path(path: Path | None) -> Path | None:
    if path is None:
        return None
    resolved = path.resolve()
    temporary_root = Path("/tmp").resolve()
    try:
        resolved.relative_to(temporary_root)
    except ValueError as exc:
        raise RuntimeError(f"SWE-bench Worker 产物路径必须位于 /tmp: {resolved}") from exc
    return resolved


def _worker_config_overrides(
    *, max_steps: int, max_wall_time_seconds: float, max_cost_usd: float | None
) -> dict[str, object]:
    if max_steps < 1:
        raise ValueError("SWE-bench max_steps 必须大于 0")
    if max_wall_time_seconds <= 0:
        raise ValueError("SWE-bench max_wall_time_seconds 必须大于 0")
    if max_cost_usd is not None and max_cost_usd <= 0:
        raise ValueError("SWE-bench max_cost_usd 必须大于 0")
    return {
        "agent": {
            "max_steps": max_steps,
            "max_wall_time_seconds": max_wall_time_seconds,
            "max_cost_usd": max_cost_usd,
        },
        "permissions": {"mode": "full-access", "network": "deny"},
        "subagents": {"enabled": False},
        "storage": {"state_path": "/testbed/.bot/state.db"},
        "skills": {"path": "/opt/kunpeng-bot-src/skills", "auto_activate": False},
    }


async def run_worker(
    instance_path: Path,
    workspace: Path,
    config_path: Path,
    *,
    result_path: Path | None = None,
    state_backup_path: Path | None = None,
    max_steps: int = DEFAULT_SWEBENCH_MAX_STEPS,
    max_wall_time_seconds: float = DEFAULT_SWEBENCH_MAX_WALL_TIME_SECONDS,
    max_cost_usd: float | None = DEFAULT_SWEBENCH_MAX_COST_USD,
) -> int:
    if os.environ.get("SWEBENCH_CONTAINER") != "1" or not Path("/.dockerenv").exists():
        raise RuntimeError("SWE-bench Worker 只允许在显式标记的一次性容器中运行")
    workspace = workspace.resolve()
    if workspace != Path("/testbed"):
        raise RuntimeError(f"SWE-bench Worker 工作区必须是 /testbed，实际为 {workspace}")
    result_path = _validate_container_artifact_path(result_path)
    state_backup_path = _validate_container_artifact_path(state_backup_path)

    instance = load_instance(instance_path)
    approval_handler = AllowApprovalHandler()
    runtime = build_runtime(
        workspace=workspace,
        config_path=config_path,
        event_sinks=[JsonlEventSink()],
        approval_handler=approval_handler,
        config_overrides=_worker_config_overrides(
            max_steps=max_steps,
            max_wall_time_seconds=max_wall_time_seconds,
            max_cost_usd=max_cost_usd,
        ),
    )
    result = None
    try:
        request = RunRequest(
            prompt=build_agent_prompt(instance),
            session_id=runtime.store.create_session(workspace),
            json_output=True,
        )
        result = await runtime.runner.run(request)
    finally:
        await runtime.aclose()
        if state_backup_path is not None:
            backup_sqlite_database(workspace / ".bot" / "state.db", state_backup_path)

    if result is None:
        return 1
    if result_path is None:
        print(result.model_dump_json())
    else:
        result_path.write_text(result.model_dump_json() + "\n", encoding="utf-8")
        result_path.chmod(0o600)
    return 0 if result.status == "completed" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Container-only SWE-bench Agent worker")
    parser.add_argument("instance_file", type=Path)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--result-file", type=Path)
    parser.add_argument("--state-backup", type=Path)
    parser.add_argument("--max-steps", type=int, default=DEFAULT_SWEBENCH_MAX_STEPS)
    parser.add_argument(
        "--max-wall-time-seconds",
        type=float,
        default=DEFAULT_SWEBENCH_MAX_WALL_TIME_SECONDS,
    )
    cost = parser.add_mutually_exclusive_group()
    cost.add_argument("--max-cost-usd", type=float, default=DEFAULT_SWEBENCH_MAX_COST_USD)
    cost.add_argument("--no-cost-limit", dest="max_cost_usd", action="store_const", const=None)
    args = parser.parse_args(argv)
    return asyncio.run(
        run_worker(
            args.instance_file.resolve(),
            args.workspace.resolve(),
            args.config.resolve(),
            result_path=args.result_file,
            state_backup_path=args.state_backup,
            max_steps=args.max_steps,
            max_wall_time_seconds=args.max_wall_time_seconds,
            max_cost_usd=args.max_cost_usd,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
