import os
from copy import deepcopy
from types import SimpleNamespace

import pytest

from bot.execution.base import ProcessSpec
from bot.execution.process_scope import Inventory, ProcessIdentity
from bot.sandbox.models import ExecutionScope, SandboxError
from bot.sandbox.recovery import cleanup_record, prepared, registered
from bot.sandbox.resources import DirectoryIdentity


@pytest.fixture
def journal(tmp_path):
    work = tmp_path.resolve() / "work"
    work.mkdir()
    state = tmp_path.resolve() / "state"
    state.mkdir(mode=0o700)
    target = SimpleNamespace(
        workspace=work,
        cleanup_scope="bound-runtime-and-boot",
        _state_identity=DirectoryIdentity.capture(state),
    )
    scope = ExecutionScope(
        session_id="session", run_id="run", tool_call_id="call", workspace=work, policy_id="policy"
    )
    reference = {"registry": {"path": str(state)}, "id": "1" * 32, "binding_sha256": "2" * 64}
    launch = SimpleNamespace(
        metadata={
            "backend": "srt",
            "backend_version": "test",
            "node_version": "test",
            "scope": scope.model_dump(mode="json"),
            "control_directory": {},
            "run_directory": {},
            "read_only": False,
            "denied_paths": [],
            "purpose": "command",
            "admission_lease": reference,
            "capabilities": {},
            "launcher_argv": ["must-not-persist"],
        }
    )
    spec = ProcessSpec(
        argv=["secret-command", "private-argument"],
        env={"LANG": "private-env"},
        scope=scope,
        session_id="session",
        run_id="run",
        cwd=work,
    )
    owner = {"pid": os.getpid(), "started": 100.0, "claim": "3" * 32}
    value = prepared(
        target,
        spec,
        launch,
        Inventory({0: ProcessIdentity(0, 1.0, 0, 0), 1: ProcessIdentity(1, 1.0, 0, 1)}, {0}),
        owner,
    )
    root = ProcessIdentity(500000, 200.0, os.getpid(), 500000)
    managed = SimpleNamespace(
        process_id="proc_" + "4" * 32,
        scope=SimpleNamespace(root_pid=root.pid, known={root.pid: root}),
    )
    return target, value, managed, reference


def test_prepared_window_is_not_pid_authority_and_journal_excludes_arguments(journal):
    target, value, _, reference = journal
    assert value["phase"] == "prepared" and value["root"] is None
    assert "secret-command" not in str(value) and "private-argument" not in str(value)
    assert "private-env" not in str(value) and "must-not-persist" not in str(value)
    with pytest.raises(SandboxError, match="持久化窗口"):
        cleanup_record(target, value, reference)


def test_registered_identity_builds_cleanup_record_without_reexecuting_command(journal):
    target, value, managed, reference = journal
    active = registered(value, managed)
    record = cleanup_record(target, active, reference)
    assert value["phase"] == "prepared"
    assert record["scope_root_pid"] == managed.scope.root_pid
    assert record["known_processes"] == [active["root"]]
    assert record["cleanup_status"] == "unknown"
    assert record["launch_metadata"]["executed"] is None
    assert record["spec"]["env"] == {} and "recovered" in record["spec"]["argv"][0]
    active["guard"]["baseline_births"]["1"] = 999
    assert record["process_guard"]["baseline_births"]["1"] == 1.0


@pytest.mark.parametrize(
    "change",
    [
        "version",
        "phase",
        "state",
        "runtime",
        "id",
        "parent",
        "birth",
        "baseline",
        "missing_guard",
        "lease",
        "workspace",
    ],
)
def test_invalid_protected_journal_never_becomes_cleanup_authority(journal, change):
    target, value, managed, reference = journal
    value = registered(value, managed)
    if change == "version":
        value["v"] = True
    elif change == "phase":
        value["phase"] = "unidentified"
    elif change == "state":
        value["state_root"]["inode"] += 1
    elif change == "runtime":
        value["cleanup_scope"] = "another-runtime"
    elif change == "id":
        value["process_id"] = "arbitrary"
    elif change == "parent":
        value["root"]["ppid"] += 1
    elif change == "birth":
        value["root"]["start_time"] = float("nan")
    elif change == "baseline":
        value["guard"]["baseline_births"][str(value["root"]["pid"])] = value["root"]["start_time"]
    elif change == "missing_guard":
        value["guard"] = None
    elif change == "lease":
        value["metadata"]["admission_lease"] = {}
    elif change == "workspace":
        value["spec"]["scope"]["workspace"] = "/outside"
    with pytest.raises(SandboxError) as failure:
        cleanup_record(target, value, deepcopy(reference))
    assert failure.value.stage == "recovery" and failure.value.executed is False
