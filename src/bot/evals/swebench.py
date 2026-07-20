from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from bot.config import load_config
from bot.evals.connect_proxy import restricted_connect_proxy


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


def _run(argv: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        check=check,
        text=True,
        capture_output=True,
    )


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
) -> int:
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
    completed = _run(command, cwd=workspace, check=False)
    patch = collect_model_patch(workspace)
    model_name = load_config(workspace, config_path=config_path).model.name

    output_path.parent.mkdir(parents=True, exist_ok=True)
    prediction = {
        "instance_id": instance.instance_id,
        "model_name_or_path": os.environ.get("BOT_MODEL_NAME", model_name),
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
) -> int:
    """Run the agent inside a disposable SWE-bench instance container."""
    if not (project_root / "pyproject.toml").is_file():
        raise ValueError(f"项目根目录无效: {project_root}")
    if not config_path.is_file():
        raise ValueError(f"配置文件不存在: {config_path}")

    config = load_config(project_root, config_path=config_path)
    model_url = urlparse(config.model.base_url)
    if model_url.scheme != "https" or not model_url.hostname:
        raise ValueError("容器评测要求 model.base_url 使用有效的 HTTPS URL")
    model_port = model_url.port or 443

    safe_id = re.sub(r"[^a-zA-Z0-9_.-]+", "-", instance.instance_id)[:48]
    container_name = f"kunpeng-swe-{safe_id}-{uuid4().hex[:8]}"
    setup_output: list[str] = []
    worker: subprocess.CompletedProcess[str] | None = None
    patch = ""
    started = False
    with restricted_connect_proxy(model_url.hostname, model_port) as proxy_port:
        setup_output.append(
            f"temporary model CONNECT proxy: {model_url.hostname}:{model_port} "
            f"via host.docker.internal:{proxy_port}\n"
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
            setup_output.append(created.stdout + created.stderr)
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
                setup_output.append(copied.stdout + copied.stderr)
            copied_config = _run(
                ["docker", "cp", str(config_path), f"{container_name}:/tmp/bot-config.toml"],
                cwd=project_root,
            )
            setup_output.append(copied_config.stdout + copied_config.stderr)

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
                setup_output.append(completed.stdout + completed.stderr)

            proxy_url = f"http://host.docker.internal:{proxy_port}"
            worker = _run(
                [
                    "docker",
                    "exec",
                    "--env",
                    "BOT_MODEL_API_KEY",
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
                ],
                cwd=project_root,
                check=False,
            )
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
                setup_output.append(removed.stdout + removed.stderr)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    prediction = {
        "instance_id": instance.instance_id,
        "model_name_or_path": os.environ.get("BOT_MODEL_NAME", config.model.name),
        "model_patch": patch,
    }
    output_path.write_text(json.dumps(prediction, ensure_ascii=False) + "\n", encoding="utf-8")
    output_path.with_suffix(".events.jsonl").write_text(
        worker.stdout if worker else "", encoding="utf-8"
    )
    output_path.with_suffix(".stderr.log").write_text(
        worker.stderr if worker else "", encoding="utf-8"
    )
    output_path.with_suffix(".setup.log").write_text("".join(setup_output), encoding="utf-8")
    return worker.returncode if worker else 1


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
