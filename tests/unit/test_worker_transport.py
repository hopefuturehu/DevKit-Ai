import asyncio
import json
import sys

import pytest

from bot.execution.base import ProcessSpec, ProcessStatus
from bot.execution.local import LocalExecutionTarget
from bot.sandbox.client import execute_worker
from bot.sandbox.models import ExecutionScope, SandboxError


def request(workspace):
    return {
        "v": 1,
        "id": "transport",
        "op": "file_version",
        "args": {"path": "x"},
        "context": {
            "workspace": str(workspace),
            "workspace_only": True,
            "read_only": True,
            "denied_paths": [],
            "max_output_bytes": 100,
            "network_mode": "deny",
            "allowed_domains": [],
        },
    }


def spec(workspace, script, *, timeout=5, output_limit=10000):
    return ProcessSpec(
        argv=[sys.executable, "-I", "-S", "-B", "-c", script],
        cwd=workspace,
        scope=ExecutionScope(
            session_id="session",
            run_id="run",
            tool_call_id="tool",
            workspace=workspace,
            policy_id="protocol-test-only",
        ),
        session_id="session",
        run_id="run",
        interactive=True,
        timeout_seconds=timeout,
        output_limit_bytes=output_limit,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["empty", "truncated", "second", "oversized", "nonzero", "invalid_utf8", "wrong_id"]
)
async def test_bad_worker_transport_never_returns_success_and_reclaims(case, tmp_path):
    reply = (
        json.dumps(
            {
                "v": 1,
                "id": "transport",
                "result": {
                    "success": True,
                    "metadata": {"path": str(tmp_path / "x"), "sha256": "a" * 64},
                },
            }
        )
        + "\n"
    )
    script = {
        "empty": "pass",
        "truncated": f"sys.stdout.write({reply[:-1]!r})",
        "second": f"sys.stdout.write({(reply + reply)!r})",
        "oversized": "sys.stdout.write('x'*20000);sys.stdout.flush();time.sleep(30)",
        "nonzero": f"sys.stdout.write({reply!r});sys.exit(7)",
        "invalid_utf8": "sys.stdout.buffer.write(b'\\xff\\n')",
        "wrong_id": f"sys.stdout.write({reply.replace('transport', 'foreign')!r})",
    }[case]
    target = LocalExecutionTarget()
    try:
        with pytest.raises(SandboxError) as failure:
            await execute_worker(
                target,
                spec(tmp_path, "import sys,time;sys.stdin.read();" + script),
                request(tmp_path),
            )
        assert failure.value.executed is None
        assert failure.value.code.value in {"TOOL_PROTOCOL_ERROR", "COMMAND_FAILED"}
        assert not target.has_live_processes
        process = (await target.list_processes())[0]
        assert process.remaining_processes == [] and process.output_complete
        # Natural completion is already observed by the manager and does not
        # request termination. Only the overflowing live worker needs cleanup.
        assert process.cleanup_status == (
            "observed_empty" if case == "oversized" else "not_requested"
        )
    finally:
        await target.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("unresolved", [False, True])
async def test_worker_pipe_error_is_structured_and_cleanup_failure_takes_precedence(
    tmp_path, monkeypatch, unresolved
):
    target = LocalExecutionTarget()
    terminate = target.terminate_process

    async def broken_pipe(*args, **kwargs):
        raise BrokenPipeError("worker stopped reading")

    async def cleanup(*args, **kwargs):
        result = await terminate(*args, **kwargs)
        # Fault-inject the manager's unresolved result after reclaiming the
        # real test process. This tests error propagation, not OS containment.
        return (
            result.model_copy(
                update={"status": ProcessStatus.CLEANUP_FAILED, "cleanup_status": "unknown"}
            )
            if unresolved
            else result
        )

    monkeypatch.setattr(target, "send_process_input", broken_pipe)
    monkeypatch.setattr(target, "terminate_process", cleanup)
    try:
        with pytest.raises(SandboxError) as failure:
            await execute_worker(
                target, spec(tmp_path, "import time;time.sleep(30)"), request(tmp_path)
            )
        assert failure.value.code.value == (
            "CLEANUP_FAILED" if unresolved else "TOOL_PROTOCOL_ERROR"
        )
        assert failure.value.stage == ("worker_cleanup" if unresolved else "worker_transport")
        assert failure.value.executed is None
        assert not target.has_live_processes
    finally:
        await target.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_worker_timeout_and_cancellation_wait_for_cleanup(tmp_path, cancel):
    target = LocalExecutionTarget()
    job = asyncio.create_task(
        execute_worker(
            target,
            spec(
                tmp_path,
                "import sys,time;sys.stdin.read();time.sleep(30)",
                timeout=5 if cancel else 0.15,
            ),
            request(tmp_path),
        )
    )
    try:
        if cancel:
            async with asyncio.timeout(5):
                while not target.has_live_processes:  # noqa: ASYNC110 - managed spawn handshake
                    await asyncio.sleep(0.01)
            job.cancel()
            with pytest.raises(asyncio.CancelledError):
                await job
        else:
            with pytest.raises(TimeoutError):
                await job
        assert not target.has_live_processes
        process = (await target.list_processes())[0]
        assert process.status in {ProcessStatus.CANCELLED, ProcessStatus.TIMED_OUT}
        assert process.cleanup_status == "observed_empty"
    finally:
        await target.aclose()
