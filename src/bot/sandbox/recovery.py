"""Protected native recovery journal; event/SQLite payloads are not authority.

Only the trusted admission registry stores these records. No argv, environment,
or output is needed for cleanup authority. A pre-spawn record without a durable
root identity remains unresolved after host death: guessing a PID is forbidden.
"""

from __future__ import annotations

import math
import re
from copy import deepcopy
from dataclasses import asdict

from bot.execution.base import ProcessSpec
from bot.sandbox.models import SandboxError, SandboxErrorCode

_METADATA = (
    "backend",
    "backend_version",
    "node_version",
    "scope",
    "control_directory",
    "run_directory",
    "read_only",
    "denied_paths",
    "purpose",
    "admission_lease",
    "capabilities",
)


def prepared(target, spec, launch, baseline, owner: dict) -> dict:
    if baseline is None:
        raise ValueError("恢复日志缺少启动前进程基线")
    return {
        "v": 1,
        "phase": "prepared",
        "cleanup_scope": target.cleanup_scope,
        "state_root": target._state_identity.record(),
        "creator": deepcopy(owner),
        "spec": ProcessSpec(
            argv=["[recovered native call; original arguments unavailable]"],
            cwd=spec.cwd,
            session_id=spec.session_id,
            run_id=spec.run_id,
            scope=spec.scope,
        ).model_dump(mode="json"),
        "metadata": {key: deepcopy(launch.metadata[key]) for key in _METADATA},
        "guard": {
            "baseline_births": {str(pid): p.start_time for pid, p in baseline.processes.items()},
            "baseline_inaccessible": sorted(baseline.inaccessible),
        },
        "process_id": None,
        "root": None,
    }


def registered(journal: dict, managed) -> dict:
    value = deepcopy(journal)
    root = managed.scope.known.get(managed.scope.root_pid)
    value.update(
        phase="registered" if root is not None else "unidentified",
        process_id=managed.process_id,
        root=asdict(root) if root is not None else None,
    )
    return value


def _pid(value) -> bool:
    return type(value) is int and value > 0


def _birth(value) -> bool:
    return type(value) in (float, int) and math.isfinite(value) and value > 0


def cleanup_record(target, journal: dict, reference: dict) -> dict:
    """Validate the protected record before Local sees any signal authority."""
    try:
        if (
            not isinstance(journal, dict)
            or journal.keys()
            != {
                "v",
                "phase",
                "cleanup_scope",
                "state_root",
                "creator",
                "spec",
                "metadata",
                "guard",
                "process_id",
                "root",
            }
            or type(journal["v"]) is not int
            or journal["v"] != 1
            or journal["cleanup_scope"] != target.cleanup_scope
            or journal["state_root"] != target._state_identity.record()
        ):
            raise ValueError("恢复日志版本、宿主目录或运行时身份不一致")
        if journal["phase"] != "registered":
            raise ValueError("崩溃发生在启动身份持久化窗口，保留占用，不能猜测或接管 PID")
        if (
            not isinstance(journal["process_id"], str)
            or re.fullmatch(r"proc_[0-9a-f]{32}", journal["process_id"]) is None
        ):
            raise ValueError("恢复调用标识无效")
        creator, root, guard = journal["creator"], journal["root"], journal["guard"]
        if (
            creator.keys() != {"pid", "started", "claim"}
            or not _pid(creator["pid"])
            or not _birth(creator["started"])
            or not isinstance(creator["claim"], str)
            or re.fullmatch(r"[0-9a-f]{32}", creator["claim"]) is None
            or root.keys() != {"pid", "start_time", "ppid", "sid"}
            or not _pid(root["pid"])
            or not _birth(root["start_time"])
            or root["ppid"] != creator["pid"]
            or root["sid"] != root["pid"]
        ):
            raise ValueError("恢复进程缺少完整创建身份")
        if guard.keys() != {"baseline_births", "baseline_inaccessible"}:
            raise ValueError("恢复基线字段无效")
        births, inaccessible = guard["baseline_births"], guard["baseline_inaccessible"]
        if (
            not isinstance(births, dict)
            or not isinstance(inaccessible, list)
            or any(
                not isinstance(pid, str)
                or re.fullmatch(r"0|[1-9][0-9]*", pid) is None
                or not _birth(born)
                for pid, born in births.items()
            )
            or any(type(pid) is not int or pid < 0 for pid in inaccessible)
            or births.get(str(root["pid"])) == root["start_time"]
        ):
            raise ValueError("恢复基线不完整或根进程已存在于启动前")
        spec = ProcessSpec.model_validate(journal["spec"])
        metadata = journal["metadata"]
        if (
            metadata.keys() != set(_METADATA)
            or metadata["admission_lease"] != reference
            or spec.scope is None
            or spec.scope.workspace != target.workspace
            or not spec.cwd.is_relative_to(target.workspace)
            or spec.scope.model_dump(mode="json") != metadata["scope"]
        ):
            raise ValueError("恢复策略、工作区或准入记录不一致")
        # The remaining resource identities and session/run binding are checked
        # again by Native._restore_launch before a managed scope is installed.
        return {
            "cleanup_scope": journal["cleanup_scope"],
            "process_id": journal["process_id"],
            "scope_root_pid": root["pid"],
            "known_processes": [deepcopy(root)],
            "process_guard": deepcopy(guard),
            "cleanup_status": "unknown",
            "spec": spec.model_dump(mode="json"),
            "launch_metadata": deepcopy(metadata)
            | {
                "executed": None,
                "launch_status": {"state": "recovered", "stage": "recovery"},
            },
            "recovery_journal": deepcopy(journal),
        }
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise SandboxError(
            SandboxErrorCode.CLEANUP_FAILED, str(exc), stage="recovery", executed=False
        ) from exc
