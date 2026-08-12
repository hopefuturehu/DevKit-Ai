from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from bot.config.models import ProgressConfig


class ProgressKind(StrEnum):
    NONE = "none"
    WEAK = "weak"
    STRONG = "strong"


class TerminationAction(StrEnum):
    CONTINUE = "continue"
    WARN = "warn"
    RECOVER = "recover"
    FINALIZE = "finalize"


@dataclass(frozen=True, slots=True)
class ProgressReport:
    action: TerminationAction
    progress: ProgressKind
    reason_code: str
    message: str
    epoch: int
    no_progress_steps: int
    recovery_attempt: int = 0
    pattern: dict[str, Any] | None = None

    def event_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": self.action.value,
            "progress": self.progress.value,
            "reason_code": self.reason_code,
            "message": self.message,
            "epoch": self.epoch,
            "no_progress_steps": self.no_progress_steps,
            "recovery_attempt": self.recovery_attempt,
        }
        if self.pattern:
            payload["pattern"] = self.pattern
        return payload


@dataclass(frozen=True, slots=True)
class _Observation:
    tool_name: str
    call_signature: str
    result_signature: str
    success: bool
    progress: ProgressKind
    exact_failure_count: int = 0
    same_tool_failure_count: int = 0
    idempotent_repeat_count: int = 0


class ProgressController:
    """Detect task stalls without imposing a fixed global step budget.

    Progress is evaluated once per model/tool turn.  Deterministic repetition
    signals can escalate sooner than the generic no-progress counter.  A
    successful recovery must be followed by real progress; recurrence then
    moves the run to a one-shot finalization phase.
    """

    def __init__(self, config: ProgressConfig) -> None:
        self.config = config
        self.epoch = 0
        self.no_progress_steps = 0
        self.recovery_attempts = 0
        self._warning_emitted = False
        self._step_observations: list[_Observation] = []
        self._recent_calls: deque[str] = deque(maxlen=config.cycle_window_size)
        self._exact_failures: dict[str, tuple[str, int]] = {}
        self._same_tool_failure_counts: dict[str, int] = {}
        self._idempotent_results: dict[str, tuple[str, int]] = {}

    def observe_tool(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        success: bool,
        result_content: str,
        metadata: dict[str, Any] | None,
        read_only: bool,
        idempotent: bool,
    ) -> None:
        call_signature = self._fingerprint(tool_name, arguments)
        result_signature = self._fingerprint(result_content)
        progress = self._classify_progress(
            success=success,
            metadata=metadata or {},
            read_only=read_only,
            idempotent=idempotent,
            call_signature=call_signature,
            result_signature=result_signature,
        )

        exact_failure_count = 0
        same_tool_failure_count = 0
        if success:
            self._same_tool_failure_counts.pop(tool_name, None)
            self._exact_failures.pop(call_signature, None)
        else:
            previous_result, previous_count = self._exact_failures.get(
                call_signature,
                ("", 0),
            )
            exact_failure_count = previous_count + 1 if previous_result == result_signature else 1
            self._exact_failures[call_signature] = (result_signature, exact_failure_count)
            same_tool_failure_count = self._same_tool_failure_counts.get(tool_name, 0) + 1
            self._same_tool_failure_counts[tool_name] = same_tool_failure_count

        idempotent_repeat_count = 0
        if success and idempotent:
            previous_result, previous_count = self._idempotent_results.get(
                call_signature,
                ("", 0),
            )
            idempotent_repeat_count = (
                previous_count + 1 if previous_result == result_signature else 1
            )
            self._idempotent_results[call_signature] = (
                result_signature,
                idempotent_repeat_count,
            )

        self._recent_calls.append(self._fingerprint(call_signature, result_signature))
        self._step_observations.append(
            _Observation(
                tool_name=tool_name,
                call_signature=call_signature,
                result_signature=result_signature,
                success=success,
                progress=progress,
                exact_failure_count=exact_failure_count,
                same_tool_failure_count=same_tool_failure_count,
                idempotent_repeat_count=idempotent_repeat_count,
            )
        )

    def finish_step(self) -> ProgressReport:
        observations = self._step_observations
        self._step_observations = []
        progress = self._step_progress(observations)

        if progress == ProgressKind.STRONG:
            self._record_strong_progress()
            return self._report(
                TerminationAction.CONTINUE,
                progress,
                "strong_progress",
                "检测到可验证的状态变化，已开启新的进展阶段。",
            )
        if progress == ProgressKind.WEAK:
            self.no_progress_steps = max(0, self.no_progress_steps - 1)
        else:
            self.no_progress_steps += 1

        signal = self._strongest_signal(observations)
        cycle = self._detect_cycle()
        if cycle and cycle[1] >= self.config.cycles_before_recovery:
            signal = (
                "tool_cycle",
                f"检测到周期为 {cycle[0]} 的工具调用循环，已重复 {cycle[1]} 次。",
                {"period": cycle[0], "repetitions": cycle[1]},
                True,
            )
        elif cycle and cycle[1] >= self.config.cycles_before_warning and signal is None:
            signal = (
                "tool_cycle",
                f"检测到周期为 {cycle[0]} 的工具调用重复模式。",
                {"period": cycle[0], "repetitions": cycle[1]},
                False,
            )

        if self.no_progress_steps >= self.config.finalize_after_no_progress_steps:
            return self._report(
                TerminationAction.FINALIZE,
                progress,
                "no_progress_after_recovery",
                "恢复后仍未产生可验证进展，进入单次收尾。",
                pattern={"no_progress_steps": self.no_progress_steps},
            )

        severe = bool(signal and signal[3]) or (
            self.no_progress_steps >= self.config.recovery_after_no_progress_steps
        )
        if severe:
            reason_code, message, pattern, _ = signal or (
                "no_progress",
                f"连续 {self.no_progress_steps} 个步骤没有可验证进展。",
                {"no_progress_steps": self.no_progress_steps},
                True,
            )
            if self.recovery_attempts < self.config.max_recovery_attempts_per_epoch:
                self.recovery_attempts += 1
                self._warning_emitted = False
                self._clear_repetition_evidence()
                return self._report(
                    TerminationAction.RECOVER,
                    progress,
                    reason_code,
                    message,
                    pattern=pattern,
                )
            return self._report(
                TerminationAction.FINALIZE,
                progress,
                f"{reason_code}_after_recovery",
                f"{message} 恢复尝试未打破该模式，进入单次收尾。",
                pattern=pattern,
            )

        should_warn = bool(signal) or (
            self.no_progress_steps >= self.config.warning_after_no_progress_steps
        )
        if should_warn and not self._warning_emitted:
            self._warning_emitted = True
            reason_code, message, pattern, _ = signal or (
                "no_progress",
                f"连续 {self.no_progress_steps} 个步骤没有可验证进展。",
                {"no_progress_steps": self.no_progress_steps},
                False,
            )
            return self._report(
                TerminationAction.WARN,
                progress,
                reason_code,
                message,
                pattern=pattern,
            )

        return self._report(
            TerminationAction.CONTINUE,
            progress,
            "weak_progress" if progress == ProgressKind.WEAK else "no_progress",
            "当前步骤只有探索性进展。"
            if progress == ProgressKind.WEAK
            else "当前步骤没有可验证进展。",
        )

    def warning_guidance(self, report: ProgressReport) -> str:
        return (
            "进展监控警告："
            f"{report.message} 请重新核对任务目标和当前证据；不要原样重复相同调用，"
            "下一步应获取新信息、改变参数或执行可验证的状态变更。"
        )

    def recovery_guidance(self, report: ProgressReport) -> str:
        return (
            f"进入恢复阶段（第 {report.recovery_attempt} 次）：{report.message} "
            "暂停当前路径，列出已知事实与失败假设，选择一种不同的工具、参数或实现路径。"
            "只有出现新的外部证据或状态变化，才恢复原方案。"
        )

    def _classify_progress(
        self,
        *,
        success: bool,
        metadata: dict[str, Any],
        read_only: bool,
        idempotent: bool,
        call_signature: str,
        result_signature: str,
    ) -> ProgressKind:
        if not success:
            return ProgressKind.NONE
        marker = metadata.get("progress")
        if isinstance(marker, dict):
            kind = marker.get("kind")
            if kind in {ProgressKind.STRONG.value, ProgressKind.WEAK.value}:
                return ProgressKind(kind)
            if marker.get("changed") is True:
                return ProgressKind.STRONG
            if marker.get("changed") is False:
                return ProgressKind.NONE
        if idempotent:
            previous = self._idempotent_results.get(call_signature)
            if previous and previous[0] == result_signature:
                return ProgressKind.NONE
            return ProgressKind.WEAK
        return ProgressKind.WEAK if read_only else ProgressKind.STRONG

    def _step_progress(self, observations: list[_Observation]) -> ProgressKind:
        if any(item.progress == ProgressKind.STRONG for item in observations):
            return ProgressKind.STRONG
        if any(item.progress == ProgressKind.WEAK for item in observations):
            return ProgressKind.WEAK
        return ProgressKind.NONE

    def _strongest_signal(
        self,
        observations: list[_Observation],
    ) -> tuple[str, str, dict[str, Any], bool] | None:
        for item in observations:
            if item.exact_failure_count >= self.config.exact_failure_recovery:
                return (
                    "exact_failure_repeat",
                    f"相同工具调用与失败结果已重复 {item.exact_failure_count} 次。",
                    {"tool": item.tool_name, "count": item.exact_failure_count},
                    True,
                )
            if item.idempotent_repeat_count >= self.config.idempotent_repeat_recovery:
                return (
                    "idempotent_result_repeat",
                    f"幂等工具的相同结果已重复 {item.idempotent_repeat_count} 次。",
                    {"tool": item.tool_name, "count": item.idempotent_repeat_count},
                    True,
                )
            if item.same_tool_failure_count >= self.config.same_tool_failure_recovery:
                return (
                    "same_tool_failure_repeat",
                    f"工具 {item.tool_name} 已连续失败 {item.same_tool_failure_count} 次。",
                    {"tool": item.tool_name, "count": item.same_tool_failure_count},
                    True,
                )
        for item in observations:
            if item.exact_failure_count >= self.config.exact_failure_warning:
                return (
                    "exact_failure_repeat",
                    f"相同工具调用与失败结果已重复 {item.exact_failure_count} 次。",
                    {"tool": item.tool_name, "count": item.exact_failure_count},
                    False,
                )
            if item.idempotent_repeat_count >= self.config.idempotent_repeat_warning:
                return (
                    "idempotent_result_repeat",
                    f"幂等工具的相同结果已重复 {item.idempotent_repeat_count} 次。",
                    {"tool": item.tool_name, "count": item.idempotent_repeat_count},
                    False,
                )
            if item.same_tool_failure_count >= self.config.same_tool_failure_warning:
                return (
                    "same_tool_failure_repeat",
                    f"工具 {item.tool_name} 已连续失败 {item.same_tool_failure_count} 次。",
                    {"tool": item.tool_name, "count": item.same_tool_failure_count},
                    False,
                )
        return None

    def _detect_cycle(self) -> tuple[int, int] | None:
        values = list(self._recent_calls)
        best: tuple[int, int] | None = None
        for period in range(1, min(self.config.max_cycle_period, len(values) // 2) + 1):
            block = values[-period:]
            repetitions = 1
            cursor = len(values) - period * 2
            while cursor >= 0 and values[cursor : cursor + period] == block:
                repetitions += 1
                cursor -= period
            if repetitions >= 2 and (best is None or repetitions > best[1]):
                best = (period, repetitions)
        return best

    def _record_strong_progress(self) -> None:
        self.epoch += 1
        self.no_progress_steps = 0
        self.recovery_attempts = 0
        self._warning_emitted = False
        self._clear_repetition_evidence()

    def _clear_repetition_evidence(self) -> None:
        self._recent_calls.clear()
        self._exact_failures.clear()
        self._same_tool_failure_counts.clear()
        self._idempotent_results.clear()

    def _report(
        self,
        action: TerminationAction,
        progress: ProgressKind,
        reason_code: str,
        message: str,
        *,
        pattern: dict[str, Any] | None = None,
    ) -> ProgressReport:
        return ProgressReport(
            action=action,
            progress=progress,
            reason_code=reason_code,
            message=message,
            epoch=self.epoch,
            no_progress_steps=self.no_progress_steps,
            recovery_attempt=self.recovery_attempts,
            pattern=pattern,
        )

    @staticmethod
    def _fingerprint(*values: Any) -> str:
        payload = json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
