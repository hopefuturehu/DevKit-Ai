#!/usr/bin/env python3
"""Run the frozen 20+20 suite with official controls and resumable per-phase evidence."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from bot.config import load_config, resolve_model_api_key
from bot.evals.terminalbench import _harbor_subprocess_env


def now() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


class Batch:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.repo = Path(__file__).resolve().parents[1]
        self.root = args.run_root.resolve()
        self.source = self.root / "source"
        self.config = self.root / "config.toml"
        self.wheel = self.root / "packages/kunpeng_cli_agent-0.1.0-py3-none-any.whl"
        self.suite = read_json(args.suite)
        self.run_manifest = read_json(self.root / "run-manifest.json")
        self.env = _harbor_subprocess_env()
        credential_config = load_config(self.repo, config_path=self.repo / ".bot/config.toml")
        self.env["BOT_MODEL_API_KEY"] = resolve_model_api_key(
            credential_config.model, workspace=self.repo
        )
        self.env["DOCKER_CONTEXT"] = "desktop-linux"
        self.env["DOCKER_HOST"] = "unix://" + str(Path.home() / ".docker/run/docker.sock")
        self.env["PYTHONPATH"] = str(self.source / "src")
        configured = load_config(self.repo, config_path=self.config)
        if configured.model.name != "deepseek-v4-flash":
            raise ValueError("This frozen run is authorized for deepseek-v4-flash")
        self.model_host = "api.deepseek.com"
        self.retained = set(self.run_manifest["retained_image_ids"])
        self.swe_rows = {
            row["instance_id"]: row
            for row in json.loads((self.root / "evaluator/swe-dataset.json").read_text())
        }

    def command(self, folder: Path, phase: str, argv: list[str], timeout: float) -> dict:
        state = folder / f"{phase}.phase.json"
        if state.exists() and read_json(state).get("finished_at"):
            return read_json(state)
        if state.exists():
            previous = read_json(state)
            pid = previous.get("pid")
            if pid:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    raise RuntimeError(
                        f"Phase {phase} still has a live PID {pid}; inspect it first"
                    )
            raise RuntimeError(
                f"Interrupted phase {phase}; preserve evidence and use a new attempt"
            )
        folder.mkdir(parents=True, exist_ok=True)
        record = {"phase": phase, "argv": argv, "started_at": now(), "timeout": timeout}
        with (folder / f"{phase}.log").open("w") as log:
            process = subprocess.Popen(
                argv,
                cwd=self.root,
                env=self.env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            record["pid"] = process.pid
            write_json(state, record)
            write_json(self.root / "live.json", {"folder": str(folder), **record})
            print(now(), folder.name, phase, "pid", process.pid, flush=True)
            try:
                record["returncode"] = process.wait(timeout=timeout)
            except BaseException as exc:
                record["interruption"] = type(exc).__name__
                try:
                    os.killpg(process.pid, signal.SIGINT)
                    process.wait(timeout=45)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                record["returncode"] = process.returncode
                if not isinstance(exc, subprocess.TimeoutExpired):
                    raise
            finally:
                record["finished_at"] = now()
                write_json(state, record)
        return record

    def inspect_image(self, tag: str) -> dict | None:
        result = subprocess.run(
            ["docker", "image", "inspect", tag, "--format", "{{json .}}"],
            env=self.env,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            return None
        data = json.loads(result.stdout)
        return {key: data[key] for key in ("Id", "RepoTags", "RepoDigests", "Architecture", "Size")}

    def image(self, case: dict, folder: Path) -> dict:
        previous = folder / "image.json"
        image = self.inspect_image(case["image"])
        if image is None:
            self.command(
                folder,
                "image-pull",
                ["docker", "pull", "--platform", "linux/amd64", case["image"]],
                3600,
            )
            image = self.inspect_image(case["image"])
        if image is None:
            raise RuntimeError("Task image unavailable; see image-pull.log")
        if previous.exists() and read_json(previous)["Id"] != image["Id"]:
            raise RuntimeError("Image tag changed within an attempt")
        write_json(previous, image)
        return image

    def terminal_phase(self, case: dict, folder: Path, phase: str) -> dict:
        name = case["id"].split("/")[-1]
        task = self.args.terminal_cache / name / case["package_hash"]
        actual_hash = hashlib.sha256((task / "task.toml").read_bytes()).hexdigest()
        if actual_hash != case["task_toml_sha256"]:
            raise RuntimeError("Cached task metadata differs from the frozen suite")
        job_name = f"{name}-{phase}-{self.args.attempt}"
        jobs = self.root / "terminalbench"
        job = jobs / job_name
        # A completed official result is authoritative, including the manually launched smoke.
        finished = list(job.glob("*/result.json"))
        if len(finished) == 1 and read_json(finished[0]).get("finished_at"):
            return read_json(finished[0])
        argv = [str(self.args.harbor), "run", "--path", str(task), "--agent"]
        if phase in ("oracle", "nop"):
            argv += [phase]
        else:
            argv += [
                "bot.evals.harbor_agent:KunpengBot",
                "--model",
                "openai-compatible/deepseek-v4-flash",
                "--agent-kwarg",
                f"package_path={self.wheel}",
                "--agent-kwarg",
                f"config_path={self.config}",
                "--agent-kwarg",
                "max_steps=240",
                "--agent-kwarg",
                f"max_wall_time_seconds={max(7200, case['agent_timeout_sec'])}",
                "--agent-kwarg",
                "subagents_enabled=false",
                "--agent-env",
                "BOT_MODEL_API_KEY=${BOT_MODEL_API_KEY}",
                "--agent-env",
                "BOT_MODEL_API_KEY_REF=env:BOT_MODEL_API_KEY",
                "--allow-agent-host",
                self.model_host,
                "--agent-setup-timeout-multiplier",
                "2",
            ]
        argv += [
            "--n-concurrent",
            "1",
            "--n-attempts",
            "1",
            "--jobs-dir",
            str(jobs),
            "--job-name",
            job_name,
            "--quiet",
        ]
        self.command(
            folder, phase, argv, case["agent_timeout_sec"] + case["verifier_timeout_sec"] + 2400
        )
        results = list(job.glob("*/result.json"))
        if len(results) != 1:
            raise RuntimeError(f"Expected exactly one official trial result for {job_name}")
        return read_json(results[0])

    def swe_grade(self, case: dict, folder: Path, phase: str, prediction: Path) -> dict:
        run_id = f"flash40-{case['id']}-{phase}-{self.args.attempt}"
        report_root = self.root / "logs/run_evaluation" / run_id
        argv = [
            str(self.args.swe_python),
            "-m",
            "swebench.harness.run_evaluation",
            "--dataset_name",
            str(self.root / "evaluator/swe-dataset.json"),
            "--predictions_path",
            str(prediction),
            "--instance_ids",
            case["id"],
            "--max_workers",
            "1",
            "--timeout",
            "1800",
            "--namespace",
            "swebench",
            "--cache_level",
            "instance",
            "--clean",
            "False",
            "--run_id",
            run_id,
        ]
        self.command(folder, phase, argv, 2100)
        reports = list(report_root.glob("*/*/report.json"))
        prediction_row = json.loads(prediction.read_text())
        if not reports and not prediction_row.get("model_patch", "").strip():
            summary = self.root / f"{prediction_row['model_name_or_path']}.{run_id}.json"
            if summary.exists() and read_json(summary).get("empty_patch_instances") == 1:
                return {"resolved": False, "empty_patch": True, "summary": read_json(summary)}
        if len(reports) != 1:
            raise RuntimeError(f"Missing official SWE report for {run_id}")
        return read_json(reports[0])[case["id"]]

    def run_case(self, kind: str, case: dict) -> None:
        name = case["id"].split("/")[-1]
        folder = self.root / "cases" / kind / name / self.args.attempt
        result_file = folder / "result.json"
        if result_file.exists():
            print("Existing result, preserve:", case["id"], flush=True)
            return
        folder.mkdir(parents=True, exist_ok=True)
        record = {
            "id": case["id"],
            "kind": kind,
            "attempt": self.args.attempt,
            "source_commit": self.run_manifest["source_commit"],
            "started_at": now(),
        }
        image = None
        try:
            image = self.image(case, folder)
            record["image_id"] = image["Id"]
            if kind == "terminalbench":
                oracle = self.terminal_phase(case, folder, "oracle")
                nop = self.terminal_phase(case, folder, "nop")
                record["controls"] = {"oracle": oracle, "nop": nop}
                for control, expected in [(oracle, 1), (nop, 0)]:
                    reward = (control.get("verifier_result") or {}).get("rewards", {}).get("reward")
                    if control.get("exception_info") or reward != expected:
                        raise RuntimeError("Official control did not produce its expected verdict")
                baseline = self.terminal_phase(case, folder, "baseline")
                record["official_result"] = baseline
                reward = (baseline.get("verifier_result") or {}).get("rewards", {}).get("reward")
                record["verdict"] = (
                    "error"
                    if baseline.get("exception_info") or reward is None
                    else "pass"
                    if reward == 1
                    else "fail"
                )
            else:
                row = self.swe_rows[case["id"]]
                private = self.root / "evaluator" / case["id"] / self.args.attempt
                private.mkdir(parents=True, exist_ok=True)
                controls = {}
                wrong_patch = (
                    "diff --git a/__benchmark_negative_control__.txt "
                    "b/__benchmark_negative_control__.txt\n"
                    "new file mode 100644\n--- /dev/null\n"
                    "+++ b/__benchmark_negative_control__.txt\n"
                    "@@ -0,0 +1 @@\n+This control does not fix the issue.\n"
                )
                for phase, patch in [("gold", row["patch"]), ("negative", wrong_patch)]:
                    prediction = private / f"{phase}.jsonl"
                    write_json(
                        prediction,
                        {
                            "instance_id": case["id"],
                            "model_name_or_path": "control-" + phase,
                            "model_patch": patch,
                        },
                    )
                    # Official JSONL parser requires one JSON object per physical line.
                    prediction.write_text(json.dumps(read_json(prediction)) + "\n")
                    prediction.chmod(0o600)
                    controls[phase] = self.swe_grade(case, folder, phase, prediction)
                record["controls"] = controls
                if (
                    not controls["gold"]["resolved"]
                    or controls["negative"]["resolved"]
                    or not controls["negative"].get("patch_successfully_applied")
                    or not controls["negative"]["tests_status"]["FAIL_TO_PASS"]["failure"]
                ):
                    raise RuntimeError("Official control did not produce its expected verdict")
                agent_input = folder / "instance.json"
                write_json(
                    agent_input,
                    {
                        k: row[k]
                        for k in ("instance_id", "repo", "base_commit", "problem_statement")
                    },
                )
                prediction = folder / "prediction.jsonl"
                argv = [
                    sys.executable,
                    str(self.source / "scripts/run_swebench_container.py"),
                    str(agent_input),
                    "--image",
                    image["Id"],
                    "--project-root",
                    str(self.source),
                    "--config",
                    str(self.config),
                    "--max-steps",
                    "240",
                    "--max-wall-time-seconds",
                    "7200",
                    "--no-cost-limit",
                    "--output",
                    str(prediction),
                ]
                record["worker_phase"] = self.command(folder, "baseline", argv, 8400)
                if not prediction.exists():
                    raise RuntimeError("Worker did not export a prediction")
                grade = self.swe_grade(case, folder, "grade", prediction)
                record.update(
                    official_result=grade, verdict="pass" if grade["resolved"] else "fail"
                )
            after = self.inspect_image(case["image"])
            if after is None or after["Id"] != image["Id"]:
                raise RuntimeError("Image changed during official control or model run")
        except Exception as exc:
            record.update(verdict="error", error_type=type(exc).__name__, error=str(exc))
        finally:
            if image is not None and image["Id"] not in self.retained:
                removed = subprocess.run(
                    ["docker", "image", "rm", case["image"]],
                    env=self.env,
                    capture_output=True,
                    text=True,
                )
                record["image_cleanup_returncode"] = removed.returncode
            record["finished_at"] = now()
            write_json(result_file, record)
            print(now(), case["id"], record.get("verdict"), record.get("error", ""), flush=True)

    def run(self) -> None:
        smoke = [
            "terminal-bench/openssl-selfsigned-cert",
            "terminal-bench/custom-memory-heap-crash",
            "terminal-bench/large-scale-text-editing",
            "astropy__astropy-12907",
            "django__django-14017",
            "sympy__sympy-18532",
        ]
        cases = [(kind, row) for kind in ("terminalbench", "swebench") for row in self.suite[kind]]
        order = {name: n for n, name in enumerate(smoke)}
        cases.sort(key=lambda pair: order.get(pair[1]["id"], len(smoke)))
        if self.args.only:
            selected = set(self.args.only)
            if selected - {r["id"] for _, r in cases}:
                raise ValueError("Unknown task ID; refusing an incomplete filter")
            cases = [(kind, row) for kind, row in cases if row["id"] in selected]
        for kind, case in cases:
            self.run_case(kind, case)
        write_json(self.root / "live.json", {"state": "batch_finished", "finished_at": now()})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--terminal-cache", type=Path, required=True)
    parser.add_argument("--harbor", type=Path, required=True)
    parser.add_argument("--swe-python", type=Path, required=True)
    parser.add_argument("--attempt", default="1")
    parser.add_argument("--only", action="append")
    args = parser.parse_args()
    args.run_root.mkdir(parents=True, exist_ok=True)
    with (args.run_root / "batch.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.write(str(os.getpid()))
        lock.flush()
        Batch(args).run()


if __name__ == "__main__":
    main()
