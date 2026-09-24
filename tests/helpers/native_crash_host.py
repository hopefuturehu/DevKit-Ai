"""Disposable trusted test host. The parent deliberately SIGKILLs this process."""

import asyncio
import json
import sys
from pathlib import Path

from bot.config.models import AppConfig
from bot.execution.base import ProcessSpec
from bot.execution.native import NativeSandboxExecutionTarget


async def main():
    runtime, work, state, phase = sys.argv[1:]
    target = NativeSandboxExecutionTarget(
        AppConfig.model_validate({"sandbox": {"mode": "required"}}),
        workspace=Path(work),
        runtime_root=Path(runtime),
        state_root=Path(state),
    )
    try:
        await target.ensure_ready()
        scope = target.bind_scope(
            session_id="crash-session", run_id="crash-run", tool_call_id="call"
        )
        if phase == "prepared":
            before_spawn = target._before_spawn

            def stop_before_spawn(spec, launch, baseline):
                before_spawn(spec, launch, baseline)
                print(
                    json.dumps({"phase": "prepared", "lease": target._leases[scope].record()}),
                    flush=True,
                )
                # The parent kills this host while the synchronous pre-spawn
                # hook is blocked. Returning must NEVER allow a business fork.
                sys.stdin.buffer.read(1)
                raise RuntimeError("test pre-spawn barrier cannot dispatch")

            target._before_spawn = stop_before_spawn
        process_id = await target.start_process(
            ProcessSpec(
                argv=[
                    str(Path(runtime) / "node/bin/node"),
                    "-e",
                    "require('fs').appendFileSync('business-started','1');"
                    "console.log('ready');setInterval(()=>{},1000)",
                ],
                cwd=Path(work),
                session_id=scope.session_id,
                run_id=scope.run_id,
                scope=scope,
            )
        )
        async with asyncio.timeout(10):
            while (  # noqa: ASYNC110 - native output handshake
                "ready" not in (await target.poll_process(process_id, consume_output=False)).stdout
            ):
                await asyncio.sleep(0.02)
        print(
            json.dumps(
                {
                    "phase": "registered",
                    "process_id": process_id,
                    "lease": target._leases[scope].record(),
                }
            ),
            flush=True,
        )
        if phase == "removed_control":
            lease = target._leases[scope]

            def stop_after_control_removal():
                print(
                    json.dumps(
                        {
                            "phase": "removed_control",
                            "process_id": process_id,
                            "lease": lease.record(),
                        }
                    ),
                    flush=True,
                )
                sys.stdin.buffer.read(1)
                raise RuntimeError("test barrier cannot release the journal")

            lease.release = stop_after_control_removal
            await target.terminate_process(process_id)
        await asyncio.Event().wait()
    finally:
        await target.aclose()


asyncio.run(main())
