"""Hold a real Native probe while the owning test signals the doctor CLI."""

import asyncio
import sys

from bot.cli.app import app
from bot.sandbox.diagnostics import SandboxDiagnostic
from bot.sandbox.preflight import _ProbeTarget

started = _ProbeTarget._after_start
close = SandboxDiagnostic.aclose


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
        print("probe-ready", file=sys.stderr, flush=True)
        await asyncio.Event().wait()


async def held_close(diagnostic):
    print("cleanup-ready", file=sys.stderr, flush=True)
    # The second signal is sent before this explicit barrier is released.
    await asyncio.to_thread(sys.stdin.buffer.readline)
    await close(diagnostic)


_ProbeTarget._after_start = held_start
SandboxDiagnostic.aclose = held_close
app()
