import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from bot.core.events import AgentEvent, EventType
from bot.core.models import RunResult
from bot.evals import EvalCase, load_eval_cases, run_eval_case, write_eval_results
from bot.evals.isolation import Entry
from bot.evals.models import CheckResult, CommandVerifier
from bot.evals.verification import command_check, docker_env
from bot.providers import ProviderError


def event(kind, *, session="root", run="run", **payload):
    return AgentEvent(type=kind, session_id=session, run_id=run, payload=payload)


def tool_events(
    *, status="completed", success=True, returncode=0, call_id="call", name="run_command", **extra
):
    return [
        event(
            EventType.TOOL_REQUESTED,
            tool_call_id=call_id,
            name=name,
            arguments=extra.pop("arguments", {}),
        ),
        event(
            EventType.TOOL_RESULT,
            tool_call_id=call_id,
            name=name,
            status=status,
            success=success,
            returncode=returncode,
            **extra,
        ),
    ]


class Harness:
    def __init__(self, root):
        self.root = root
        self.fixture = root / "fixture"
        self.fixture.mkdir()
        self.config = root / "eval.toml"
        self.config.write_text("")
        self.events = []
        self.calls = []
        self.requests = []
        self.closed = 0
        self.resets = 0
        self.action = lambda workspace, kwargs: None
        self.error = None
        self.close_error = None
        self.include_start = True
        self.run_result = RunResult(
            session_id="root",
            status="completed",
            final_text="done safely",
            steps=2,
            input_tokens=12,
            output_tokens=4,
        )

    def builder(self, **kwargs):
        self.calls.append(kwargs)
        harness = self

        class Runner:
            async def run(self, request):
                harness.requests.append(request)
                sink = kwargs["event_sinks"][0]
                if harness.include_start:
                    await sink.publish(event(EventType.RUN_STARTED))
                harness.action(kwargs["workspace"], kwargs)
                for item in harness.events:
                    await sink.publish(item)
                if harness.error:
                    raise harness.error
                return harness.run_result

        async def close():
            harness.closed += 1
            if harness.close_error:
                raise harness.close_error

        def reset():
            harness.resets += 1

        return SimpleNamespace(
            runner=Runner(),
            aclose=close,
            catalog=SimpleNamespace(skills={"analysis": object()}),
            skills=SimpleNamespace(reset=reset),
        )

    async def run(self, *, disable_skills=False, **fields):
        case = EvalCase(id="audit", prompt="complete task", workspace="fixture", **fields)
        return await run_eval_case(
            case,
            base=self.root,
            config_path=self.config,
            runtime_builder=self.builder,
            disable_skills=disable_skills,
            artifacts_dir=self.root / "results",
        )


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


def test_load_eval_cases_rejects_duplicate_ids(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text('{"id":"same","prompt":"one"}\n{"id":"same","prompt":"two"}\n')
    with pytest.raises(ValueError, match="重复"):
        load_eval_cases(path)


@pytest.mark.asyncio
async def test_correct_artifact_and_real_tool_evidence_pass_with_skill_ablation(harness):
    harness.action = lambda workspace, _: (workspace / "result.txt").write_text("verified output")
    harness.events = tool_events() + [event(EventType.SKILL_ACTIVATED, name="analysis")]
    fields = dict(
        files_contain={"result.txt": ["verified"]},
        expected_tools=["run_command"],
        expected_skills=["analysis"],
        explicit_skills=["analysis"],
        final_contains=["safely"],
    )
    normal = await harness.run(**fields)
    ablation = await harness.run(disable_skills=True, **fields)
    assert normal.passed, normal.failures
    assert ablation.passed, ablation.failures
    assert normal.verdict == "pass" and normal.failure_kind is None
    assert normal.attempt_id != ablation.attempt_id
    assert harness.requests[0].explicit_skills == ["analysis"]
    assert harness.requests[1].explicit_skills == []
    assert harness.resets == 1 and harness.closed == 2
    assert not (harness.fixture / "result.txt").exists()
    assert normal.manifest["fixture_sha256"] == ablation.manifest["fixture_sha256"]
    assert normal.manifest["config_sha256"] == ablation.manifest["config_sha256"]
    write_eval_results(harness.root / "summary.jsonl", [normal, ablation])
    assert len((harness.root / "summary.jsonl").read_text().splitlines()) == 2
    assert json.loads((Path(normal.artifact_dir) / "result.json").read_text())["passed"]


@pytest.mark.asyncio
async def test_old_artifact_and_requested_only_tool_cannot_pass(harness):
    (harness.fixture / "result.txt").write_text("verified output")
    harness.events = tool_events()[:1]
    result = await harness.run(
        files_contain={"result.txt": ["verified"]}, expected_tools=["run_command"]
    )
    assert not result.passed and result.verdict == "fail"
    assert {c.name for c in result.checks if c.verdict == "fail"} == {
        "file:result.txt",
        "tool_result:0:run_command",
    }


@pytest.mark.asyncio
async def test_existing_input_can_be_checked_only_with_explicit_nonfresh_opt_in(harness):
    (harness.fixture / "input.txt").write_text("source data")
    result = await harness.run(
        files_contain={"input.txt": ["source"]},
        files_contain_require_change=False,
        forbidden_changes=["**"],
    )
    assert result.passed, result.failures


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "events",
    [
        tool_events(status="failed", success=False, returncode=1),
        tool_events(status="running", returncode=None, process_id="p"),
        tool_events(returncode=None),
        tool_events(returncode=3),
        tool_events()[1:],
        [tool_events()[0], tool_events(call_id="wrong")[1]],
    ],
)
async def test_failed_running_missing_or_unpaired_tool_results_fail(harness, events):
    harness.events = events
    result = await harness.run(expected_tools=["run_command"])
    assert result.verdict == "fail", result


@pytest.mark.asyncio
async def test_managed_command_requires_matching_terminal_poll(harness):
    harness.events = tool_events(status="running", returncode=None, process_id="p")
    harness.events += tool_events(
        name="poll_process", call_id="poll", process_id="p", arguments={"process_id": "p"}
    )
    result = await harness.run(expected_tools=["run_command"])
    assert result.passed, result.failures
    harness.events[-2].payload["arguments"]["process_id"] = "other"
    result = await harness.run(expected_tools=["run_command"])
    assert not result.passed


@pytest.mark.asyncio
async def test_explicit_tool_failure_and_argument_contract(harness):
    harness.events = tool_events(
        success=False, status="failed", returncode=2, arguments={"argv": ["bad"]}
    )
    fields = {
        "tool_results": [
            {
                "name": "run_command",
                "success": False,
                "status": "failed",
                "returncode": 2,
                "arguments_contain": {"argv": ["bad"]},
            }
        ]
    }
    assert (await harness.run(**fields)).passed
    fields["tool_results"][0]["returncode"] = 3
    assert not (await harness.run(**fields)).passed


@pytest.mark.asyncio
async def test_child_events_cannot_satisfy_root_execution(harness):
    harness.events = tool_events()[:1] + [
        item.model_copy(update={"session_id": "child"}) for item in tool_events()
    ]
    assert not (await harness.run(expected_tools=["run_command"])).passed


@pytest.mark.asyncio
async def test_duplicate_call_ids_are_verifier_errors(harness):
    harness.events = tool_events() + tool_events()
    result = await harness.run(expected_tools=["run_command"])
    assert result.verdict == "error" and result.failure_kind == "verifier"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw,passed",
    [
        ('{"score": 9, "label": "good"}', True),
        ('{"score": -1, "label": "good"}', False),
        ('{"score": "9", "label": "good"}', False),
        ('{"score": NaN, "label": "good"}', False),
        ('{"label": "good"}', False),
        ("not json", False),
    ],
)
async def test_structured_verifier_rejects_keyword_correct_but_invalid_results(
    harness, raw, passed
):
    harness.action = lambda workspace, _: (workspace / "result.json").write_text(raw)
    result = await harness.run(
        verifiers=[
            {
                "kind": "json",
                "path": "result.json",
                "schema": {
                    "type": "object",
                    "required": ["score", "label"],
                    "properties": {
                        "score": {"type": "number", "minimum": 0, "maximum": 10},
                        "label": {"const": "good"},
                    },
                },
            }
        ]
    )
    assert result.passed is passed, result.failures


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema", [{"type": "nonexistent"}, {"$ref": "https://example.com/schema"}]
)
async def test_invalid_or_remote_schema_is_rejected_before_agent_run(harness, schema):
    result = await harness.run(
        verifiers=[{"kind": "json", "path": "result.json", "schema": schema}]
    )
    assert result.verdict == "error" and result.failure_kind == "verifier"
    assert not harness.calls


@pytest.mark.asyncio
async def test_no_verifier_and_missing_trace_do_not_pass(harness):
    result = await harness.run()
    assert result.verdict == "error" and not harness.calls
    harness.include_start = False
    result = await harness.run(final_contains=["done"])
    assert result.verdict == "error" and result.failure_kind == "verifier"


@pytest.mark.asyncio
async def test_forbidden_changes_include_creations_deletions_modes_and_symlinks(harness):
    (harness.fixture / "keep.txt").write_text("keep")
    (harness.fixture / "mode.txt").write_text("mode")

    def mutate(workspace, _):
        (workspace / "keep.txt").unlink()
        (workspace / "new.txt").write_text("new")
        (workspace / "mode.txt").chmod(0o700)
        (workspace / "link").symlink_to(harness.root / "eval.toml")

    harness.action = mutate
    result = await harness.run(forbidden_changes=["**"])
    assert result.verdict == "fail", result.failures
    assert result.manifest["changes"] == {
        "keep.txt": "deleted",
        "link": "created",
        "mode.txt": "modified",
        "new.txt": "created",
    }
    assert not (Path(result.artifact_dir) / "artifacts/link").exists()


@pytest.mark.asyncio
async def test_required_change_and_symlink_artifacts_cannot_fake_content(harness):
    (harness.root / "outside.txt").write_text("verified output")
    harness.action = lambda workspace, _: (workspace / "result.txt").symlink_to(
        harness.root / "outside.txt"
    )
    result = await harness.run(
        files_contain={"result.txt": ["verified"]}, required_changes=["missing.py"]
    )
    assert result.verdict == "fail"
    assert sum(c.verdict == "fail" for c in result.checks) == 2


@pytest.mark.asyncio
async def test_symlink_and_size_limited_fixtures_fail_before_run(harness):
    (harness.fixture / "link").symlink_to(harness.config)
    result = await harness.run(final_contains=["done"])
    assert result.failure_kind == "environment" and not harness.calls
    (harness.fixture / "link").unlink()
    (harness.fixture / "large").write_bytes(b"x" * 1024)
    result = await harness.run(final_contains=["done"], max_snapshot_bytes=100)
    assert result.failure_kind == "environment" and not harness.calls


@pytest.mark.asyncio
async def test_isolation_overrides_shared_state_memory_skills_and_user_agents(harness, monkeypatch):
    shared = harness.root / "shared"
    monkeypatch.setenv("BOT_STATE_PATH", str(shared / "state.db"))
    monkeypatch.setenv("BOT_MEMORY_PATH", str(shared / "memory"))
    (harness.fixture / ".bot").mkdir()
    (harness.fixture / ".bot/state.db").write_text("old db")
    (harness.fixture / ".env").write_text("private")

    def inspect(workspace, kwargs):
        cfg = kwargs["config_overrides"]
        assert not (workspace / ".env").exists()
        assert not (workspace / ".bot/state.db").exists()
        for path in [
            cfg["storage"]["state_path"],
            cfg["memory"]["path"],
            cfg["agents"]["user_path"],
            cfg["skills"]["path"],
        ]:
            assert Path(path).is_relative_to(workspace)
        (workspace / "generated").write_text("new")

    harness.action = inspect
    one = await harness.run(required_changes=["generated"])
    two = await harness.run(required_changes=["generated"])
    assert one.passed and two.passed, (one.failures, two.failures)
    assert harness.calls[0]["workspace"] != harness.calls[1]["workspace"]
    assert not shared.exists()
    assert (harness.fixture / ".bot/state.db").read_text() == "old db"


@pytest.mark.asyncio
async def test_explicit_memory_and_sqlite_seeds_are_copied_not_reused(harness):
    memory = harness.root / "seed-memory"
    memory.mkdir()
    (memory / "USER.md").write_text("seed preference")
    database = harness.root / "seed.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE evidence (value TEXT)")
        db.execute("INSERT INTO evidence VALUES ('original')")

    def inspect(workspace, kwargs):
        assert (workspace / ".bot/memory/USER.md").read_text() == "seed preference"
        with sqlite3.connect(kwargs["config_overrides"]["storage"]["state_path"]) as db:
            assert db.execute("SELECT value FROM evidence").fetchone() == ("original",)
            db.execute("DELETE FROM evidence")
        (workspace / ".bot/memory/USER.md").write_text("changed")

    harness.action = inspect
    result = await harness.run(
        final_contains=["done"], memory_fixture="seed-memory", state_fixture="seed.db"
    )
    assert result.passed, result.failures
    assert (memory / "USER.md").read_text() == "seed preference"
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT value FROM evidence").fetchone() == ("original",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,kind",
    [(RuntimeError("broken runner"), "runtime"), (ProviderError("offline"), "provider")],
)
async def test_errors_are_classified_and_runtime_always_closed(harness, error, kind):
    harness.error = error
    result = await harness.run(final_contains=["done"])
    assert result.verdict == "error" and result.failure_kind == kind
    assert harness.closed == 1
    assert not harness.calls[0]["workspace"].exists()


@pytest.mark.asyncio
async def test_cleanup_failure_prevents_success_and_cancellation_still_cleans(harness):
    harness.close_error = RuntimeError("cleanup broken")
    result = await harness.run(final_contains=["done"])
    assert result.verdict == "error" and result.failure_kind == "runtime"
    harness.close_error = None
    harness.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await harness.run(final_contains=["done"])
    assert harness.closed == 2
    assert not harness.calls[-1]["workspace"].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,reason,kind",
    [
        ("limit_reached", "max_cost_usd", "budget"),
        ("failed", "provider_error", "provider"),
        ("failed", "runtime_error", "runtime"),
        ("blocked", "no_progress", "task"),
    ],
)
async def test_terminal_failure_classification(harness, status, reason, kind):
    harness.run_result = RunResult(session_id="root", status=status, termination_reason=reason)
    result = await harness.run(final_contains=["done"])
    assert result.verdict == "fail" and result.failure_kind == kind


@pytest.mark.asyncio
async def test_config_and_error_artifacts_do_not_expose_api_key(harness):
    secret = "private-fixture-key-012345"
    harness.config.write_text(f'[model]\nname="fake"\napi_key="{secret}"\n')
    harness.error = RuntimeError(secret)
    result = await harness.run(final_contains=["done"])
    assert secret not in result.model_dump_json()
    for path in Path(result.artifact_dir).glob("*.json*"):
        assert secret not in path.read_text()
    assert "api_key" not in result.manifest["config"]["model"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("files_contain", {"../outside": ["x"]}),
        ("fixture_files", ["/etc/passwd"]),
        ("forbidden_changes", ["../*"]),
    ],
)
def test_case_paths_cannot_escape_fixture(field, value):
    with pytest.raises(ValidationError):
        EvalCase(id="bad", prompt="bad", **{field: value})


@pytest.mark.asyncio
async def test_grader_source_is_frozen_before_run_and_excluded_from_workspace(harness, monkeypatch):
    tests = harness.fixture / "skills/private-tests"
    tests.mkdir(parents=True)
    (tests / "check.sh").write_text("trusted test")

    async def image(_):
        return {"Id": "sha256:local"}

    async def grade(spec, **kwargs):
        assert kwargs["tests"]["check.sh"].content == b"trusted test"
        assert kwargs["artifacts"]["answer"].content == b"42"
        assert harness.closed == 1
        return CheckResult(name="command", verdict="pass")

    def act(workspace, _):
        assert not (workspace / "skills/private-tests").exists()
        assert not (workspace / ".bot/eval-skills/private-tests").exists()
        (tests / "check.sh").write_text("modified externally")
        (workspace / "answer").write_text("42")

    harness.action = act
    monkeypatch.setattr("bot.evals.runner.resolve_image", image)
    monkeypatch.setattr("bot.evals.runner.command_check", grade)
    result = await harness.run(
        verifiers=[
            {
                "kind": "command",
                "tests": "fixture/skills/private-tests",
                "image": "local",
                "argv": ["/bin/sh", "/verifier/check.sh"],
            }
        ]
    )
    assert result.passed, result.failures
    assert result.manifest["graders"][0]["image"]["Id"] == "sha256:local"


@pytest.mark.asyncio
async def test_missing_grader_image_is_environment_error_before_run(harness, monkeypatch):
    tests = harness.root / "tests"
    tests.mkdir()
    (tests / "check.sh").write_text("exit 0")

    async def unavailable(_):
        raise ValueError("image unavailable")

    monkeypatch.setattr("bot.evals.runner.resolve_image", unavailable)
    result = await harness.run(
        verifiers=[{"kind": "command", "tests": "tests", "image": "missing", "argv": ["sh"]}]
    )
    assert result.verdict == "error" and result.failure_kind == "environment"
    assert not harness.calls


@pytest.mark.asyncio
async def test_command_grader_cleanup_failure_overrides_pass(tmp_path, monkeypatch):
    async def capture(argv, max_seconds):
        if argv[1] == "run":
            (tmp_path / "output/result.json").write_text('{"passed":true}')
            return 0, "ok"
        assert argv[:3] == ["docker", "rm", "--force"]
        return 1, "cleanup broken"

    monkeypatch.setattr("bot.evals.verification.capture", capture)
    result = await command_check(
        CommandVerifier(tests="tests", image="local", argv=["sh"]),
        image={"Id": "local"},
        tests={},
        artifacts={},
        root=tmp_path,
    )
    assert result.verdict == "error" and "清理失败" in result.message


@pytest.mark.asyncio
async def test_command_grader_does_not_silently_drop_symlinks(tmp_path):
    result = await command_check(
        CommandVerifier(tests="tests", image="local", argv=["sh"]),
        image={"Id": "local"},
        tests={},
        artifacts={"link": Entry("symlink", 0o777, b"/etc/passwd")},
        root=tmp_path,
    )
    assert result.verdict == "error" and "符号链接" in result.message


def test_docker_env_preserves_context_config_without_model_credentials(monkeypatch):
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    monkeypatch.setenv("DOCKER_CONTEXT", "desktop-linux")
    monkeypatch.setenv("BOT_MODEL_API_KEY", "private-key")
    env = docker_env()
    assert env["DOCKER_CONFIG"] == str(Path.home() / ".docker")
    assert env["DOCKER_CONTEXT"] == "desktop-linux"
    assert "BOT_MODEL_API_KEY" not in env


@pytest.mark.asyncio
async def test_sensitive_generated_files_are_not_exported(harness):
    secret = "private-fixture-key-012345"
    harness.config.write_text(f'[model]\nname="fake"\napi_key="{secret}"\n')
    harness.action = lambda workspace, _: (workspace / "key.txt").write_text(secret)
    result = await harness.run(required_changes=["key.txt"])
    assert result.passed, result.failures
    assert result.manifest["omitted_sensitive_artifacts"] == ["key.txt"]
    assert "key.txt" in result.manifest["final_files"]
    assert not (Path(result.artifact_dir) / "artifacts/key.txt").exists()


@pytest.mark.asyncio
async def test_nested_runtime_named_paths_remain_visible_to_change_checks(harness):
    def act(workspace, _):
        (workspace / "output/.bot").mkdir(parents=True)
        (workspace / "output/.bot/hidden").write_text("unexpected")

    harness.action = act
    result = await harness.run(forbidden_changes=["**"])
    assert result.verdict == "fail"
    assert "output/.bot/hidden" in result.manifest["changes"]


@pytest.mark.asyncio
async def test_artifact_write_failure_returns_error_instead_of_aborting_batch(harness, monkeypatch):
    original = Path.write_text

    def fail_result_write(path, *args, **kwargs):
        if path.name == "result.json":
            raise OSError("disk full")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_result_write)
    result = await harness.run(final_contains=["done"])
    assert not result.passed and result.verdict == "error"
    assert result.failure_kind == "environment" and "disk full" in result.failures[-1]
