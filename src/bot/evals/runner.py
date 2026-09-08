from __future__ import annotations

import hashlib
import json
import os
import platform
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from pydantic import SecretStr

from bot.cli.runtime import Runtime, build_runtime
from bot.config import load_config, resolve_model_api_key
from bot.core.events import AgentEvent, EventType, MemoryEventSink
from bot.core.models import RunRequest, RunResult
from bot.evals.isolation import changes, digest, inventory, materialize, snapshot
from bot.evals.models import CheckResult, CommandVerifier, EvalCase, EvalResult, JsonVerifier
from bot.evals.verification import (
    VERIFIER_VERSION,
    check,
    command_check,
    file_checks,
    has_assertions,
    resolve_image,
    tool_checks,
    validate_schema,
)
from bot.observability import Redactor
from bot.providers import ProviderError


def load_eval_cases(path: Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            cases.append(EvalCase.model_validate_json(stripped))
        except Exception as exc:
            raise ValueError(f"{path}:{line_number}: Eval case 无效: {exc}") from exc
    identifiers = [case.id for case in cases]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"{path}: Eval case id 重复")
    return cases


def _git(root: Path, *args: str) -> str:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    return result.stdout.strip()


def _code_manifest() -> dict:
    source = Path(__file__).resolve().parents[1]
    files = {
        p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(source.rglob("*.py"))
    }
    code = {
        "package_version": version("kunpeng-cli-agent"),
        "source_sha256": digest(files),
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
    try:
        code["git_commit"] = _git(source, "rev-parse", "HEAD")
        code["git_dirty"] = bool(_git(source, "status", "--porcelain"))
    except (OSError, subprocess.SubprocessError):
        code["git_commit"] = None
        code["git_dirty"] = None
    return code


def _seed_database(source: Path, destination: Path, limit: int) -> str:
    if source.is_symlink() or not source.is_file() or source.stat().st_size > limit:
        raise ValueError("state_fixture 必须是大小有界的普通 SQLite 文件")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(destination)) as copied:
            original.backup(copied)
    if destination.stat().st_size > limit:
        raise ValueError("state_fixture 备份超过大小限制")
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def _run_failure(result: RunResult) -> str:
    reason = result.termination_reason or ""
    if result.status == "limit_reached":
        return "budget"
    if reason == "provider_error":
        return "provider"
    if reason in {"runtime_error", "runtime_timeout"}:
        return "runtime"
    return "task"


def _behavior_checks(
    case: EvalCase, result: RunResult, events: list[AgentEvent], disable_skills: bool
) -> list[CheckResult]:
    checks = [
        check(
            "run_status",
            result.status == case.expected_status,
            f"期望 {case.expected_status}，实际 {result.status}",
        )
    ]
    for text in case.final_contains:
        checks.append(check("final_contains", text in result.final_text, f"答案应包含: {text}"))
    for text in case.final_not_contains:
        checks.append(
            check("final_not_contains", text not in result.final_text, f"答案不应包含: {text}")
        )
    requested = [
        event.payload.get("name") for event in events if event.type == EventType.TOOL_REQUESTED
    ]
    skills = [
        event.payload.get("name") for event in events if event.type == EventType.SKILL_ACTIVATED
    ]
    for name in case.forbidden_tools:
        checks.append(check(f"forbidden_tool:{name}", name not in requested))
    if not disable_skills:
        for name in case.expected_skills:
            checks.append(check(f"skill:{name}", name in skills))
    if case.max_tool_calls is not None:
        checks.append(check("max_tool_calls", len(requested) <= case.max_tool_calls))
    if case.max_approval_requests is not None:
        count = sum(event.type == EventType.APPROVAL_REQUESTED for event in events)
        checks.append(check("max_approval_requests", count <= case.max_approval_requests))
    checks.extend(tool_checks(case, events))
    return checks


async def run_eval_case(
    case: EvalCase,
    *,
    base: Path,
    config_path: Path | None,
    disable_skills: bool = False,
    runtime_builder: Callable[..., Runtime] = build_runtime,
    artifacts_dir: Path | None = None,
    config_workspace: Path | None = None,
) -> EvalResult:
    """Run one case in a fresh fixture; a runtime completion alone cannot pass it."""
    started = monotonic()
    attempt_id = uuid4().hex
    source = case.workspace_path(base)
    artifacts_root = (artifacts_dir or Path(tempfile.gettempdir()) / "bot-evals").resolve()
    destination = artifacts_root / attempt_id
    events = MemoryEventSink()
    root_events: list[AgentEvent] = []
    redactor = Redactor()
    result: RunResult | None = None
    run_started = False
    checks: list[CheckResult] = []
    phase = "environment"
    failure_kind = None
    manifest: dict = {
        "attempt_id": attempt_id,
        "created_at": datetime.now(UTC).isoformat(),
        "verifier_version": VERIFIER_VERSION,
        "disable_skills": disable_skills,
        "case": case.model_dump(mode="json", by_alias=True),
        "task_sha256": digest(case.model_dump(mode="json", by_alias=True)),
    }
    try:
        destination.mkdir(parents=True, exist_ok=False)
        with tempfile.TemporaryDirectory(prefix="bot-eval-") as temporary:
            scratch = Path(temporary)
            workspace = scratch / "workspace"
            workspace.mkdir()
            phase = "verifier"
            effective_case = (
                case.model_copy(update={"expected_skills": []}) if disable_skills else case
            )
            if not has_assertions(effective_case):
                raise ValueError("case 必须包含独立验收条件，不能仅以 completed 判定通过")
            graders = []
            excluded = [artifacts_root]
            for spec in case.verifiers:
                if isinstance(spec, JsonVerifier):
                    validate_schema(spec.schema_)
                elif isinstance(spec, CommandVerifier):
                    tests_path = (base / spec.tests).resolve()
                    if tests_path == source or tests_path in source.parents:
                        raise ValueError("验收器目录不能等于 fixture 或包含 fixture")
                    excluded.append(tests_path)
                    tests = snapshot(tests_path, limit=case.max_snapshot_bytes, fixture=True)
                    if not any(entry.kind == "file" for entry in tests.values()):
                        raise ValueError("验收器目录不能为空")
                    phase = "environment"
                    image = await resolve_image(spec.image)
                    phase = "verifier"
                    graders.append((spec, tests, image))
            phase = "environment"
            settings_root = (config_workspace or source).resolve()
            config = load_config(settings_root, config_path=config_path)
            if config.model.name:
                key = resolve_model_api_key(config.model, workspace=settings_root)
                redactor = Redactor([key])
                config.model.api_key = SecretStr(key)
            for seed in (case.memory_fixture, case.state_fixture):
                if seed:
                    excluded.append((base / seed).resolve())
            before = snapshot(
                source,
                limit=case.max_snapshot_bytes,
                exclude=tuple(excluded),
                inputs=case.fixture_files,
                fixture=True,
            )
            materialize(before, workspace)
            _git(workspace, "init", "--quiet", "--template=")
            _git(workspace, "config", "core.hooksPath", os.devnull)
            _git(workspace, "add", "--all")
            _git(
                workspace,
                "-c",
                "user.name=Bot Eval",
                "-c",
                "user.email=eval@localhost",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--quiet",
                "--allow-empty",
                "-m",
                "eval fixture",
            )
            before = snapshot(workspace, limit=case.max_snapshot_bytes)
            original_skills = config.skill_path(settings_root)
            original_agents = config.project_agent_path(settings_root)
            resources = {}
            for name, origin, target in (
                ("skills", original_skills, workspace / ".bot/eval-skills"),
                ("project_agents", original_agents, workspace / ".bot/agents"),
            ):
                if origin.is_dir():
                    data = snapshot(
                        origin,
                        limit=case.max_snapshot_bytes,
                        exclude=tuple(excluded),
                        fixture=True,
                    )
                    materialize(data, target)
                    resources[name] = digest(inventory(data))
            config.storage.state_path = str(workspace / ".bot/state.db")
            config.memory.path = str(workspace / ".bot/memory")
            config.skills.path = str(workspace / ".bot/eval-skills")
            config.agents.user_path = str(workspace / ".bot/user-agents")
            config.agents.project_path = ".bot/agents"
            config.subagents.worktree_dir = ".bot/agent-worktrees"
            config.permissions.workspace_only = True
            if case.memory_fixture:
                memory = snapshot(
                    (base / case.memory_fixture).resolve(),
                    limit=case.max_snapshot_bytes,
                    fixture=True,
                )
                materialize(memory, workspace / ".bot/memory")
                resources["memory_seed"] = digest(inventory(memory))
            if case.state_fixture:
                resources["state_seed"] = _seed_database(
                    base / case.state_fixture, workspace / ".bot/state.db", case.max_snapshot_bytes
                )
            public_config = config.model_dump(mode="json", exclude={"model": {"api_key"}})
            endpoint = urlsplit(config.model.base_url)
            public_config["model"]["base_url"] = urlunsplit(
                (endpoint.scheme, endpoint.netloc.rsplit("@", 1)[-1], endpoint.path, "", "")
            )
            public_config = json.loads(
                json.dumps(public_config).replace(str(workspace), "<workspace>")
            )
            manifest.update(
                code=_code_manifest(),
                fixture_sha256=digest(inventory(before)),
                fixture=inventory(before),
                resources=resources,
                config=redactor.redact(public_config),
                config_sha256=digest(public_config),
                graders=[
                    {"tests_sha256": digest(inventory(tests)), "image": image}
                    for _, tests, image in graders
                ],
            )
            runtime = runtime_builder(
                workspace=workspace,
                config_path=Path(os.devnull),
                config_overrides=config.model_dump(mode="python"),
                event_sinks=[events],
                approval_handler=None,
            )
            phase = "runtime"
            try:
                if disable_skills:
                    runtime.catalog.skills.clear()
                    runtime.skills.reset()
                pool = getattr(runtime, "subagents", None)
                if pool is not None and config.subagents.enabled:
                    await pool.start()
                run_started = True
                result = await runtime.runner.run(
                    RunRequest(
                        prompt=case.prompt,
                        explicit_skills=[] if disable_skills else case.explicit_skills,
                    )
                )
            finally:
                close = getattr(runtime, "aclose", None)
                if close is not None:
                    await close()
                else:
                    runtime.close()
            phase = "verifier"
            roots = [
                event
                for event in events.events
                if event.type == EventType.RUN_STARTED and event.session_id == result.session_id
            ]
            if not roots:
                raise ValueError("缺少 root run.started，无法验证执行轨迹来源")
            run_id = roots[0].run_id
            manifest.update(session_id=result.session_id, run_id=run_id)
            root_events = [
                event
                for event in events.events
                if event.session_id == result.session_id and event.run_id == run_id
            ]
            checks.extend(_behavior_checks(case, result, root_events, disable_skills))
            after = snapshot(workspace, limit=case.max_snapshot_bytes)
            manifest.update(
                final_sha256=digest(inventory(after)),
                final_files=inventory(after),
                changes=changes(before, after),
            )
            checks.extend(file_checks(case, before, after))
            for index, (spec, tests, image) in enumerate(graders):
                checks.append(
                    await command_check(
                        spec,
                        image=image,
                        tests=tests,
                        artifacts=after,
                        root=scratch / f"grader-{index}",
                    )
                )
            diff = changes(before, after)
            sensitive = {
                name
                for name, entry in after.items()
                if any(secret.encode() in entry.content for secret in redactor.secrets)
            }
            manifest["omitted_sensitive_artifacts"] = sorted(sensitive & diff.keys())
            changed = {
                name: entry
                for name, entry in after.items()
                if entry.kind == "file" and name in diff and name not in sensitive
            }
            materialize(changed, destination / "artifacts")
    except Exception as exc:
        failure_kind = "provider" if isinstance(exc, ProviderError) else phase
        checks.append(
            CheckResult(name=phase, verdict="error", message=f"{type(exc).__name__}: {exc}")
        )

    if any(item.verdict == "error" for item in checks):
        verdict = "error"
        failure_kind = failure_kind or "verifier"
    elif any(item.verdict == "fail" for item in checks):
        verdict = "fail"
        failure_kind = (
            _run_failure(result) if result and result.status != case.expected_status else "task"
        )
    else:
        verdict = "pass"
    tool_names = [
        str(event.payload.get("name", ""))
        for event in root_events
        if event.type == EventType.TOOL_REQUESTED
    ]
    activated_skills = [
        str(event.payload.get("name", ""))
        for event in root_events
        if event.type == EventType.SKILL_ACTIVATED
    ]
    outcome = EvalResult(
        id=case.id,
        attempt_id=attempt_id,
        passed=verdict == "pass",
        verdict=verdict,
        run_status=result.status if result else "failed" if run_started else "not_started",
        status=result.status if result else "failed" if run_started else "not_started",
        failure_kind=failure_kind,
        checks=checks,
        manifest=manifest,
        artifact_dir=str(destination),
        failures=[
            f"{item.name}: {item.message or item.verdict}"
            for item in checks
            if item.verdict != "pass"
        ],
        duration_seconds=monotonic() - started,
        steps=result.steps if result else 0,
        tool_calls=len(tool_names),
        tool_names=tool_names,
        activated_skills=activated_skills,
        approval_requests=sum(event.type == EventType.APPROVAL_REQUESTED for event in root_events),
        input_tokens=result.input_tokens if result else 0,
        output_tokens=result.output_tokens if result else 0,
        cost_usd=result.cost_usd if result else None,
        final_text=result.final_text if result else "",
    )
    outcome = EvalResult.model_validate(redactor.redact(outcome.model_dump(mode="json")))
    if destination.is_dir():
        try:
            (destination / "events.jsonl").write_text(
                "".join(
                    json.dumps(redactor.redact(event.model_dump(mode="json")), ensure_ascii=False)
                    + "\n"
                    for event in events.events
                )
            )
            (destination / "manifest.json").write_text(
                json.dumps(outcome.manifest, ensure_ascii=False, indent=2) + "\n"
            )
            (destination / "result.json").write_text(outcome.model_dump_json(indent=2) + "\n")
        except OSError as exc:
            message = redactor.redact(f"验收记录写入失败: {exc}")
            outcome.passed = False
            outcome.verdict = "error"
            outcome.failure_kind = "environment"
            outcome.checks.append(CheckResult(name="artifacts", verdict="error", message=message))
            outcome.failures.append(message)
    return outcome


def write_eval_results(path: Path, results: list[EvalResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(result.model_dump_json() + "\n" for result in results)
    path.write_text(content, encoding="utf-8")
