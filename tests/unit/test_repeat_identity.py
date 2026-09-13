import pytest

from bot.config.models import ProgressConfig
from bot.core.progress import ProgressKind, ProgressSignal
from bot.core.termination import ProgressController, TerminationAction
from bot.core.termination.identity import call_identity, fingerprint, process_evidence


def observe(controller, name="run_command", arguments=None, result="stable", **kwargs):
    controller.observe_tool(
        tool_name=name,
        arguments=arguments or {"argv": ["true"]},
        success=True,
        result_content=result,
        metadata={},
        progress_signal=ProgressSignal(
            kind=ProgressKind.WEAK,
            evidence_key=result,
            evidence_complete=True,
        ),
        read_only=False,
        idempotent=False,
        repeat_eligible=True,
        **kwargs,
    )
    return controller.finish_step()


def enforced(**kwargs):
    return ProgressController(ProgressConfig(repeat_guard_mode="enforce", **kwargs))


def test_command_identity_normalizes_only_execution_neutral_arguments():
    base = call_identity("run_command", {"argv": ["/bin/sh", "-c", "printf x"]})
    assert base == call_identity(
        "run_shell",
        {
            "script": "printf x",
            "wait_seconds": 59,
            "cwd": ".",
            "interactive": False,
            "timeout_seconds": None,
        },
    )
    assert base != call_identity("run_shell", {"script": "printf  x"})
    assert base != call_identity("run_shell", {"script": "printf x", "cwd": "/elsewhere"})
    assert base != call_identity("run_shell", {"script": "printf x"}, scope="other-target")
    assert base != call_identity("run_shell", {"script": "printf x"}, hard_timeout=2)
    assert call_identity("plugin", {"a": 1, "b": 2}) == call_identity("plugin", {"b": 2, "a": 1})
    assert call_identity("plugin", {"a": [1, 2]}) != call_identity("plugin", {"a": [2, 1]})


@pytest.mark.parametrize(
    "tool,args,changed",
    [
        ("read_file", {"path": "a"}, {"path": "b"}),
        ("read_file", {"path": "a"}, {"path": "a", "start_line": 2}),
        ("search_text", {"query": "x"}, {"query": "x", "glob": "*.py"}),
        ("load_context_reference", {"reference": "x"}, {"reference": "x", "offset": 100}),
    ],
)
def test_resource_ranges_are_distinct(tool, args, changed):
    assert call_identity(tool, args) != call_identity(tool, changed)


def test_process_stream_boundaries_and_exit_status_are_significant():
    base = process_evidence("completed", 0, fingerprint("ab"), fingerprint("c"))
    assert base != process_evidence("completed", 0, fingerprint("a"), fingerprint("bc"))
    assert base != process_evidence("failed", 1, fingerprint("ab"), fingerprint("c"))


def test_enforced_repeat_limits_are_independent_of_idempotency_and_wait_time():
    controller = enforced()
    for n, expected in enumerate(
        [
            TerminationAction.CONTINUE,
            TerminationAction.WARN,
            TerminationAction.RECOVER,
        ],
        1,
    ):
        assert (
            observe(controller, arguments={"argv": ["true"], "wait_seconds": n}).action == expected
        )
    key = call_identity("run_command", {"argv": ["true"]})
    for expected in [TerminationAction.CONTINUE, TerminationAction.FINALIZE]:
        assert controller.repeat_guard.check(key).denied
        assert controller.finish_step().action == expected


def test_new_result_restarts_evidence_count():
    controller = enforced()
    observe(controller)
    observe(controller)
    assert observe(controller, result="changed").progress == ProgressKind.WEAK
    assert controller.repeat_guard.check(call_identity("run_command", {"argv": ["true"]})) is None


def test_unrelated_strong_progress_and_waiting_do_not_hide_a_repeat():
    controller = enforced()
    observe(controller)
    observe(controller)
    controller.observe_tool(
        tool_name="apply_patch",
        arguments={"path": "unrelated"},
        success=True,
        result_content="changed",
        metadata={},
        read_only=False,
        idempotent=False,
        progress_signal=ProgressSignal(kind=ProgressKind.STRONG, subject_key="file:unrelated"),
    )
    controller.observe_tool(
        tool_name="poll_process",
        arguments={"process_id": "alive"},
        success=True,
        result_content="running",
        metadata={},
        read_only=True,
        idempotent=False,
        progress_signal=ProgressSignal(kind=ProgressKind.WAITING, evidence_key="process:alive"),
    )
    assert observe(controller).action == TerminationAction.RECOVER
    assert controller.epoch == 1
    assert controller.repeat_guard.check(call_identity("run_command", {"argv": ["true"]})).denied


def test_alternating_calls_keep_separate_repeat_evidence():
    controller = enforced()
    for _ in range(3):
        for command in ("a", "b"):
            observe(controller, arguments={"argv": [command]}, result=command)
    for command in ("a", "b"):
        assert controller.repeat_guard.check(
            call_identity("run_command", {"argv": [command]})
        ).denied


def test_limits_survive_restore_config_change_and_are_scoped():
    controller = enforced()
    for _ in range(3):
        observe(controller)
    saved = controller.snapshot()
    assert "true" not in repr(saved)
    restored = ProgressController(
        ProgressConfig(repeat_guard_mode="enforce", repeat_limit=10), saved
    )
    assert restored.restored
    assert restored.repeat_guard.check(call_identity("run_command", {"argv": ["true"]})).denied
    other_target = ProgressController(restored.config, saved, scope="new-target")
    assert not other_target.restored
    assert not other_target.repeat_guard.records


def test_v1_migration_keeps_generic_counters_and_discards_old_fingerprints():
    restored = ProgressController(
        ProgressConfig(),
        {
            "version": 1,
            "epoch": 2,
            "no_progress_steps": 4,
            "recovery_attempts": 1,
            "recent_calls": ["volatile-pid"],
        },
    )
    assert restored.restored
    assert restored.no_progress_steps == 4
    assert restored.epoch == 2
    assert restored.snapshot()["recent_calls"] == []
    assert restored.migration.startswith("v1_to_v2")


def test_compaction_grants_one_redelivery_without_resetting_limits():
    controller = enforced()
    key = call_identity("read_file", {"path": "a"})
    for position in (2, 4, 6):
        observe(controller, "read_file", {"path": "a"}, result_position=position)
    assert controller.repeat_guard.check(key, cursor=5, allow_redelivery=True).denied
    controller.finish_step()
    assert controller.repeat_guard.check(key, cursor=6, allow_redelivery=True) is None
    assert (
        observe(controller, "read_file", {"path": "a"}, result_position=8).progress
        == ProgressKind.WEAK
    )
    saved = controller.snapshot()
    restored = ProgressController(controller.config, saved)
    assert restored.repeat_guard.check(key, cursor=100, allow_redelivery=True).denied


def test_truncated_results_do_not_enable_execution_rejection():
    controller = enforced()
    for _ in range(6):
        controller.observe_tool(
            tool_name="run_command",
            arguments={"argv": ["true"]},
            success=True,
            result_content="prefix",
            metadata={"truncated": True},
            progress_signal=ProgressSignal(kind=ProgressKind.WEAK, evidence_key="prefix"),
            read_only=False,
            idempotent=False,
            repeat_eligible=True,
        )
        assert controller.finish_step().action != TerminationAction.FINALIZE
    assert controller.repeat_guard.check(call_identity("run_command", {"argv": ["true"]})) is None


def test_host_configured_repeat_allowance_is_effective():
    controller = enforced(repeat_tool_limits={"run_command": 15})
    for _ in range(14):
        assert observe(controller).action != TerminationAction.FINALIZE
    key = call_identity("run_command", {"argv": ["true"]})
    assert controller.repeat_guard.check(key) is None
    assert observe(controller).action == TerminationAction.RECOVER
    assert controller.repeat_guard.check(key).denied


def test_observe_mode_reports_would_block_without_refusing():
    controller = ProgressController(ProgressConfig())
    for _ in range(3):
        observe(controller)
    check = controller.repeat_guard.check(call_identity("run_command", {"argv": ["true"]}))
    assert check is not None and not check.denied


def test_async_completion_is_attributed_to_the_original_launch():
    controller = enforced()
    for n in range(3):
        controller.observe_tool(
            tool_name="run_command",
            arguments={"argv": ["slow"], "wait_seconds": n},
            success=True,
            result_content="running",
            metadata={"process_id": f"pid-{n}"},
            progress_signal=ProgressSignal(
                kind=ProgressKind.WAITING, evidence_key=f"process:pid-{n}"
            ),
            read_only=False,
            idempotent=False,
            repeat_eligible=True,
        )
        assert controller.finish_step().progress == ProgressKind.WAITING
        controller.observe_tool(
            tool_name="poll_process",
            arguments={"process_id": f"pid-{n}"},
            success=True,
            result_content="done",
            metadata={"process_id": f"pid-{n}"},
            progress_signal=ProgressSignal(
                kind=ProgressKind.WEAK,
                evidence_key="same-complete-output",
                evidence_complete=True,
                subject_key=f"process:pid-{n}",
            ),
            read_only=True,
            idempotent=False,
            repeat_eligible=True,
        )
        controller.finish_step()
    assert controller.repeat_guard.check(call_identity("run_command", {"argv": ["slow"]})).denied


def test_repetition_caches_are_bounded_and_active_limits_are_never_evicted():
    controller = enforced(repeat_capacity=16)
    for n in range(16):
        for _ in range(3):
            observe(controller, arguments={"argv": [f"cmd-{n}"]})
    assert len(controller.repeat_guard.records) == 16
    assert (
        observe(controller, arguments={"argv": ["overflow"]}).reason_code
        == "repeat_capacity_exhausted"
    )
    assert len(controller.repeat_guard.records) == 16
    assert controller.repeat_guard.check(call_identity("run_command", {"argv": ["cmd-0"]})).denied

    controller = enforced(repeat_capacity=16)
    for n in range(30):
        controller.observe_tool(
            tool_name="read_file",
            arguments={"path": str(n)},
            success=True,
            result_content=str(n),
            metadata={},
            read_only=True,
            idempotent=True,
        )
        controller.finish_step()
    saved = controller.snapshot()
    assert len(saved["idempotent_results"]) <= 16
    assert len(saved["repeat_guard"]["records"]) <= 16
