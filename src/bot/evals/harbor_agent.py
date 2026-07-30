from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, override

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext


class KunpengBot(BaseInstalledAgent):
    """Harbor installed-agent adapter for kunpeng-cli-agent."""

    SUPPORTS_ATIF = False
    _REMOTE_ROOT = "/installed-agent"
    _REMOTE_VENV = f"{_REMOTE_ROOT}/venv"
    _REMOTE_CONFIG = f"{_REMOTE_ROOT}/config.toml"
    _REMOTE_INSTRUCTION = f"{_REMOTE_ROOT}/instruction.md"

    @staticmethod
    @override
    def name() -> str:
        return "kunpeng-bot"

    def __init__(
        self,
        logs_dir: Path,
        *,
        package_path: str,
        config_path: str,
        max_steps: int = 60,
        max_wall_time_seconds: float = 1800,
        max_cost_usd: float = 1.0,
        subagents_enabled: bool = False,
        **kwargs: Any,
    ) -> None:
        self.package_path = Path(package_path).expanduser().resolve()
        self.config_path = Path(config_path).expanduser().resolve()
        if not self.package_path.is_file() or self.package_path.suffix != ".whl":
            raise ValueError(f"kunpeng bot wheel 不存在或格式无效: {self.package_path}")
        if not self.config_path.is_file():
            raise ValueError(f"kunpeng bot 配置不存在: {self.config_path}")
        if max_steps < 1 or max_wall_time_seconds <= 0 or max_cost_usd <= 0:
            raise ValueError("Terminal-Bench Agent 预算必须大于 0")
        self.max_steps = int(max_steps)
        self.max_wall_time_seconds = float(max_wall_time_seconds)
        self.max_cost_usd = float(max_cost_usd)
        self.subagents_enabled = bool(subagents_enabled)
        super().__init__(logs_dir, **kwargs)

    @override
    def get_version_command(self) -> str | None:
        return f"{self._REMOTE_VENV}/bin/bot --version"

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        await environment.upload_file(
            self.package_path,
            f"{self._REMOTE_ROOT}/{self.package_path.name}",
        )
        await environment.upload_file(self.config_path, self._REMOTE_CONFIG)
        # Harbor 0.20.0's published BaseInstalledAgent predates the
        # ensure_system_dependencies helper that is present on main.
        await self.exec_as_root(
            environment,
            command=(
                "set -euo pipefail; "
                "if command -v curl >/dev/null 2>&1; then exit 0; fi; "
                "if command -v apt-get >/dev/null 2>&1; then "
                "export DEBIAN_FRONTEND=noninteractive; "
                "apt-get update && apt-get install -y curl ca-certificates; "
                "elif command -v apk >/dev/null 2>&1; then "
                "apk add --no-cache curl ca-certificates; "
                "elif command -v dnf >/dev/null 2>&1; then "
                "dnf install -y curl ca-certificates; "
                "elif command -v yum >/dev/null 2>&1; then "
                "yum install -y curl ca-certificates; "
                "else echo 'curl is required to install uv' >&2; exit 1; fi"
            ),
        )
        remote_wheel = f"{self._REMOTE_ROOT}/{self.package_path.name}"
        await self.exec_as_root(
            environment,
            command=(
                "set -euo pipefail; "
                "if ! command -v uv >/dev/null 2>&1; then "
                "curl -LsSf https://astral.sh/uv/0.11.3/install.sh "
                "| env UV_INSTALL_DIR=/usr/local/bin sh; "
                "fi; "
                "PYTHON_SPEC=python3; "
                "if ! command -v python3 >/dev/null 2>&1 "
                "|| ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'; "
                "then "
                "UV_PYTHON_INSTALL_DIR=/installed-agent/python uv python install 3.12; "
                "PYTHON_SPEC=3.12; "
                "fi; "
                "UV_PYTHON_INSTALL_DIR=/installed-agent/python "
                f'uv venv --python "$PYTHON_SPEC" {self._REMOTE_VENV}; '
                "UV_PYTHON_INSTALL_DIR=/installed-agent/python "
                f"uv pip install --python {self._REMOTE_VENV}/bin/python "
                f"{shlex.quote(remote_wheel)}; "
                f"{self._REMOTE_VENV}/bin/bot --version"
            ),
        )

    @override
    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        local_instruction = self.logs_dir / "instruction.md"
        local_instruction.write_text(instruction, encoding="utf-8")
        await environment.upload_file(local_instruction, self._REMOTE_INSTRUCTION)

        pwd = await environment.exec(command="pwd")
        if pwd.return_code != 0 or not pwd.stdout or not pwd.stdout.strip():
            raise RuntimeError("无法确定 Terminal-Bench 任务工作目录")
        workspace = pwd.stdout.strip().splitlines()[-1]
        if not workspace.startswith("/"):
            raise RuntimeError(f"Terminal-Bench 任务工作目录不是绝对路径: {workspace}")

        argv = [
            f"{self._REMOTE_VENV}/bin/python",
            "-m",
            "bot.evals.terminalbench_worker",
            "--instruction",
            self._REMOTE_INSTRUCTION,
            "--workspace",
            workspace,
            "--config",
            self._REMOTE_CONFIG,
            "--max-steps",
            str(self.max_steps),
            "--max-wall-time-seconds",
            f"{self.max_wall_time_seconds:g}",
            "--max-cost-usd",
            f"{self.max_cost_usd:g}",
            ("--subagents-enabled" if self.subagents_enabled else "--no-subagents-enabled"),
        ]
        await self.exec_as_agent(
            environment,
            command=f"{shlex.join(argv)} 2>&1 | tee /logs/agent/worker.log",
            cwd=workspace,
            env={"HARBOR_CONTAINER": "1"},
        )

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        result_path = self.logs_dir / "result.json"
        if not result_path.is_file():
            return
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        context.n_input_tokens = int(result.get("input_tokens") or 0)
        context.n_output_tokens = int(result.get("output_tokens") or 0)
        cost = result.get("cost_usd")
        context.cost_usd = float(cost) if cost is not None else None
        context.metadata = {
            "status": result.get("status"),
            "steps": result.get("steps"),
            "session_id": result.get("session_id"),
            "error": result.get("error"),
        }
