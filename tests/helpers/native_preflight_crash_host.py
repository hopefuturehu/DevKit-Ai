"""Fixed-probe crash barriers; only the owning test may terminate this host."""

import asyncio
import json
import sys
from pathlib import Path

from bot.config.models import AppConfig
from bot.execution.native import NativeSandboxExecutionTarget
from bot.sandbox.preflight import NativePreflight, _ProbeTarget
from bot.sandbox.preflight_recovery import ProbeResources


def packet(path, phase, process_id=None):
    record = json.loads((path / "resources.json").read_text())
    print(json.dumps({"phase": phase, "resources": record, "process_id": process_id}), flush=True)


async def main():
    runtime, work, state, phase = sys.argv[1:]
    target = NativeSandboxExecutionTarget(
        AppConfig.model_validate({"sandbox": {"mode": "required"}}),
        workspace=Path(work),
        runtime_root=Path(runtime),
        state_root=Path(state),
    )
    if phase == "resources":

        async def held_controls(probe):
            packet(probe.owned.path, phase)
            await asyncio.Event().wait()

        NativePreflight._positive_controls = held_controls
    elif phase == "registered":
        started = _ProbeTarget._after_start

        async def held_start(manager, process_id):
            await started(manager, process_id)
            if manager._managed_processes[process_id].spec.run_id == "readonly":
                async with asyncio.timeout(10):
                    while True:
                        result = await manager.poll_process(
                            process_id, wait_seconds=0.05, consume_output=False
                        )
                        if result.stdout.endswith("\n"):
                            break
                packet(manager.state_root.parent, phase, process_id)
                await asyncio.Event().wait()

        _ProbeTarget._after_start = held_start
    elif phase == "recoverer":

        async def held_recovery(manager, process_id, **kwargs):
            packet(manager.state_root.parent, phase, process_id)
            await asyncio.Event().wait()

        _ProbeTarget.terminate_process = held_recovery
    else:

        def held_removal(resources):
            if phase == "after_socket":
                resources.socket_directory.remove()
            packet(resources.owned.path, phase)
            sys.stdin.buffer.read(1)
            raise RuntimeError("test barrier cannot remove resource journal")

        ProbeResources.remove = held_removal
    try:
        await target.prepare_run()
        raise RuntimeError("crash barrier was not reached")
    finally:
        await target.aclose()


asyncio.run(main())
