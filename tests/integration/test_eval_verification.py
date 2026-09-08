import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bot.cli.app import app
from bot.core.models import ModelCapabilities, ModelEvent, ModelEventKind, Role
from bot.evals.isolation import Entry
from bot.evals.models import CommandVerifier
from bot.evals.verification import command_check, resolve_image
from bot.providers import ModelProvider


class EvalProvider(ModelProvider):
    requests = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def capabilities(self, model):
        return ModelCapabilities()

    async def stream(self, request):
        self.requests.append(request)
        if any(message.role == Role.TOOL for message in request.messages):
            yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="verified")
        else:
            yield ModelEvent(
                kind=ModelEventKind.TOOL_CALL_DELTA,
                tool_index=0,
                tool_call_id="read-input",
                tool_name="read_file",
                arguments_delta='{"path":"data.json"}',
            )
            yield ModelEvent(
                kind=ModelEventKind.TOOL_CALL_DELTA,
                tool_index=1,
                tool_call_id="command",
                tool_name="run_command",
                arguments_delta='{"argv":["pwd"]}',
            )
        yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")


def test_eval_cli_uses_real_runtime_with_isolated_config_and_execution_evidence(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("bot.cli.runtime.OpenAICompatibleProvider", EvalProvider)
    EvalProvider.requests.clear()
    (tmp_path / ".bot").mkdir()
    (tmp_path / ".bot/config.toml").write_text("""
[model]
base_url = "https://provider.example/v1"
api_key_ref = "dotenv:BOT_MODEL_API_KEY"
name = "mock-model"
[storage]
state_path = "./original-state.db"
[subagents]
enabled = false
""")
    (tmp_path / ".env").write_text("BOT_MODEL_API_KEY=isolated-test-credential\n")
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "data.json").write_text('{"score": 7}')
    cases = tmp_path / "cases.jsonl"
    cases.write_text(
        json.dumps(
            {
                "id": "real-runtime",
                "prompt": "Read data.json and verify it.",
                "workspace": "fixture",
                "expected_tools": ["read_file", "run_command"],
                "forbidden_changes": ["**"],
                "verifiers": [
                    {
                        "kind": "json",
                        "path": "data.json",
                        "fresh": False,
                        "schema": {
                            "type": "object",
                            "required": ["score"],
                            "properties": {"score": {"const": 7}},
                        },
                    }
                ],
            }
        )
        + "\n"
    )
    output = tmp_path / "results.jsonl"
    result = CliRunner().invoke(
        app, ["-C", str(tmp_path), "eval", "run", str(cases), "-o", str(output)]
    )
    assert result.exit_code == 0, result.output
    row = json.loads(output.read_text())
    assert row["passed"] and row["verdict"] == "pass"
    assert row["run_status"] == "completed"
    assert row["tool_names"] == ["read_file", "run_command"]
    assert row["manifest"]["config"]["model"]["name"] == "mock-model"
    assert not (tmp_path / "original-state.db").exists()
    assert not (fixture / ".bot").exists()
    assert "isolated-test-credential" not in output.read_text()
    events = [
        json.loads(line)
        for line in (Path(row["artifact_dir"]) / "events.jsonl").read_text().splitlines()
    ]
    assert any(
        item["type"] == "tool.result"
        and item["payload"]["name"] == "run_command"
        and item["payload"]["status"] == "completed"
        and item["payload"]["returncode"] == 0
        for item in events
    )


def test_eval_cli_continues_after_invalid_case_and_exits_nonzero(tmp_path):
    cases = tmp_path / "cases.jsonl"
    cases.write_text(
        '{"id":"missing","prompt":"x","workspace":"absent","final_contains":["x"]}\n'
        '{"id":"ungraded","prompt":"x"}\n'
    )
    output = tmp_path / "result.jsonl"
    result = CliRunner().invoke(
        app, ["-C", str(tmp_path), "eval", "run", str(cases), "-o", str(output)]
    )
    assert result.exit_code == 1, result.output
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(rows) == 2
    assert all(row["verdict"] == "error" and not row["passed"] for row in rows)


docker_only = pytest.mark.skipif(
    os.environ.get("RUN_EVAL_DOCKER_TESTS") != "1", reason="需要显式启用本地 Docker 验收测试"
)


@docker_only
@pytest.mark.asyncio
async def test_command_verifier_grades_clean_copies_and_cannot_modify_tests(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_MODEL_API_KEY", "must-not-enter-grader")
    image = await resolve_image("alpine:3.20")
    script = b"""#!/bin/sh
set -eu
test "$(cat /workspace/answer.txt)" = "42"
test ! -e /workspace/.env
test -z "${BOT_MODEL_API_KEY+x}"
if echo tampered >> /verifier/check.sh 2>/dev/null; then exit 1; fi
echo changed > /workspace/answer.txt
printf '{"passed":true}' > "$BOT_EVAL_RESULT"
"""
    tests = {"check.sh": Entry("file", 0o644, script)}
    files = {"answer.txt": Entry("file", 0o644, b"42")}
    spec = CommandVerifier(
        tests="private-tests", image="alpine:3.20", argv=["/bin/sh", "/verifier/check.sh"]
    )
    for index in range(2):
        result = await command_check(
            spec, image=image, tests=tests, artifacts=files, root=tmp_path / str(index)
        )
        assert result.verdict == "pass", result
    assert files["answer.txt"].content == b"42"
    assert (tmp_path / "0/tests/check.sh").read_bytes() == script


@docker_only
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "script,verdict",
    [
        (
            'printf \'{"passed":false,"message":"wrong output"}\' > "$BOT_EVAL_RESULT"; exit 1',
            "fail",
        ),
        ("exit 0", "error"),
        ('printf \'{"passed":true}\' > "$BOT_EVAL_RESULT"; exit 1', "error"),
        ('printf \'{"passed":"true"}\' > "$BOT_EVAL_RESULT"', "error"),
        ('ln -s /etc/passwd "$BOT_EVAL_RESULT"', "error"),
    ],
)
async def test_command_verifier_distinguishes_failed_missing_and_invalid_results(
    tmp_path, script, verdict
):
    image = await resolve_image("alpine:3.20")
    spec = CommandVerifier(
        tests="tests", image="alpine:3.20", argv=["/bin/sh", "/verifier/check.sh"]
    )
    result = await command_check(
        spec,
        image=image,
        tests={"check.sh": Entry("file", 0o644, script.encode())},
        artifacts={},
        root=tmp_path / "grader",
    )
    assert result.verdict == verdict, result


@docker_only
@pytest.mark.asyncio
async def test_command_timeout_removes_container(tmp_path):
    image = await resolve_image("alpine:3.20")
    spec = CommandVerifier(
        tests="tests",
        image="alpine:3.20",
        argv=["/bin/sh", "/verifier/check.sh"],
        timeout_seconds=0.1,
    )
    result = await command_check(
        spec,
        image=image,
        tests={"check.sh": Entry("file", 0o644, b"sleep 30")},
        artifacts={},
        root=tmp_path / "grader",
    )
    assert result.verdict == "error" and "TimeoutError" in result.message
    assert "清理失败" not in result.message
