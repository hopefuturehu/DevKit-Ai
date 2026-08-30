from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_JOBS_DIR = Path("artifacts/terminalbench/jobs")
DEFAULT_REPORTS_DIR = Path("artifacts/terminalbench/reports")
_SENSITIVE_ENV_RE = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|AUTH)", re.IGNORECASE)
_ENV_TEMPLATE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} 必须包含 JSON object")
    return value


def find_latest_job(jobs_dir: Path) -> Path:
    jobs_dir = jobs_dir.expanduser().resolve()
    candidates = (
        [path for path in jobs_dir.iterdir() if path.is_dir() and (path / "result.json").is_file()]
        if jobs_dir.is_dir()
        else []
    )
    if not candidates:
        raise FileNotFoundError(f"未在 {jobs_dir} 找到 Harbor job")
    return max(candidates, key=lambda path: (path / "result.json").stat().st_mtime_ns)


def _agent_entries(config: dict[str, Any]) -> list[dict[str, Any]]:
    entries = config.get("agents")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _secret_values(config: dict[str, Any]) -> tuple[dict[str, bytes], list[str]]:
    resolved: dict[str, bytes] = {}
    unresolved: set[str] = set()
    for agent in _agent_entries(config):
        env = agent.get("env")
        if not isinstance(env, dict):
            continue
        for key, raw_value in env.items():
            if not isinstance(key, str) or not _SENSITIVE_ENV_RE.search(key):
                continue
            variable = key
            if isinstance(raw_value, str):
                match = _ENV_TEMPLATE_RE.fullmatch(raw_value)
                if match:
                    variable = match.group(1)
                    value = os.environ.get(variable)
                else:
                    value = raw_value
            else:
                value = os.environ.get(variable)
            if value and len(value) >= 8:
                resolved[variable] = value.encode()
            else:
                unresolved.add(variable)
    return resolved, sorted(unresolved)


def _file_contains(path: Path, needles: list[bytes]) -> bool:
    if not needles:
        return False
    overlap = max(len(needle) for needle in needles) - 1
    carry = b""
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            data = carry + chunk
            if any(needle in data for needle in needles):
                return True
            carry = data[-overlap:] if overlap > 0 else b""
    return False


def _scan_secret_hits(
    job_dir: Path,
    extra_inputs: list[Path],
    secrets: dict[str, bytes],
) -> dict[str, list[str]]:
    hits: dict[str, list[str]] = {name: [] for name in secrets}
    candidates = [path for path in job_dir.rglob("*") if path.is_file() and not path.is_symlink()]
    candidates.extend(path for path in extra_inputs if path.is_file() and not path.is_symlink())
    for path in candidates:
        for name, value in secrets.items():
            if _file_contains(path, [value]):
                try:
                    display = str(path.relative_to(job_dir))
                except ValueError:
                    display = str(path)
                hits[name].append(display)
    return {name: paths for name, paths in hits.items() if paths}


def _input_paths(config: dict[str, Any]) -> list[tuple[str, Path]]:
    result: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for index, agent in enumerate(_agent_entries(config), 1):
        kwargs = agent.get("kwargs")
        if not isinstance(kwargs, dict):
            continue
        for key, label in (("package_path", "agent-wheel"), ("config_path", "bot-config")):
            raw_path = kwargs.get(key)
            if not isinstance(raw_path, str):
                continue
            path = Path(raw_path).expanduser().resolve()
            if path in seen:
                continue
            seen.add(path)
            suffix = path.suffix
            result.append((f"{label}-{index}{suffix}", path))
    return result


def _copy_job(job_dir: Path, destination: Path, *, include_artifacts: bool) -> None:
    for source in job_dir.rglob("*"):
        if not source.is_file() or source.is_symlink():
            continue
        relative = source.relative_to(job_dir)
        if (
            not include_artifacts
            and "artifacts" in relative.parts[1:]
            and source.name != "manifest.json"
        ):
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _copy_inputs(inputs: list[tuple[str, Path]], destination: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for name, source in inputs:
        record: dict[str, Any] = {
            "name": name,
            "source": str(source),
            "available": source.is_file(),
        }
        if source.is_file():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            record["archive_path"] = str(target.relative_to(destination.parent))
        records.append(record)
    return records


def _duration_seconds(start: Any, finish: Any) -> float | None:
    if not isinstance(start, str) or not isinstance(finish, str):
        return None
    try:
        return (datetime.fromisoformat(finish) - datetime.fromisoformat(start)).total_seconds()
    except ValueError:
        return None


def _event_metrics(trial_dir: Path) -> dict[str, Any]:
    candidates = (
        trial_dir / "agent" / "events.jsonl",
        trial_dir / "agent" / "trace" / "events.jsonl",
    )
    events_path = next((path for path in candidates if path.is_file()), None)
    metrics: dict[str, Any] = {
        "events_available": events_path is not None,
        "completed": 0,
        "failed": 0,
        "skipped": 0,
        "blocked": 0,
        "requests_started": 0,
        "requests_completed": 0,
        "requests_failed": 0,
        "request_input_tokens": 0,
        "request_output_tokens": 0,
        "request_cost_usd": 0.0,
        "model_steps": 0,
        "tool_completed": 0,
        "max_agent_prompt_tokens": None,
        "post_last_compaction_steps": None,
    }
    if events_path is None:
        return metrics

    event_names = {
        "context.compaction.completed": "completed",
        "context.compaction.failed": "failed",
        "context.compaction.skipped": "skipped",
        "context.compaction.blocked": "blocked",
        "context.compaction.request.started": "requests_started",
        "context.compaction.request.completed": "requests_completed",
        "context.compaction.request.failed": "requests_failed",
    }
    last_compaction_sequence: int | None = None
    model_steps: list[tuple[int, int]] = []
    max_prompt_tokens = 0
    try:
        with events_path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                event_type = event.get("type")
                payload = event.get("payload")
                payload = payload if isinstance(payload, dict) else {}
                sequence = int(event.get("sequence") or 0)
                metric = event_names.get(event_type)
                if metric is not None:
                    metrics[metric] += 1
                if event_type == "context.compaction.completed":
                    last_compaction_sequence = sequence
                elif event_type == "context.compaction.request.completed":
                    metrics["request_input_tokens"] += int(payload.get("input_tokens") or 0)
                    metrics["request_output_tokens"] += int(payload.get("output_tokens") or 0)
                    metrics["request_cost_usd"] += float(payload.get("cost_usd") or 0)
                elif event_type == "model.response":
                    step = payload.get("step")
                    if isinstance(step, int):
                        model_steps.append((sequence, step))
                elif event_type == "tool.completed":
                    metrics["tool_completed"] += 1
                elif event_type == "model.usage":
                    usage = payload.get("turn_usage")
                    usage = usage if isinstance(usage, dict) else {}
                    prompt_tokens = int(usage.get("prompt_tokens") or 0)
                    max_prompt_tokens = max(max_prompt_tokens, prompt_tokens)
    except OSError:
        metrics["events_available"] = False
        return metrics

    metrics["request_cost_usd"] = round(metrics["request_cost_usd"], 12)
    metrics["model_steps"] = len({step for _, step in model_steps})
    metrics["max_agent_prompt_tokens"] = max_prompt_tokens or None
    if last_compaction_sequence is not None:
        metrics["post_last_compaction_steps"] = len(
            {step for sequence, step in model_steps if sequence > last_compaction_sequence}
        )
    return metrics


def _trial_summary(trial_dir: Path) -> dict[str, Any]:
    result = _load_json(trial_dir / "result.json")
    agent_result = result.get("agent_result")
    agent_result = agent_result if isinstance(agent_result, dict) else {}
    agent_metadata = agent_result.get("metadata")
    agent_metadata = agent_metadata if isinstance(agent_metadata, dict) else {}
    verifier_result = result.get("verifier_result")
    verifier_result = verifier_result if isinstance(verifier_result, dict) else {}
    rewards = verifier_result.get("rewards")
    rewards = rewards if isinstance(rewards, dict) else {}
    reward = rewards.get("reward")
    if reward is None and len(rewards) == 1:
        reward = next(iter(rewards.values()))
    exception = result.get("exception_info")
    exception = exception if isinstance(exception, dict) else None
    agent_status = agent_metadata.get("status")

    if exception is not None:
        classification = "exception"
    elif agent_status == "failed":
        classification = "agent_failed"
    elif isinstance(reward, (int, float)) and reward > 0:
        classification = "passed"
    elif isinstance(reward, (int, float)):
        classification = "failed_verifier"
    else:
        classification = "unscored"

    trace_path = trial_dir / "agent" / "trace" / "manifest.json"
    trace = _load_json(trace_path) if trace_path.is_file() else {}
    context_metrics = _event_metrics(trial_dir)
    steps = agent_metadata.get("steps")
    if steps is None and context_metrics["events_available"]:
        steps = context_metrics["model_steps"] or None
    tool_count = trace.get("tool_count")
    if tool_count is None and context_metrics["events_available"]:
        tool_count = context_metrics["tool_completed"]
    timing: dict[str, float | None] = {}
    for key in ("environment_setup", "agent_setup", "agent_execution", "verifier"):
        record = result.get(key)
        record = record if isinstance(record, dict) else {}
        timing[key] = _duration_seconds(record.get("started_at"), record.get("finished_at"))

    return {
        "trial_name": result.get("trial_name") or trial_dir.name,
        "task_name": result.get("task_name"),
        "classification": classification,
        "reward": reward,
        "agent_status": agent_status,
        "agent_error": agent_metadata.get("error"),
        "steps": steps,
        "input_tokens": agent_result.get("n_input_tokens"),
        "output_tokens": agent_result.get("n_output_tokens"),
        "cost_usd": agent_result.get("cost_usd"),
        "exception_type": exception.get("exception_type") if exception else None,
        "exception_message": exception.get("exception_message") if exception else None,
        "timing_seconds": timing,
        "event_count": trace.get("event_count"),
        "tool_count": tool_count,
        "reasoning_count": trace.get("reasoning_count"),
        "trace_available": bool(trace),
        "context": context_metrics,
        "result_path": f"snapshot/{trial_dir.name}/result.json",
        "transcript_path": (
            f"snapshot/{trial_dir.name}/agent/trace/transcript.md"
            if (trial_dir / "agent" / "trace" / "transcript.md").is_file()
            else None
        ),
    }


def _inventory(
    root: Path,
    *,
    relative_to: Path | None = None,
) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    total_bytes = 0
    relative_to = relative_to or root.parent
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        total_bytes += size
        records.append(
            {
                "path": str(path.relative_to(relative_to)),
                "size_bytes": size,
                "sha256": digest.hexdigest(),
            }
        )
    return records, total_bytes


def _markdown(summary: dict[str, Any]) -> str:
    aggregate = summary["aggregate"]
    job = summary["job"]
    security = summary["security"]
    mean_reward = aggregate["mean_reward"] if aggregate["mean_reward"] is not None else "-"
    compacted_pass_rate = (
        aggregate["compacted_pass_rate"] if aggregate["compacted_pass_rate"] is not None else "-"
    )
    lines = [
        "# Terminal-Bench 结果归档",
        "",
        f"- 归档时间：`{summary['created_at']}`",
        f"- 来源 Job：`{summary['source_job_name']}`",
        f"- Harbor Job：total={job['total']}，completed={job['completed']}，"
        f"running={job['running']}，pending={job['pending']}，"
        f"cancelled={job['cancelled']}，errored={job['errored']}",
        f"- 已产生结果的 Trial：{aggregate['trials']}",
        f"- Passed：{aggregate['passed']}",
        f"- Agent failed：{aggregate['agent_failed']}",
        f"- Verifier failed：{aggregate['failed_verifier']}",
        f"- Exception：{aggregate['exception']}",
        f"- Unscored：{aggregate['unscored']}",
        f"- 平均 Reward：{mean_reward}",
        f"- 任务完成率：{aggregate['pass_rate'] if aggregate['pass_rate'] is not None else '-'}",
        f"- 发生压缩的 Trial：{aggregate['compacted_trials']}；其中通过："
        f"{aggregate['compacted_passed']}；完成率："
        f"{compacted_pass_rate}",
        f"- 压缩完成/失败：{aggregate['compactions_completed']}/{aggregate['compactions_failed']}",
        f"- 总费用：{aggregate['cost_usd'] if aggregate['cost_usd'] is not None else '-'}",
        f"- 敏感值明文命中：{security['secret_hit_count']}",
        "",
        "## Trials",
        "",
        "| Task | 分类 | Reward | Steps | Tools | 压缩完成/失败 | Max prompt | "
        "Tokens in/out | Cost | Trace |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for trial in summary["trials"]:
        task = str(trial.get("task_name") or trial["trial_name"]).replace("|", "\\|")
        trace = f"[transcript]({trial['transcript_path']})" if trial.get("transcript_path") else "-"
        lines.append(
            "| "
            + " | ".join(
                [
                    task,
                    str(trial["classification"]),
                    str(trial["reward"] if trial["reward"] is not None else "-"),
                    str(trial["steps"] if trial["steps"] is not None else "-"),
                    str(trial["tool_count"] if trial["tool_count"] is not None else "-"),
                    f"{trial['context']['completed']}/{trial['context']['failed']}",
                    str(trial["context"]["max_agent_prompt_tokens"] or "-"),
                    f"{trial['input_tokens'] or 0}/{trial['output_tokens'] or 0}",
                    str(trial["cost_usd"] if trial["cost_usd"] is not None else "-"),
                    trace,
                ]
            )
            + " |"
        )

    issues: list[str] = []
    if job["finished_at"] is None or job["completed"] < job["total"]:
        issues.append(
            "- Harbor Job 未完整结束："
            f"total={job['total']}，completed={job['completed']}，"
            f"running={job['running']}，pending={job['pending']}，"
            f"cancelled={job['cancelled']}，errored={job['errored']}。"
        )
    for trial in summary["trials"]:
        if trial["classification"] == "exception":
            issues.append(
                f"- `{trial['trial_name']}` exception："
                f"`{trial.get('exception_type') or 'unknown'}` — "
                f"{trial.get('exception_message') or ''}"
            )
        elif trial["classification"] == "agent_failed":
            issues.append(
                f"- `{trial['trial_name']}` Agent 内部失败（steps={trial.get('steps') or 0}）："
                f"{trial.get('agent_error') or '未记录错误'}"
            )
        elif trial["classification"] == "failed_verifier":
            issues.append(
                f"- `{trial['trial_name']}` Agent 已运行但 verifier reward=`{trial['reward']}`。"
            )
        elif trial["classification"] == "unscored":
            issues.append(f"- `{trial['trial_name']}` 未产生 verifier reward。")
        if not trial["trace_available"]:
            issues.append(f"- `{trial['trial_name']}` 缺少 Agent Trace。")
    if security["unresolved_secret_env_vars"]:
        unresolved = ", ".join(f"`{name}`" for name in security["unresolved_secret_env_vars"])
        issues.append(f"- 归档时无法解析以下敏感环境变量，未能扫描其明文：{unresolved}。")

    lines.extend(["", "## 检测提示", "", *(issues or ["- 未发现结构化异常。"])])
    lines.extend(
        [
            "",
            "## 内容",
            "",
            "- `snapshot/`：Harbor job、trial、Agent trace 与 verifier 日志快照。",
            "- `inputs/`：评测使用的 Agent wheel 与 Bot 配置（如仍可读取）。",
            "- `summary.json`：机器可读汇总。",
            "- `manifest.json`：归档文件大小和 SHA-256。",
            "",
        ]
    )
    return "\n".join(lines)


def archive_job(
    job_dir: Path,
    *,
    output_root: Path,
    include_artifacts: bool = False,
    now: datetime | None = None,
) -> Path:
    job_dir = job_dir.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    if not job_dir.is_dir() or not (job_dir / "result.json").is_file():
        raise FileNotFoundError(f"Harbor job 无效: {job_dir}")
    try:
        output_root.relative_to(job_dir)
    except ValueError:
        pass
    else:
        raise ValueError("归档目录不能位于来源 job 内部")

    config = _load_json(job_dir / "config.json")
    inputs = _input_paths(config)
    secrets, unresolved = _secret_values(config)
    secret_hits = _scan_secret_hits(
        job_dir,
        [path for _, path in inputs],
        secrets,
    )
    if secret_hits:
        details = "; ".join(f"{name}: {', '.join(paths)}" for name, paths in secret_hits.items())
        raise RuntimeError(f"拒绝归档：检测到敏感环境变量明文（{details}）")

    created_at = now or datetime.now().astimezone()
    if created_at.tzinfo is None:
        created_at = created_at.astimezone()
    timestamp = created_at.strftime("%Y%m%d-%H%M%S%z")
    output_root.mkdir(parents=True, exist_ok=True)
    destination = output_root / timestamp
    suffix = 1
    while destination.exists():
        destination = output_root / f"{timestamp}-{suffix:02d}"
        suffix += 1

    temporary = Path(tempfile.mkdtemp(prefix=f".{timestamp}-", dir=output_root))
    try:
        snapshot = temporary / "snapshot"
        _copy_job(job_dir, snapshot, include_artifacts=include_artifacts)
        input_records = _copy_inputs(inputs, temporary / "inputs")

        trial_dirs = sorted(
            path for path in job_dir.iterdir() if path.is_dir() and (path / "result.json").is_file()
        )
        trials = [_trial_summary(path) for path in trial_dirs]
        classifications = Counter(trial["classification"] for trial in trials)
        compacted_trials = [trial for trial in trials if trial["context"]["completed"] > 0]
        compacted_passed = sum(trial["classification"] == "passed" for trial in compacted_trials)
        rewards = [
            float(trial["reward"]) for trial in trials if isinstance(trial["reward"], (int, float))
        ]
        costs = [
            float(trial["cost_usd"])
            for trial in trials
            if isinstance(trial["cost_usd"], (int, float))
        ]
        _, total_bytes = _inventory(snapshot)
        _, input_bytes = _inventory(temporary / "inputs")

        job_result = _load_json(job_dir / "result.json")
        raw_job_stats = job_result.get("stats")
        raw_job_stats = raw_job_stats if isinstance(raw_job_stats, dict) else {}
        job_status = {
            "started_at": job_result.get("started_at"),
            "updated_at": job_result.get("updated_at"),
            "finished_at": job_result.get("finished_at"),
            "total": int(job_result.get("n_total_trials") or len(trials)),
            "completed": int(raw_job_stats.get("n_completed_trials") or 0),
            "errored": int(raw_job_stats.get("n_errored_trials") or 0),
            "running": int(raw_job_stats.get("n_running_trials") or 0),
            "pending": int(raw_job_stats.get("n_pending_trials") or 0),
            "cancelled": int(raw_job_stats.get("n_cancelled_trials") or 0),
            "retries": int(raw_job_stats.get("n_retries") or 0),
        }

        summary = {
            "schema_version": 1,
            "created_at": created_at.isoformat(),
            "source_job": str(job_dir),
            "source_job_name": job_dir.name,
            "include_task_artifacts": include_artifacts,
            "job": job_status,
            "aggregate": {
                "trials": len(trials),
                "passed": classifications["passed"],
                "agent_failed": classifications["agent_failed"],
                "failed_verifier": classifications["failed_verifier"],
                "exception": classifications["exception"],
                "unscored": classifications["unscored"],
                "pass_rate": classifications["passed"] / len(trials) if trials else None,
                "compacted_trials": len(compacted_trials),
                "compacted_passed": compacted_passed,
                "compacted_pass_rate": (
                    compacted_passed / len(compacted_trials) if compacted_trials else None
                ),
                "compactions_completed": sum(trial["context"]["completed"] for trial in trials),
                "compactions_failed": sum(trial["context"]["failed"] for trial in trials),
                "compaction_requests": sum(
                    trial["context"]["requests_started"] for trial in trials
                ),
                "compaction_input_tokens": sum(
                    trial["context"]["request_input_tokens"] for trial in trials
                ),
                "compaction_output_tokens": sum(
                    trial["context"]["request_output_tokens"] for trial in trials
                ),
                "compaction_cost_usd": sum(
                    trial["context"]["request_cost_usd"] for trial in trials
                ),
                "mean_reward": sum(rewards) / len(rewards) if rewards else None,
                "input_tokens": sum(int(trial["input_tokens"] or 0) for trial in trials),
                "output_tokens": sum(int(trial["output_tokens"] or 0) for trial in trials),
                "cost_usd": sum(costs) if costs else None,
                "archive_bytes": total_bytes + input_bytes,
            },
            "security": {
                "secret_env_vars_checked": sorted(secrets),
                "unresolved_secret_env_vars": unresolved,
                "secret_hit_count": 0,
            },
            "inputs": input_records,
            "job_result": job_result,
            "trials": trials,
        }
        (temporary / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (temporary / "SUMMARY.md").write_text(_markdown(summary), encoding="utf-8")
        inventory, _ = _inventory(temporary, relative_to=temporary)
        manifest = {
            "schema_version": 1,
            "created_at": created_at.isoformat(),
            "source_job": str(job_dir),
            "files": inventory,
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.rename(destination)
    except Exception:
        shutil.rmtree(temporary)
        raise
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Archive and summarize one Terminal-Bench Harbor job"
    )
    parser.add_argument("--job", type=Path, help="Harbor job 目录；默认选择最新 job")
    parser.add_argument("--jobs-dir", type=Path, default=DEFAULT_JOBS_DIR)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_REPORTS_DIR)
    parser.add_argument(
        "--include-artifacts",
        action="store_true",
        help="同时复制 task artifacts；可能显著增大归档",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    job_dir = args.job.expanduser().resolve() if args.job else find_latest_job(args.jobs_dir)
    destination = archive_job(
        job_dir,
        output_root=args.output_root,
        include_artifacts=args.include_artifacts,
    )
    summary = _load_json(destination / "summary.json")
    aggregate = summary["aggregate"]
    print(f"归档完成: {destination}")
    print(f"汇总报告: {destination / 'SUMMARY.md'}")
    print(
        "Trials="
        f"{aggregate['trials']} "
        f"passed={aggregate['passed']} "
        f"agent_failed={aggregate['agent_failed']} "
        f"failed={aggregate['failed_verifier']} "
        f"exceptions={aggregate['exception']} "
        f"unscored={aggregate['unscored']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
