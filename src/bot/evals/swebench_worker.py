from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from bot.cli.runtime import build_runtime
from bot.core.approval import AllowApprovalHandler
from bot.core.events import JsonlEventSink
from bot.core.models import RunRequest
from bot.evals.swebench import build_agent_prompt, load_instance


async def run_worker(instance_path: Path, workspace: Path, config_path: Path) -> int:
    if os.environ.get("SWEBENCH_CONTAINER") != "1" or not Path("/.dockerenv").exists():
        raise RuntimeError("SWE-bench Worker 只允许在显式标记的一次性容器中运行")
    workspace = workspace.resolve()
    if workspace != Path("/testbed"):
        raise RuntimeError(f"SWE-bench Worker 工作区必须是 /testbed，实际为 {workspace}")

    instance = load_instance(instance_path)
    approval_handler = AllowApprovalHandler()
    runtime = build_runtime(
        workspace=workspace,
        config_path=config_path,
        event_sinks=[JsonlEventSink()],
        approval_handler=approval_handler,
        config_overrides={
            "permissions": {"mode": "full-access", "network": "deny"},
            "subagents": {"enabled": False},
            "storage": {"state_path": "/testbed/.bot/state.db"},
            "skills": {"path": "/opt/kunpeng-bot-src/skills", "auto_activate": False},
        },
    )
    try:
        request = RunRequest(
            prompt=build_agent_prompt(instance),
            session_id=runtime.store.create_session(workspace),
            json_output=True,
        )
        result = await runtime.runner.run(request)
        print(result.model_dump_json())
        return 0 if result.status == "completed" else 1
    finally:
        await runtime.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Container-only SWE-bench Agent worker")
    parser.add_argument("instance_file", type=Path)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    return asyncio.run(
        run_worker(args.instance_file.resolve(), args.workspace.resolve(), args.config.resolve())
    )


if __name__ == "__main__":
    raise SystemExit(main())
