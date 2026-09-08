"""Independent file/trace checks and container-only command grading."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from jsonschema.validators import validator_for

from bot.core.events import AgentEvent, EventType
from bot.evals.isolation import Snapshot, changes, fresh_file, materialize
from bot.evals.models import CheckResult, CommandVerifier, EvalCase, JsonVerifier, ToolExpectation

VERIFIER_VERSION = "isolated-eval-v1"


def check(name: str, passed: bool, message: str = "", **details: Any) -> CheckResult:
    return CheckResult(
        name=name, verdict="pass" if passed else "fail", message=message, details=details
    )


def has_assertions(case: EvalCase) -> bool:
    return bool(
        case.final_contains
        or case.final_not_contains
        or case.files_contain
        or case.required_changes
        or case.forbidden_changes
        or case.verifiers
        or case.expected_tools
        or case.tool_results
        or case.forbidden_tools
        or case.expected_skills
        or case.max_tool_calls is not None
        or case.max_approval_requests is not None
        or case.expected_status != "completed"
    )


def validate_schema(schema: dict) -> None:
    def local_references(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (
                    not isinstance(item, str) or not item.startswith("#")
                ):
                    raise ValueError("JSON Schema 仅允许文档内部引用，不能访问远程或本地文件")
                local_references(item)
        elif isinstance(value, list):
            for item in value:
                local_references(item)

    local_references(schema)
    validator_for(schema).check_schema(schema)


def parse_json(text: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"非法 JSON 数值: {value}")

    return json.loads(text, parse_constant=reject_constant)


def file_checks(case: EvalCase, before: Snapshot, after: Snapshot) -> list[CheckResult]:
    results: list[CheckResult] = []
    for name, parts in case.files_contain.items():
        entry = after.get(name)
        valid = entry is not None and entry.kind == "file"
        fresh = not case.files_contain_require_change or fresh_file(name, before, after)
        text = entry.content.decode("utf-8", errors="replace") if valid else ""
        results.append(
            check(
                f"file:{name}",
                valid and fresh and all(part in text for part in parts),
                "要求普通文件、指定文本及本次产生的内容变化",
                fresh=fresh,
            )
        )
    diff = changes(before, after)
    for pattern in case.required_changes:
        matches = [name for name in diff if fnmatch.fnmatchcase(name, pattern)]
        results.append(check(f"required_change:{pattern}", bool(matches), paths=matches))
    for pattern in case.forbidden_changes:
        matches = [name for name in diff if fnmatch.fnmatchcase(name, pattern)]
        results.append(check(f"forbidden_change:{pattern}", not matches, paths=matches))
    for spec in case.verifiers:
        if not isinstance(spec, JsonVerifier):
            continue
        entry = after.get(spec.path)
        if entry is None or entry.kind != "file":
            results.append(check(f"json:{spec.path}", False, "验收文件缺失或不是普通文件"))
            continue
        if spec.fresh and not fresh_file(spec.path, before, after):
            results.append(check(f"json:{spec.path}", False, "验收产物未发生内容变化"))
            continue
        try:
            data = parse_json(entry.content.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            results.append(check(f"json:{spec.path}", False, f"产物不是有效 JSON: {exc}"))
            continue
        try:
            validator = validator_for(spec.schema_)(spec.schema_)
            errors = [str(error.message) for error in validator.iter_errors(data)]
            results.append(check(f"json:{spec.path}", not errors, "; ".join(errors[:10])))
        except Exception as exc:
            results.append(CheckResult(name=f"json:{spec.path}", verdict="error", message=str(exc)))
    return results


def tool_checks(case: EvalCase, events: list[AgentEvent]) -> list[CheckResult]:
    requests: dict[str, AgentEvent] = {}
    results: dict[str, AgentEvent] = {}
    duplicate_ids: set[str] = set()
    # Only evaluator-captured events from the root run are supplied here.
    for event in events:
        call_id = event.payload.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        if event.type == EventType.TOOL_REQUESTED:
            if call_id in requests:
                duplicate_ids.add(call_id)
            requests[call_id] = event
        elif event.type in {EventType.TOOL_RESULT, EventType.TOOL_COMPLETED}:
            request = requests.get(call_id)
            if request is not None and request.payload.get("name") == event.payload.get("name"):
                # New uniform result events supersede legacy built-in completion events.
                if call_id not in results or event.type == EventType.TOOL_RESULT:
                    results[call_id] = event
    checks: list[CheckResult] = []
    if duplicate_ids:
        checks.append(
            CheckResult(name="tool_pairing", verdict="error", message="重复 Tool Call ID")
        )
    expectations = [ToolExpectation(name=name) for name in case.expected_tools] + case.tool_results
    for index, expected in enumerate(expectations):
        matched: list[str] = []
        for call_id, request in requests.items():
            if call_id in duplicate_ids or request.payload.get("name") != expected.name:
                continue
            arguments = request.payload.get("arguments", {})
            if not isinstance(arguments, dict) or any(
                arguments.get(key) != value for key, value in expected.arguments_contain.items()
            ):
                continue
            completion = results.get(call_id)
            if completion is None:
                continue
            payload = completion.payload
            # A managed process is complete only after a matching, later poll/input/kill result.
            if payload.get("status") == "running" and payload.get("process_id"):
                process_id = payload["process_id"]
                for later in events[events.index(completion) + 1 :]:
                    follow_id = later.payload.get("tool_call_id")
                    follow_request = requests.get(follow_id)
                    follow_args = (
                        follow_request.payload.get("arguments", {}) if follow_request else {}
                    )
                    if (
                        later.type == EventType.TOOL_RESULT
                        and later.payload.get("name")
                        in {"poll_process", "send_process_input", "terminate_process"}
                        and later.payload.get("process_id") == process_id
                        and isinstance(follow_args, dict)
                        and follow_args.get("process_id") == process_id
                        and later.payload.get("status") != "running"
                    ):
                        payload = later.payload
            if (
                payload.get("success") is not expected.success
                or payload.get("status") != expected.status
            ):
                continue
            if expected.returncode is not None and payload.get("returncode") != expected.returncode:
                continue
            # Successful process completion must include a successful OS exit status.
            if expected.success and expected.name in {
                "run_command",
                "run_shell",
                "poll_process",
                "send_process_input",
            }:
                if payload.get("returncode") != 0:
                    continue
            matched.append(call_id)
        checks.append(
            check(
                f"tool_result:{index}:{expected.name}",
                len(matched) >= expected.min_count,
                "检查配对的实际结果、状态、参数和退出码",
                matched_call_ids=matched,
            )
        )
    return checks


def docker_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "TMPDIR",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        }
    }
    # Docker resolves contexts via its config directory; HOME is deliberately
    # absent from the allowlist, so preserve that default explicitly.
    env.setdefault("DOCKER_CONFIG", str(Path.home() / ".docker"))
    return env


async def capture(argv: list[str], max_seconds: float) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=docker_env(),
    )
    chunks: list[bytes] = []
    size = 0

    async def drain() -> None:
        nonlocal size
        assert process.stdout is not None
        while chunk := await process.stdout.read(8192):
            if size < 256 * 1024:
                chunks.append(chunk[: 256 * 1024 - size])
            size += len(chunk)

    reader = asyncio.create_task(drain())
    try:
        await asyncio.wait_for(process.wait(), max_seconds)
        await asyncio.wait_for(reader, 5)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        if not reader.done():
            reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
    text = b"".join(chunks).decode("utf-8", errors="replace")
    if size > 256 * 1024:
        text += "\n[verifier output truncated]"
    return int(process.returncode), text


async def resolve_image(image: str) -> dict:
    code, output = await capture(["docker", "image", "inspect", image], 15)
    if code:
        raise ValueError(f"验收镜像不可用（不会自动拉取）: {output}")
    info = json.loads(output)[0]
    return {key: info.get(key) for key in ("Id", "RepoDigests", "Os", "Architecture")}


async def command_check(
    spec: CommandVerifier,
    *,
    image: dict,
    tests: Snapshot,
    artifacts: Snapshot,
    root: Path,
) -> CheckResult:
    # Called only after the Agent runtime, workers and managed processes are closed.
    # Each grader receives its own clean artifact copy; graders cannot affect one another.
    if any(entry.kind == "symlink" for entry in artifacts.values()):
        return CheckResult(
            name=f"command:{spec.tests}",
            verdict="error",
            message="命令验收暂不支持含符号链接的产物，不能对省略链接后的副本评分",
        )
    materialize(tests, root / "tests")
    materialize(artifacts, root / "workspace")
    output_dir = root / "output"
    output_dir.mkdir()
    container = f"bot-verifier-{uuid4().hex}"
    argv = [
        "docker",
        "run",
        "--name",
        container,
        "--pull=never",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=128",
        "--memory=1g",
        "--cpus=1",
        "--tmpfs",
        "/tmp:rw,nosuid,size=128m",
        "--mount",
        f"type=bind,src={root / 'tests'},dst=/verifier,readonly",
        "--mount",
        f"type=bind,src={root / 'workspace'},dst=/workspace",
        "--mount",
        f"type=bind,src={output_dir},dst=/verifier-output",
        "--workdir=/workspace",
        "--env",
        "BOT_EVAL_RESULT=/verifier-output/result.json",
        "--entrypoint",
        spec.argv[0],
        str(image["Id"]),
        *spec.argv[1:],
    ]
    details: dict[str, Any] = {"image": image, "argv": spec.argv}
    result = CheckResult(name=f"command:{spec.tests}", verdict="error")
    try:
        code, output = await capture(argv, spec.timeout_seconds)
        details.update(returncode=code, output=output)
        path = output_dir / "result.json"
        if code in {125, 126, 127}:
            raise ValueError("Docker 或验收命令无法启动")
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            raise ValueError("验收器未产生有效的 result.json（普通文件且不超过 64 KiB）")
        report = parse_json(path.read_text())
        if not isinstance(report, dict) or type(report.get("passed")) is not bool:
            raise ValueError("验收结果必须包含布尔 passed 字段")
        if report["passed"] and code != 0:
            raise ValueError("验收器声称通过但进程退出码非零")
        result.verdict = "pass" if report["passed"] else "fail"
        result.message = str(report.get("message", ""))
    except Exception as exc:
        result.message = f"验收器异常: {type(exc).__name__}: {exc}"
    finally:
        try:
            code, cleanup_output = await capture(["docker", "rm", "--force", container], 15)
            if code and "No such container" not in cleanup_output:
                result.verdict = "error"
                result.message += f"；验收容器清理失败: {cleanup_output}"
        except Exception as exc:
            result.verdict = "error"
            result.message += f"；验收容器清理失败: {exc}"
    result.details = details
    return result
