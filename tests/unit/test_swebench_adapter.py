import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from bot.evals.connect_proxy import _connect_target_allowed
from bot.evals.swebench import (
    SWEbenchInstance,
    _api_key_process_environment,
    _run_streaming,
    build_agent_prompt,
    collect_model_patch,
    load_instance,
)
from bot.evals.swebench_worker import (
    _validate_container_artifact_path,
    _worker_config_overrides,
    run_worker,
)


def test_load_instance_selects_one_jsonl_row(tmp_path: Path) -> None:
    path = tmp_path / "instances.jsonl"
    rows = [
        {
            "instance_id": name,
            "repo": "owner/repo",
            "base_commit": "abc123",
            "problem_statement": f"fix {name}",
        }
        for name in ("first", "second")
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    instance = load_instance(path, "second")

    assert instance.instance_id == "second"
    assert instance.problem_statement == "fix second"


def test_load_instance_rejects_ambiguous_input(tmp_path: Path) -> None:
    path = tmp_path / "instances.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="恰好匹配一个"):
        load_instance(path)


def test_prompt_requires_an_implementation_not_just_an_explanation() -> None:
    instance = SWEbenchInstance("case", "owner/repo", "abc123", "broken behavior")

    prompt = build_agent_prompt(instance)

    assert "modify the implementation" in prompt
    assert "Do not merely describe" in prompt
    assert prompt.endswith("broken behavior")


def test_collect_model_patch_includes_new_files_but_excludes_runtime_state(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.py").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "baseline"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.py").write_text("after\n", encoding="utf-8")
    (tmp_path / "new.py").write_text("new\n", encoding="utf-8")
    (tmp_path / ".bot").mkdir()
    (tmp_path / ".bot" / "state.db").write_text("runtime\n", encoding="utf-8")

    patch = collect_model_patch(tmp_path)

    assert "tracked.py" in patch
    assert "new.py" in patch
    assert ".bot/state.db" not in patch


@pytest.mark.asyncio
async def test_container_worker_refuses_to_run_without_explicit_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SWEBENCH_CONTAINER", raising=False)

    with pytest.raises(RuntimeError, match="只允许在显式标记的一次性容器中运行"):
        await run_worker(
            tmp_path / "instance.json",
            Path("/testbed"),
            tmp_path / "config.toml",
        )


def test_container_worker_artifacts_are_restricted_to_tmp() -> None:
    expected = Path("/tmp/trace/state.db").resolve()
    assert _validate_container_artifact_path(Path("/tmp/trace/state.db")) == expected
    with pytest.raises(RuntimeError, match="产物路径必须位于 /tmp"):
        _validate_container_artifact_path(Path("/testbed/.bot/state-copy.db"))


def test_swebench_worker_uses_independent_agent_limits() -> None:
    overrides = _worker_config_overrides(
        max_steps=72,
        max_wall_time_seconds=2400,
        max_cost_usd=1.5,
    )

    assert overrides["agent"] == {
        "max_steps": 72,
        "max_wall_time_seconds": 2400,
        "max_cost_usd": 1.5,
    }
    with pytest.raises(ValueError, match="max_steps"):
        _worker_config_overrides(
            max_steps=0,
            max_wall_time_seconds=2400,
            max_cost_usd=1.5,
        )


def test_restricted_connect_proxy_only_accepts_configured_upstream() -> None:
    assert _connect_target_allowed("CONNECT api.example:443 HTTP/1.1", "api.example", 443)
    assert not _connect_target_allowed("CONNECT forbidden.example:443 HTTP/1.1", "api.example", 443)
    assert not _connect_target_allowed("GET api.example:443 HTTP/1.1", "api.example", 443)


def test_api_key_environment_normalizes_dotenv_for_child_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SWEBENCH_TEST_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "SWEBENCH_TEST_KEY=dotenv-secret\n",
        encoding="utf-8",
    )

    variable, environment = _api_key_process_environment(
        "dotenv:SWEBENCH_TEST_KEY",
        workspace=tmp_path,
    )

    assert variable == "SWEBENCH_TEST_KEY"
    assert environment[variable] == "dotenv-secret"
    assert environment["BOT_MODEL_API_KEY_REF"] == "env:SWEBENCH_TEST_KEY"
    assert "SWEBENCH_TEST_KEY" not in os.environ


def test_streaming_process_writes_stdout_and_stderr_to_files(tmp_path: Path) -> None:
    stdout_path = tmp_path / "events.jsonl"
    stderr_path = tmp_path / "stderr.log"

    returncode = _run_streaming(
        [
            sys.executable,
            "-c",
            "import sys; print('event'); print('diagnostic', file=sys.stderr)",
        ],
        cwd=tmp_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )

    assert returncode == 0
    assert stdout_path.read_text(encoding="utf-8") == "event\n"
    assert stderr_path.read_text(encoding="utf-8") == "diagnostic\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
def test_streaming_process_interrupt_terminates_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[tuple[int, signal.Signals]] = []

    class FakeProcess:
        pid = 4242
        waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt
            return -signal.SIGTERM

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(
        os,
        "killpg",
        lambda pid, sig: signals.append((pid, signal.Signals(sig))),
    )

    with pytest.raises(KeyboardInterrupt):
        _run_streaming(
            ["worker"],
            cwd=tmp_path,
            stdout_path=tmp_path / "stdout.log",
            stderr_path=tmp_path / "stderr.log",
        )

    assert signals == [(4242, signal.SIGTERM)]
