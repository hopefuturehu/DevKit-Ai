from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.core.events import AgentEvent, EventType
from bot.core.models import RunResult
from bot.evals import EvalCase, load_eval_cases, run_eval_case, write_eval_results


def test_load_eval_cases_rejects_duplicate_ids(tmp_path: Path) -> None:
    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text(
        '{"id":"same","prompt":"one"}\n{"id":"same","prompt":"two"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="id 重复"):
        load_eval_cases(cases_path)


@pytest.mark.asyncio
async def test_eval_case_checks_artifacts_trace_and_skill_control(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "result.txt").write_text("verified output", encoding="utf-8")
    captured_requests = []
    reset_calls = []

    class FakeRunner:
        def __init__(self, sink) -> None:
            self.sink = sink

        async def run(self, request):
            captured_requests.append(request)
            await self.sink.publish(
                AgentEvent(
                    type=EventType.TOOL_REQUESTED,
                    session_id="session",
                    run_id="run",
                    payload={"name": "read_file"},
                )
            )
            await self.sink.publish(
                AgentEvent(
                    type=EventType.SKILL_ACTIVATED,
                    session_id="session",
                    run_id="run",
                    payload={"name": "analysis"},
                )
            )
            return RunResult(
                session_id="session",
                status="completed",
                final_text="done safely",
                steps=2,
                input_tokens=12,
                output_tokens=4,
            )

    def runtime_builder(**kwargs):
        return SimpleNamespace(
            runner=FakeRunner(kwargs["event_sinks"][0]),
            catalog=SimpleNamespace(skills={"analysis": object()}),
            skills=SimpleNamespace(reset=lambda: reset_calls.append(True)),
            close=lambda: None,
        )

    case = EvalCase(
        id="trace",
        prompt="check",
        workspace="workspace",
        explicit_skills=["analysis"],
        final_contains=["safely"],
        files_contain={"result.txt": ["verified"]},
        expected_tools=["read_file"],
        expected_skills=["analysis"],
        max_tool_calls=1,
        max_approval_requests=0,
    )

    result = await run_eval_case(
        case,
        base=tmp_path,
        config_path=None,
        runtime_builder=runtime_builder,
    )

    assert result.passed
    assert result.tool_names == ["read_file"]
    assert result.activated_skills == ["analysis"]
    assert captured_requests[0].explicit_skills == ["analysis"]

    baseline = await run_eval_case(
        case,
        base=tmp_path,
        config_path=None,
        disable_skills=True,
        runtime_builder=runtime_builder,
    )

    assert baseline.passed
    assert captured_requests[1].explicit_skills == []
    assert reset_calls == [True]

    output = tmp_path / "results" / "eval.jsonl"
    write_eval_results(output, [result, baseline])
    assert len(output.read_text(encoding="utf-8").splitlines()) == 2
