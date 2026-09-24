import asyncio

import pytest

from bot.execution.base import ProcessEvent, ProcessEventKind, ProcessSpec
from bot.execution.scoped import InvocationIdentity, NativeInvocationTarget
from bot.sandbox.models import ExecutionScope, SandboxError


@pytest.mark.asyncio
async def test_invocation_stream_close_finishes_inner_cleanup_before_returning(tmp_path):
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Backend:
        async def prepare_run(self):
            pass

        def check_call_available(self, *args):
            pass

        def bind_scope(self, **identity):
            return ExecutionScope(
                session_id=identity["session_id"],
                run_id=identity["run_id"],
                tool_call_id=identity["tool_call_id"],
                workspace=tmp_path,
                policy_id="test",
            )

        async def execute(self, spec):
            try:
                yield ProcessEvent(kind=ProcessEventKind.STDOUT, data="ready")
                await asyncio.Event().wait()
            finally:
                entered.set()
                await release.wait()
                finished.set()

    view = NativeInvocationTarget(
        Backend(), InvocationIdentity(tmp_path, "session", "run", "call", False, ())
    )
    stream = view.execute(ProcessSpec(argv=["unused"], cwd=tmp_path))
    assert (await anext(stream)).data == "ready"
    closing = asyncio.create_task(stream.aclose())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert not closing.done() and not finished.is_set()
    finally:
        release.set()
        await closing
    assert finished.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_spoofed_identity_is_rejected_before_startup_side_effects(tmp_path, streaming):
    class Backend:
        async def prepare_run(self):
            pytest.fail("invalid identity must not start recovery or a probe")

    view = NativeInvocationTarget(
        Backend(), InvocationIdentity(tmp_path, "session", "run", "call", False, ())
    )
    spec = ProcessSpec(argv=["unused"], cwd=tmp_path, run_id="forged")
    with pytest.raises(SandboxError, match="不能替换"):
        if streaming:
            await anext(view.execute(spec))
        else:
            await view.start_process(spec)
