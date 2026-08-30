import os
import signal
import subprocess
from pathlib import Path

import pytest

from bot.evals.terminalbench import (
    CONTEXT_MEDIUM_SIX,
    HARBOR_AGENT_IMPORT_PATH,
    HARBOR_VERSION,
    TERMINALBENCH_DATASET,
    _harbor_subprocess_env,
    _parser,
    _run_harbor,
    api_key_env_name,
    build_harbor_command,
    model_hostname,
    validate_api_key_value,
)
from bot.evals.terminalbench_worker import (
    _validate_log_path,
    _worker_config_overrides,
    run_worker,
)


def test_build_harbor_command_uses_dataset_adapter_and_secret_template(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "agent.whl"
    config = tmp_path / "config.toml"

    command = build_harbor_command(
        uvx=Path("/usr/bin/uvx"),
        wheel_path=wheel,
        config_path=config,
        model_name="example-model",
        api_key_variable="BOT_MODEL_API_KEY",
        model_host="api.example.com",
        jobs_dir=tmp_path / "jobs",
        tasks=["terminal-bench/openssl-selfsigned-cert"],
        run_all=False,
        n_concurrent=1,
        n_attempts=1,
        max_steps=60,
        max_wall_time_seconds=1800,
        max_cost_usd=1.0,
        subagents_enabled=False,
    )

    assert f"harbor=={HARBOR_VERSION}" in command
    assert TERMINALBENCH_DATASET in command
    assert HARBOR_AGENT_IMPORT_PATH in command
    assert "BOT_MODEL_API_KEY=${BOT_MODEL_API_KEY}" in command
    assert "BOT_MODEL_API_KEY_REF=env:BOT_MODEL_API_KEY" in command
    assert "terminal-bench/openssl-selfsigned-cert" in command
    assert "package_path=" + str(wheel) in command


def test_context_medium_suite_uses_non_smoke_internal_ceilings() -> None:
    assert CONTEXT_MEDIUM_SIX.tasks == (
        "custom-memory-heap-crash",
        "filter-js-from-html",
        "llm-inference-batching-scheduler",
        "mailman",
        "path-tracing-reverse",
        "large-scale-text-editing",
    )
    assert CONTEXT_MEDIUM_SIX.max_steps > 60
    assert CONTEXT_MEDIUM_SIX.max_wall_time_seconds > 1800
    assert CONTEXT_MEDIUM_SIX.max_cost_usd is None


def test_build_harbor_command_omits_cost_limit_by_default(tmp_path: Path) -> None:
    command = build_harbor_command(
        uvx=Path("/usr/bin/uvx"),
        wheel_path=tmp_path / "agent.whl",
        config_path=tmp_path / "config.toml",
        model_name="model",
        api_key_variable="API_KEY",
        model_host="api.example.com",
        jobs_dir=tmp_path / "jobs",
        tasks=["mailman"],
        run_all=False,
        n_concurrent=1,
        n_attempts=1,
        max_steps=60,
        max_wall_time_seconds=1800,
        max_cost_usd=None,
        subagents_enabled=False,
    )

    assert not any(value.startswith("max_cost_usd=") for value in command)


def test_terminalbench_cli_has_no_default_cost_limit() -> None:
    args = _parser().parse_args(["--task", "mailman"])

    assert args.max_cost_usd is None


def test_build_harbor_command_requires_explicit_task_scope(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="--task"):
        build_harbor_command(
            uvx=Path("/usr/bin/uvx"),
            wheel_path=tmp_path / "agent.whl",
            config_path=tmp_path / "config.toml",
            model_name="model",
            api_key_variable="API_KEY",
            model_host="api.example.com",
            jobs_dir=tmp_path / "jobs",
            tasks=[],
            run_all=False,
            n_concurrent=1,
            n_attempts=1,
            max_steps=1,
            max_wall_time_seconds=1,
            max_cost_usd=1,
            subagents_enabled=False,
        )


def test_terminalbench_model_connection_validation() -> None:
    assert api_key_env_name("env:MODEL_KEY") == "MODEL_KEY"
    assert api_key_env_name("auto:MODEL_KEY") == "MODEL_KEY"
    assert api_key_env_name("dotenv:MODEL_KEY") == "MODEL_KEY"
    assert model_hostname("https://api.example.com/v1") == "api.example.com"
    validate_api_key_value("sk-valid-example")
    with pytest.raises(ValueError, match="变量名无效"):
        api_key_env_name("env:not-valid!")
    with pytest.raises(ValueError, match="HTTPS"):
        model_hostname("http://api.example.com/v1")
    with pytest.raises(ValueError, match="弯引号"):
        validate_api_key_value("‘sk-invalid-example’")
    with pytest.raises(ValueError, match="包裹引号"):
        validate_api_key_value("'sk-invalid-example'")
    with pytest.raises(ValueError, match="空白"):
        validate_api_key_value(" sk-invalid-example")


def test_harbor_environment_prefers_http_proxy_over_socks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:7890")
    monkeypatch.setenv("all_proxy", "socks5h://127.0.0.1:7890")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")

    env = _harbor_subprocess_env()

    assert "ALL_PROXY" not in env
    assert "all_proxy" not in env
    assert env["HTTPS_PROXY"] == os.environ["HTTPS_PROXY"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
def test_harbor_interrupt_terminates_process_group(
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
        _run_harbor(["harbor"], cwd=tmp_path, env={})

    assert signals == [(4242, signal.SIGTERM)]


def test_terminalbench_worker_uses_disposable_container_policy(tmp_path: Path) -> None:
    state_path = tmp_path / "state.db"

    overrides = _worker_config_overrides(
        state_path=state_path,
        max_steps=72,
        max_wall_time_seconds=2400,
        max_cost_usd=1.5,
        subagents_enabled=True,
    )

    assert overrides["agent"] == {
        "max_steps": 72,
        "max_wall_time_seconds": 2400,
        "max_cost_usd": 1.5,
    }
    assert overrides["permissions"] == {
        "mode": "full-access",
        "workspace_only": False,
        "network": "allow",
    }
    assert overrides["subagents"] == {"enabled": True}
    assert overrides["skills"] == {
        "path": "/installed-agent/no-skills",
        "auto_activate": False,
        "max_auto_activated": 0,
    }


def test_terminalbench_worker_omits_cost_override_by_default(tmp_path: Path) -> None:
    overrides = _worker_config_overrides(
        state_path=tmp_path / "state.db",
        max_steps=60,
        max_wall_time_seconds=1800,
        max_cost_usd=None,
        subagents_enabled=False,
    )

    assert overrides["agent"] == {
        "max_steps": 60,
        "max_wall_time_seconds": 1800,
    }


def test_terminalbench_worker_artifacts_are_restricted_to_agent_logs() -> None:
    assert _validate_log_path(Path("/logs/agent/result.json")) == Path("/logs/agent/result.json")
    with pytest.raises(RuntimeError, match="/logs/agent"):
        _validate_log_path(Path("/app/result.json"))


@pytest.mark.asyncio
async def test_terminalbench_worker_refuses_host_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HARBOR_CONTAINER", raising=False)

    with pytest.raises(RuntimeError, match="一次性容器"):
        await run_worker(
            tmp_path / "instruction.md",
            tmp_path,
            tmp_path / "config.toml",
            events_path=Path("/logs/agent/events.jsonl"),
            result_path=Path("/logs/agent/result.json"),
            state_path=Path("/logs/agent/state.db"),
            trace_path=Path("/logs/agent/trace"),
            max_steps=1,
            max_wall_time_seconds=1,
            max_cost_usd=1,
            subagents_enabled=False,
        )
