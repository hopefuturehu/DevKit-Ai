import asyncio
import json
import os

import pytest

from bot.sandbox.launch_status import MAX_STATUS_BYTES, LaunchStatusPipe, decode_status
from bot.sandbox.models import SandboxError


def frames(identifier, *, state="dispatched", stage="spawn"):
    return b"".join(
        (json.dumps({"v": 1, "id": identifier, "state": s, "stage": p}) + "\n").encode()
        for s, p in (("initializing", "initialize"), (state, stage))
    )


@pytest.mark.parametrize(
    ("state", "stage", "executed"),
    [("dispatched", "spawn", None), ("failed", "policy", False), ("unknown", "spawn", None)],
)
def test_status_keeps_dispatch_distinct_from_target_exec(state, stage, executed):
    result = decode_status(frames("fixed", state=state, stage=stage), "fixed")
    assert result.state == state and result.stage == stage and result.executed is executed


@pytest.mark.parametrize(
    "case",
    ["empty", "partial", "extra", "wrong_id", "bool_version", "bad_state", "bad_stage", "huge"],
)
def test_status_rejects_ambiguous_or_forged_frames(case):
    raw = frames("fixed")
    if case == "empty":
        raw = b""
    elif case == "partial":
        raw = raw[:-1]
    elif case == "extra":
        raw += b"{}\n"
    elif case == "wrong_id":
        raw = frames("other")
    elif case == "bool_version":
        raw = raw.replace(b'"v": 1', b'"v": true')
    elif case == "bad_state":
        raw = frames("fixed", state=[])
    elif case == "bad_stage":
        raw = frames("fixed", stage=[])
    elif case == "huge":
        raw = b"x" * (MAX_STATUS_BYTES + 1)
    with pytest.raises(ValueError):
        decode_status(raw, "fixed")


@pytest.mark.asyncio
async def test_pipe_requires_eof_and_accepts_fragmented_data():
    pipe = LaunchStatusPipe()
    writer = os.dup(pipe.writer)
    pipe.after_spawn()
    task = asyncio.create_task(pipe.wait())
    try:
        raw = frames(pipe.invocation)
        for part in (raw[:7], raw[7:60], raw[60:]):
            os.write(writer, part)
            await asyncio.sleep(0)
        assert not task.done()
        os.close(writer)
        writer = None
        result = await asyncio.wait_for(task, 1)
        assert result.state == "dispatched" and pipe.eof
        assert pipe.reader is None and pipe.writer is None
    finally:
        if writer is not None:
            os.close(writer)
        pipe.close()


@pytest.mark.asyncio
async def test_close_preserves_status_for_a_command_that_exited_before_reader_registration():
    pipe = LaunchStatusPipe()
    os.write(pipe.writer, frames(pipe.invocation))
    pipe.close()
    pipe.close()
    assert (await pipe.wait()).state == "dispatched"


@pytest.mark.asyncio
async def test_incomplete_pipe_cannot_attest_nonexecution():
    pipe = LaunchStatusPipe()
    pipe.after_spawn()
    try:
        with pytest.raises(SandboxError) as failure:
            await pipe.wait()
        assert failure.value.executed is None
        assert failure.value.stage == "launcher_status"
    finally:
        pipe.close()


@pytest.mark.asyncio
async def test_waiter_cancellation_keeps_pipe_owned_until_explicit_cleanup():
    pipe = LaunchStatusPipe()
    task = asyncio.create_task(pipe.wait())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pipe.reader is not None and pipe.writer is not None
    pipe.close()
    assert pipe.reader is None and pipe.writer is None


@pytest.mark.asyncio
async def test_status_timeout_stays_unknown_and_cleanup_releases_descriptors(monkeypatch):
    from bot.sandbox import launch_status

    monkeypatch.setattr(launch_status, "STARTUP_TIMEOUT", 0.01)
    pipe = LaunchStatusPipe()
    try:
        with pytest.raises(SandboxError) as failure:
            await pipe.wait()
        assert failure.value.executed is None
        assert pipe.reader is not None
    finally:
        pipe.close()
