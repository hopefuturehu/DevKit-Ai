from __future__ import annotations

import argparse
import os
import shlex
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from bot.config import (
    ConfigError,
    api_key_reference_variable,
    load_config,
    resolve_model_api_key,
)

HARBOR_VERSION = "0.20.0"
TERMINALBENCH_DATASET = "terminal-bench/terminal-bench-2-1"
HARBOR_AGENT_IMPORT_PATH = "bot.evals.harbor_agent:KunpengBot"
DEFAULT_TERMINALBENCH_MAX_STEPS = 60
DEFAULT_TERMINALBENCH_MAX_WALL_TIME_SECONDS = 1800.0
DEFAULT_TERMINALBENCH_MAX_COST_USD: float | None = None


@dataclass(frozen=True)
class TerminalBenchSuite:
    tasks: tuple[str, ...]
    max_steps: int
    max_wall_time_seconds: float
    max_cost_usd: float | None


CONTEXT_MEDIUM_SIX = TerminalBenchSuite(
    tasks=(
        "custom-memory-heap-crash",
        "filter-js-from-html",
        "llm-inference-batching-scheduler",
        "mailman",
        "path-tracing-reverse",
        "large-scale-text-editing",
    ),
    # Harbor still enforces every task's published timeout.  These internal
    # ceilings are deliberately higher so this suite is not truncated by the
    # old 60-step / 30-minute smoke-test defaults first.
    max_steps=240,
    max_wall_time_seconds=7_200.0,
    max_cost_usd=None,
)
TERMINALBENCH_SUITES = {"context-medium-six": CONTEXT_MEDIUM_SIX}


def api_key_env_name(reference: str) -> str:
    try:
        return api_key_reference_variable(reference)
    except ConfigError as exc:
        raise ValueError(str(exc)) from exc


def validate_api_key_value(value: str) -> None:
    if not value:
        raise ValueError("值为空")
    if not value.isascii():
        raise ValueError("必须是 ASCII；请勿把中文弯引号包含在值中")
    if value != value.strip():
        raise ValueError("首尾不能包含空白")
    if value[0] in "'\"`" or value[-1] in "'\"`":
        raise ValueError("值本身不能包含包裹引号")
    if not value.isprintable():
        raise ValueError("不能包含控制字符")


def model_hostname(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Terminal-Bench 容器评测要求 model.base_url 使用有效的 HTTPS URL")
    return parsed.hostname


def build_harbor_command(
    *,
    uvx: Path,
    wheel_path: Path,
    config_path: Path,
    model_name: str,
    api_key_variable: str,
    model_host: str,
    jobs_dir: Path,
    tasks: list[str],
    run_all: bool,
    n_concurrent: int,
    n_attempts: int,
    max_steps: int,
    max_wall_time_seconds: float,
    max_cost_usd: float | None,
    subagents_enabled: bool,
    extra_args: list[str] | None = None,
) -> list[str]:
    if not run_all and not tasks:
        raise ValueError("必须通过 --task 选择 smoke task，或显式传入 --all")
    if run_all and tasks:
        raise ValueError("--task 与 --all 不能同时使用")
    if n_concurrent < 1 or n_attempts < 1:
        raise ValueError("并发数和重复次数必须大于 0")
    if max_steps < 1 or max_wall_time_seconds <= 0:
        raise ValueError("Agent 步数和时间限制必须大于 0")
    if max_cost_usd is not None and max_cost_usd <= 0:
        raise ValueError("Agent 费用预算必须大于 0")

    command = [
        str(uvx),
        "--from",
        f"harbor=={HARBOR_VERSION}",
        "--with",
        str(wheel_path),
        "harbor",
        "run",
        "--dataset",
        TERMINALBENCH_DATASET,
        "--agent",
        HARBOR_AGENT_IMPORT_PATH,
        "--model",
        f"openai-compatible/{model_name}",
        "--agent-kwarg",
        f"package_path={wheel_path}",
        "--agent-kwarg",
        f"config_path={config_path}",
        "--agent-kwarg",
        f"max_steps={max_steps}",
        "--agent-kwarg",
        f"max_wall_time_seconds={max_wall_time_seconds:g}",
        *(["--agent-kwarg", f"max_cost_usd={max_cost_usd:g}"] if max_cost_usd is not None else []),
        "--agent-kwarg",
        f"subagents_enabled={str(subagents_enabled).lower()}",
        "--agent-env",
        f"{api_key_variable}=${{{api_key_variable}}}",
        "--agent-env",
        f"BOT_MODEL_API_KEY_REF=env:{api_key_variable}",
        "--allow-agent-host",
        model_host,
        "--n-concurrent",
        str(n_concurrent),
        "--n-attempts",
        str(n_attempts),
        "--agent-setup-timeout-multiplier",
        "2",
        "--jobs-dir",
        str(jobs_dir),
    ]
    for task in tasks:
        task_name = task if task.startswith("terminal-bench/") else f"terminal-bench/{task}"
        command.extend(["--include-task-name", task_name])
    command.extend(extra_args or [])
    return command


def build_project_wheel(project_root: Path, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    before = {path.resolve(): path.stat().st_mtime_ns for path in output_dir.glob("*.whl")}
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(output_dir), str(project_root)],
        cwd=project_root,
        check=True,
    )
    candidates = list(output_dir.glob("*.whl"))
    changed = [path for path in candidates if before.get(path.resolve()) != path.stat().st_mtime_ns]
    if not changed:
        raise RuntimeError(f"uv build 未在 {output_dir} 生成 wheel")
    return max(changed, key=lambda path: path.stat().st_mtime_ns).resolve()


def _docker_available() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    completed = subprocess.run(
        [docker, "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def _harbor_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    # Harbor currently pulls litellm through an isolated maturin build on
    # macOS ARM64. Its temporary build environment has httpx but not socksio;
    # prefer the separately configured HTTP(S) proxy over a loopback SOCKS
    # proxy so Rust bootstrap does not fail before Harbor starts.
    for name in ("ALL_PROXY", "all_proxy"):
        if env.get(name, "").lower().startswith(("socks5://", "socks5h://")):
            env.pop(name)
    return env


def _run_harbor(command: list[str], *, cwd: Path, env: dict[str, str]) -> int:
    process = subprocess.Popen(
        command,
        cwd=cwd,
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
            process.wait(timeout=5)
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run kunpeng-cli-agent on Terminal-Bench 2.1 through Harbor"
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--task",
        action="append",
        help="Terminal-Bench task 名称；可重复。示例: openssl-selfsigned-cert",
    )
    selection.add_argument(
        "--suite",
        choices=sorted(TERMINALBENCH_SUITES),
        help="运行项目固定的可重复任务集",
    )
    selection.add_argument(
        "--all",
        action="store_true",
        help="运行全部 89 个任务；会消耗大量时间、算力和模型费用",
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, default=Path(".bot/config.toml"))
    parser.add_argument(
        "--wheel",
        type=Path,
        help="复用已构建 wheel；默认构建到 artifacts/terminalbench/packages",
    )
    parser.add_argument(
        "--jobs-dir",
        type=Path,
        default=Path("artifacts/terminalbench/jobs"),
    )
    parser.add_argument("--n-concurrent", type=int, default=1)
    parser.add_argument("--n-attempts", type=int, default=1)
    parser.add_argument("--max-steps", type=int)
    parser.add_argument(
        "--max-wall-time-seconds",
        type=float,
    )
    parser.add_argument(
        "--max-cost-usd",
        type=float,
    )
    parser.add_argument(
        "--subagents",
        action="store_true",
        help="启用本项目子 Agent；默认关闭以建立核心 harness 基线",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="完成预检和 wheel 构建，只打印 Harbor 命令",
    )
    parser.add_argument(
        "harbor_args",
        nargs=argparse.REMAINDER,
        help="在 -- 之后追加的 Harbor 参数",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    project_root = args.project_root.resolve()
    config_path = (
        args.config.resolve()
        if args.config.is_absolute()
        else (project_root / args.config).resolve()
    )
    jobs_dir = (
        args.jobs_dir.resolve()
        if args.jobs_dir.is_absolute()
        else (project_root / args.jobs_dir).resolve()
    )
    if not (project_root / "pyproject.toml").is_file():
        raise SystemExit(f"项目根目录无效: {project_root}")
    if not config_path.is_file():
        raise SystemExit(f"配置文件不存在: {config_path}")
    uvx_name = shutil.which("uvx")
    if uvx_name is None:
        raise SystemExit("未找到 uvx；请先安装 uv")
    if not _docker_available():
        raise SystemExit("Docker daemon 不可用；请启动 Docker Desktop 后重试")

    config = load_config(project_root, config_path=config_path)
    if not config.model.name:
        raise SystemExit("model.name 未配置")
    key_variable = api_key_env_name(config.model.api_key_ref)
    try:
        key_value = resolve_model_api_key(config.model, workspace=project_root)
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc
    try:
        validate_api_key_value(key_value)
    except ValueError as exc:
        raise SystemExit(f"API Key 引用 {config.model.api_key_ref} 的值无效: {exc}") from exc
    host = model_hostname(config.model.base_url)

    suite = TERMINALBENCH_SUITES.get(args.suite) if args.suite else None
    tasks = list(suite.tasks) if suite is not None else list(args.task or [])
    max_steps = (
        args.max_steps
        if args.max_steps is not None
        else (suite.max_steps if suite is not None else DEFAULT_TERMINALBENCH_MAX_STEPS)
    )
    max_wall_time_seconds = (
        args.max_wall_time_seconds
        if args.max_wall_time_seconds is not None
        else (
            suite.max_wall_time_seconds
            if suite is not None
            else DEFAULT_TERMINALBENCH_MAX_WALL_TIME_SECONDS
        )
    )
    max_cost_usd = (
        args.max_cost_usd
        if args.max_cost_usd is not None
        else (suite.max_cost_usd if suite is not None else DEFAULT_TERMINALBENCH_MAX_COST_USD)
    )

    if args.wheel is None:
        wheel_path = build_project_wheel(
            project_root,
            project_root / "artifacts" / "terminalbench" / "packages",
        )
    else:
        wheel_path = args.wheel.expanduser().resolve()
        if not wheel_path.is_file() or wheel_path.suffix != ".whl":
            raise SystemExit(f"wheel 不存在或格式无效: {wheel_path}")

    extra_args = list(args.harbor_args)
    if extra_args[:1] == ["--"]:
        extra_args.pop(0)
    command = build_harbor_command(
        uvx=Path(uvx_name).resolve(),
        wheel_path=wheel_path,
        config_path=config_path,
        model_name=config.model.name,
        api_key_variable=key_variable,
        model_host=host,
        jobs_dir=jobs_dir,
        tasks=tasks,
        run_all=args.all,
        n_concurrent=args.n_concurrent,
        n_attempts=args.n_attempts,
        max_steps=max_steps,
        max_wall_time_seconds=max_wall_time_seconds,
        max_cost_usd=max_cost_usd,
        subagents_enabled=args.subagents,
        extra_args=extra_args,
    )
    print(shlex.join(command), flush=True)
    if args.dry_run:
        return 0
    jobs_dir.mkdir(parents=True, exist_ok=True)
    harbor_env = _harbor_subprocess_env()
    harbor_env[key_variable] = key_value
    return _run_harbor(
        command,
        cwd=project_root,
        env=harbor_env,
    )


if __name__ == "__main__":
    raise SystemExit(main())
