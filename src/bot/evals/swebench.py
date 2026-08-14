from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from bot.config import api_key_reference_variable, load_config, resolve_api_key
from bot.evals.connect_proxy import restricted_connect_proxy
from bot.observability import export_trace_bundle

DEFAULT_SWEBENCH_MAX_STEPS = 60
DEFAULT_SWEBENCH_MAX_WALL_TIME_SECONDS = 1800.0
DEFAULT_SWEBENCH_MAX_COST_USD = 1.0


@dataclass(frozen=True)
class SWEbenchInstance:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> SWEbenchInstance:
        required = ("instance_id", "repo", "base_commit", "problem_statement")
        missing = [key for key in required if not isinstance(value.get(key), str) or not value[key]]
        if missing:
            raise ValueError(f"SWE-bench instance 缺少有效字段: {', '.join(missing)}")
        return cls(**{key: str(value[key]) for key in required})


def load_instance(path: Path, instance_id: str | None = None) -> SWEbenchInstance:
    """Load one instance from a JSON object, JSON array, or JSONL file."""
    text = path.read_text(encoding="utf-8")
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        decoded = [json.loads(line) for line in text.splitlines() if line.strip()]

    rows = decoded if isinstance(decoded, list) else [decoded]
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"{path} 必须包含 JSON object、object array 或 JSONL")
    if instance_id is not None:
        rows = [row for row in rows if row.get("instance_id") == instance_id]
    if len(rows) != 1:
        qualifier = f" instance_id={instance_id}" if instance_id else ""
        raise ValueError(f"{path}{qualifier} 应恰好匹配一个 instance，实际 {len(rows)}")
    return SWEbenchInstance.from_mapping(rows[0])


def build_agent_prompt(instance: SWEbenchInstance) -> str:
    return (
        "Fix the issue described below in the checked-out repository. "
        "Inspect the code, modify the implementation, and run relevant tests when possible. "
        "Do not merely describe a solution, do not commit changes, and do not access the "
        "network.\n\n"
        f"{instance.problem_statement}"
    )


def _run(
    argv: list[str],
    *,
    cwd: Path,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        check=check,
        text=True,
        capture_output=True,
        env=env,
    )


def _run_streaming(
    argv: list[str],
    *,
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    env: dict[str, str] | None = None,
) -> int:
    """Run a process with output written directly to tail-able host files."""
    with (
        stdout_path.open("w", encoding="utf-8") as stdout,
        stderr_path.open("w", encoding="utf-8") as stderr,
    ):
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            text=True,
            stdout=stdout,
            stderr=stderr,
            env=env,
            start_new_session=os.name == "posix",
        )
        try:
            return process.wait()
        except BaseException:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                process.wait()
            raise


def _api_key_process_environment(
    reference: str,
    *,
    workspace: Path,
) -> tuple[str, dict[str, str]]:
    """Resolve a host credential and normalize it for a child process."""
    variable = api_key_reference_variable(reference)
    value = resolve_api_key(reference, workspace=workspace)
    environment = os.environ.copy()
    environment[variable] = value
    environment["BOT_MODEL_API_KEY_REF"] = f"env:{variable}"
    return variable, environment


def _append_log(path: Path, content: str) -> None:
    if not content:
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()


def prepare_workspace(instance: SWEbenchInstance, workspace: Path) -> None:
    if workspace.exists():
        raise FileExistsError(f"拒绝覆盖已存在的评测工作区: {workspace}")
    workspace.mkdir(parents=True)
    _run(["git", "init", "--quiet"], cwd=workspace)
    _run(
        ["git", "remote", "add", "origin", f"https://github.com/{instance.repo}.git"],
        cwd=workspace,
    )
    _run(
        ["git", "fetch", "--quiet", "--depth", "1", "origin", instance.base_commit],
        cwd=workspace,
    )
    _run(["git", "checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=workspace)


def collect_model_patch(workspace: Path) -> str:
    # Intent-to-add makes untracked source files appear in git diff. Runtime state
    # remains excluded from the prediction passed to the official harness.
    _run(["git", "add", "-N", "--all"], cwd=workspace)
    completed = _run(
        [
            "git",
            "diff",
            "--binary",
            "--no-ext-diff",
            "--",
            ".",
            ":(exclude).bot/**",
        ],
        cwd=workspace,
    )
    return completed.stdout


def run_instance(
    instance: SWEbenchInstance,
    *,
    workspace: Path,
    bot_executable: Path,
    config_path: Path,
    output_path: Path,
    credential_workspace: Path | None = None,
) -> int:
    if credential_workspace is None:
        config_parent = config_path.resolve().parent
        credential_workspace = (
            config_parent.parent if config_parent.name == ".bot" else config_parent
        )
    config = load_config(credential_workspace, config_path=config_path)
    _, process_environment = _api_key_process_environment(
        config.model.api_key_ref,
        workspace=credential_workspace,
    )
    prepare_workspace(instance, workspace)
    prompt = build_agent_prompt(instance)
    command = [
        str(bot_executable),
        "-C",
        str(workspace),
        "--config",
        str(config_path),
        "run",
        prompt,
        "--json",
    ]
    completed = _run(command, cwd=workspace, check=False, env=process_environment)
    patch = collect_model_patch(workspace)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    prediction = {
        "instance_id": instance.instance_id,
        "model_name_or_path": process_environment.get("BOT_MODEL_NAME", config.model.name),
        "model_patch": patch,
    }
    output_path.write_text(json.dumps(prediction, ensure_ascii=False) + "\n", encoding="utf-8")
    output_path.with_suffix(".events.jsonl").write_text(completed.stdout, encoding="utf-8")
    output_path.with_suffix(".stderr.log").write_text(completed.stderr, encoding="utf-8")
    return completed.returncode


def run_container_instance(
    instance: SWEbenchInstance,
    *,
    image: str,
    project_root: Path,
    config_path: Path,
    output_path: Path,
    max_steps: int = DEFAULT_SWEBENCH_MAX_STEPS,
    max_wall_time_seconds: float = DEFAULT_SWEBENCH_MAX_WALL_TIME_SECONDS,
    max_cost_usd: float = DEFAULT_SWEBENCH_MAX_COST_USD,
) -> int:
    """Run the agent inside a disposable SWE-bench instance container."""
    if not (project_root / "pyproject.toml").is_file():
        raise ValueError(f"项目根目录无效: {project_root}")
    if not config_path.is_file():
        raise ValueError(f"配置文件不存在: {config_path}")
    if max_steps < 1:
        raise ValueError("SWE-bench max_steps 必须大于 0")
    if max_wall_time_seconds <= 0:
        raise ValueError("SWE-bench max_wall_time_seconds 必须大于 0")
    if max_cost_usd <= 0:
        raise ValueError("SWE-bench max_cost_usd 必须大于 0")

    config = load_config(project_root, config_path=config_path)
    key_variable, process_environment = _api_key_process_environment(
        config.model.api_key_ref,
        workspace=project_root,
    )
    model_url = urlparse(config.model.base_url)
    if model_url.scheme != "https" or not model_url.hostname:
        raise ValueError("容器评测要求 model.base_url 使用有效的 HTTPS URL")
    model_port = model_url.port or 443

    output_path.parent.mkdir(parents=True, exist_ok=True)
    events_path = output_path.with_suffix(".events.jsonl")
    stderr_path = output_path.with_suffix(".stderr.log")
    setup_path = output_path.with_suffix(".setup.log")
    result_path = output_path.with_suffix(".result.json")
    state_path = output_path.with_suffix(".state.db")
    trace_dir = output_path.with_suffix(".trace")
    for path in (events_path, stderr_path, setup_path):
        path.write_text("", encoding="utf-8")
        path.chmod(0o600)
    for path in (result_path, state_path):
        if path.exists():
            path.unlink()

    safe_id = re.sub(r"[^a-zA-Z0-9_.-]+", "-", instance.instance_id)[:48]
    container_name = f"kunpeng-swe-{safe_id}-{uuid4().hex[:8]}"
    worker_returncode: int | None = None
    patch = ""
    started = False
    with restricted_connect_proxy(model_url.hostname, model_port) as proxy_port:
        _append_log(
            setup_path,
            f"temporary model CONNECT proxy: {model_url.hostname}:{model_port} "
            f"via host.docker.internal:{proxy_port}\n",
        )
        _append_log(
            setup_path,
            "SWE-bench worker limits: "
            f"max_steps={max_steps}, "
            f"max_wall_time_seconds={max_wall_time_seconds:g}, "
            f"max_cost_usd={max_cost_usd:g}\n",
        )
        try:
            mount = f"type=bind,src={project_root},dst=/opt/kunpeng-bot-src,readonly"
            created = _run(
                [
                    "docker",
                    "run",
                    "--detach",
                    "--platform",
                    "linux/amd64",
                    "--name",
                    container_name,
                    "--mount",
                    mount,
                    image,
                    "sleep",
                    "infinity",
                ],
                cwd=project_root,
            )
            _append_log(setup_path, created.stdout + created.stderr)
            started = True

            with tempfile.TemporaryDirectory(prefix="swebench-instance-") as temporary:
                instance_path = Path(temporary) / "instance.json"
                instance_path.write_text(
                    json.dumps(instance.__dict__, ensure_ascii=False), encoding="utf-8"
                )
                copied = _run(
                    ["docker", "cp", str(instance_path), f"{container_name}:/tmp/instance.json"],
                    cwd=project_root,
                )
                _append_log(setup_path, copied.stdout + copied.stderr)
            copied_config = _run(
                ["docker", "cp", str(config_path), f"{container_name}:/tmp/bot-config.toml"],
                cwd=project_root,
            )
            _append_log(setup_path, copied_config.stdout + copied_config.stderr)

            setup_commands = [
                [
                    "docker",
                    "exec",
                    container_name,
                    "/opt/miniconda3/bin/python",
                    "-m",
                    "pip",
                    "install",
                    "uv",
                ],
                [
                    "docker",
                    "exec",
                    container_name,
                    "/opt/miniconda3/bin/uv",
                    "venv",
                    "--python",
                    "3.12",
                    "/opt/kunpeng-bot-venv",
                ],
                [
                    "docker",
                    "exec",
                    container_name,
                    "/opt/miniconda3/bin/uv",
                    "pip",
                    "install",
                    "--python",
                    "/opt/kunpeng-bot-venv/bin/python",
                    "/opt/kunpeng-bot-src",
                ],
            ]
            for command in setup_commands:
                completed = _run(command, cwd=project_root)
                _append_log(setup_path, completed.stdout + completed.stderr)

            proxy_url = f"http://host.docker.internal:{proxy_port}"
            worker_returncode = _run_streaming(
                [
                    "docker",
                    "exec",
                    "--env",
                    key_variable,
                    "--env",
                    f"BOT_MODEL_API_KEY_REF=env:{key_variable}",
                    "--env",
                    f"HTTPS_PROXY={proxy_url}",
                    "--env",
                    f"https_proxy={proxy_url}",
                    "--env",
                    "SWEBENCH_CONTAINER=1",
                    container_name,
                    "/opt/kunpeng-bot-venv/bin/python",
                    "-m",
                    "bot.evals.swebench_worker",
                    "/tmp/instance.json",
                    "--workspace",
                    "/testbed",
                    "--config",
                    "/tmp/bot-config.toml",
                    "--result-file",
                    "/tmp/run-result.json",
                    "--state-backup",
                    "/tmp/trace-state.db",
                    "--max-steps",
                    str(max_steps),
                    "--max-wall-time-seconds",
                    str(max_wall_time_seconds),
                    "--max-cost-usd",
                    str(max_cost_usd),
                ],
                cwd=project_root,
                stdout_path=events_path,
                stderr_path=stderr_path,
                env=process_environment,
            )
            copied_state = _run(
                ["docker", "cp", f"{container_name}:/tmp/trace-state.db", str(state_path)],
                cwd=project_root,
                check=False,
            )
            _append_log(setup_path, copied_state.stdout + copied_state.stderr)
            if state_path.is_file():
                state_path.chmod(0o600)
            copied_result = _run(
                ["docker", "cp", f"{container_name}:/tmp/run-result.json", str(result_path)],
                cwd=project_root,
                check=False,
            )
            _append_log(setup_path, copied_result.stdout + copied_result.stderr)
            if result_path.is_file():
                result_path.chmod(0o600)
            _run(
                [
                    "docker",
                    "exec",
                    container_name,
                    "git",
                    "-C",
                    "/testbed",
                    "add",
                    "-N",
                    "--all",
                ],
                cwd=project_root,
            )
            patch = _run(
                [
                    "docker",
                    "exec",
                    container_name,
                    "git",
                    "-C",
                    "/testbed",
                    "diff",
                    "--binary",
                    "--no-ext-diff",
                    "--",
                    ".",
                    ":(exclude).bot/**",
                ],
                cwd=project_root,
            ).stdout
        finally:
            if started:
                removed = _run(
                    ["docker", "rm", "--force", container_name],
                    cwd=project_root,
                    check=False,
                )
                _append_log(setup_path, removed.stdout + removed.stderr)

    prediction = {
        "instance_id": instance.instance_id,
        "model_name_or_path": os.environ.get("BOT_MODEL_NAME", config.model.name),
        "model_patch": patch,
    }
    output_path.write_text(json.dumps(prediction, ensure_ascii=False) + "\n", encoding="utf-8")
    output_path.chmod(0o600)
    if state_path.is_file():
        try:
            export_trace_bundle(
                events_path=events_path,
                state_path=state_path,
                output_dir=trace_dir,
                prediction_path=output_path,
                result_path=result_path,
                setup_log_path=setup_path,
                stderr_log_path=stderr_path,
            )
        except Exception as exc:
            _append_log(setup_path, f"trace bundle export failed: {type(exc).__name__}: {exc}\n")
    return worker_returncode if worker_returncode is not None else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run kunpeng-cli-agent on one SWE-bench instance")
    parser.add_argument("instance_file", type=Path)
    parser.add_argument("--instance-id")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--bot", type=Path, default=Path(".venv/bin/bot"))
    args = parser.parse_args(argv)

    instance = load_instance(args.instance_file.resolve(), args.instance_id)
    return run_instance(
        instance,
        workspace=args.workspace.resolve(),
        bot_executable=args.bot.resolve(),
        config_path=args.config.resolve(),
        output_path=args.output.resolve(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
