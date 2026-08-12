from bot.config.models import ProgressConfig
from bot.core.termination import ProgressController, TerminationAction


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
