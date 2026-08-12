from bot.config.models import ProgressConfig
from bot.core.termination import (
    ProgressController,
    ProgressKind,
    ProgressSignal,
    TerminationAction,
)


def _observe_failure(controller: ProgressController) -> TerminationAction:
    controller.observe_tool(
        tool_name="read_file",
        arguments={"path": "missing.txt"},
        success=False,
        result_content="not found",
        metadata={},
        read_only=True,
        idempotent=True,
    )
    return controller.finish_step().action


def test_repeated_failure_warns_recovers_then_finalizes() -> None:
    controller = ProgressController(
        ProgressConfig(
            cycle_window_size=64,
            max_cycle_period=1,
            cycles_before_warning=10,
            cycles_before_recovery=12,
        )
    )

    assert _observe_failure(controller) == TerminationAction.CONTINUE
    assert _observe_failure(controller) == TerminationAction.WARN
    assert _observe_failure(controller) == TerminationAction.RECOVER
    assert _observe_failure(controller) == TerminationAction.WARN
    assert _observe_failure(controller) == TerminationAction.CONTINUE
    assert _observe_failure(controller) == TerminationAction.FINALIZE


def test_strong_progress_starts_a_fresh_epoch() -> None:
    controller = ProgressController(ProgressConfig())
    _observe_failure(controller)
    _observe_failure(controller)
    _observe_failure(controller)
    assert controller.recovery_attempts == 1

    controller.observe_tool(
        tool_name="write_file",
        arguments={"path": "output.txt"},
        success=True,
        result_content="written",
        metadata={"progress": {"changed": True}},
        read_only=False,
        idempotent=False,
    )
    report = controller.finish_step()

    assert report.action == TerminationAction.CONTINUE
    assert report.reason_code == "strong_progress"
    assert controller.epoch == 1
    assert controller.recovery_attempts == 0
    assert controller.no_progress_steps == 0


def test_progress_state_round_trips_without_raw_tool_data() -> None:
    config = ProgressConfig()
    controller = ProgressController(config)
    _observe_failure(controller)
    _observe_failure(controller)

    snapshot = controller.snapshot()
    restored = ProgressController(config, state=snapshot)

    assert restored.restored is True
    assert restored.state_summary() == controller.state_summary()
    assert "missing.txt" not in repr(snapshot)
    assert _observe_failure(restored) == TerminationAction.RECOVER


def test_failed_tool_cannot_claim_strong_progress() -> None:
    controller = ProgressController(ProgressConfig())
    controller.observe_tool(
        tool_name="bad_tool",
        arguments={},
        success=False,
        result_content="failed",
        metadata={},
        progress_signal=ProgressSignal(kind=ProgressKind.STRONG),
        read_only=False,
        idempotent=False,
    )

    report = controller.finish_step()

    assert report.progress == ProgressKind.NONE
    assert controller.epoch == 0


def test_live_quiet_process_warns_and_recovers_without_default_finalization() -> None:
    controller = ProgressController(
        ProgressConfig(
            process_inactivity_warning_seconds=1,
            process_inactivity_recovery_seconds=2,
            process_inactivity_finalize_seconds=None,
        )
    )

    def observe(inactivity: float):
        controller.observe_tool(
            tool_name="poll_process",
            arguments={"process_id": "proc-1"},
            success=True,
            result_content=f"elapsed={inactivity}",
            metadata={},
            progress_signal=ProgressSignal(
                kind=ProgressKind.WAITING,
                evidence_key="process:proc-1",
                summary="进程仍在运行",
                inactivity_seconds=inactivity,
            ),
            read_only=True,
            idempotent=False,
        )
        return controller.finish_step()

    assert observe(1.5).action == TerminationAction.WARN
    assert observe(2.5).action == TerminationAction.RECOVER
    report = observe(10_000)
    assert report.action == TerminationAction.CONTINUE
    assert report.reason_code == "process_waiting_after_recovery"
    assert controller.no_progress_steps == 0
    assert "process:proc-1" not in repr(controller.snapshot())


def test_quiet_process_can_have_an_explicit_finalize_policy() -> None:
    controller = ProgressController(
        ProgressConfig(
            process_inactivity_warning_seconds=1,
            process_inactivity_recovery_seconds=2,
            process_inactivity_finalize_seconds=3,
        )
    )

    for inactivity, expected in (
        (1.5, TerminationAction.WARN),
        (2.5, TerminationAction.RECOVER),
        (3.5, TerminationAction.FINALIZE),
    ):
        controller.observe_tool(
            tool_name="poll_process",
            arguments={"process_id": "proc-1"},
            success=True,
            result_content="running",
            metadata={},
            progress_signal=ProgressSignal(
                kind=ProgressKind.WAITING,
                evidence_key="process:proc-1",
                inactivity_seconds=inactivity,
            ),
            read_only=True,
            idempotent=False,
        )
        assert controller.finish_step().action == expected
