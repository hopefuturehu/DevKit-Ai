#!/usr/bin/env python3
"""Check existing process management against SRT's real macOS process tree.

The independent fixture heartbeat supplies ground truth, not ownership to the
target. This detects false completion when a short-lived parent loses a
detached child before the target's inventory observes it.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import tempfile
import time
from pathlib import Path

import psutil
from probe_native_sandbox import Probe, settings

from bot.execution.base import ProcessSpec
from bot.execution.local import LocalExecutionTarget


class ProbeTarget(LocalExecutionTarget):
    @staticmethod
    def _safe_environment() -> dict[str, str]:
        # Test only the manager, not Local's ordinary host environment policy.
        return {}


async def check_case(runtime: Path, *, immediate_exit: bool, strict: bool) -> dict:
    with tempfile.TemporaryDirectory(prefix="bot-srt-manager-", dir="/tmp") as temp:
        fixture = Path(temp).resolve()
        for name in ("work", "outside", "control", "home", "tmp", "cache"):
            (fixture / name).mkdir()
        probe = Probe(runtime, fixture)
        probe.policy.write_text(json.dumps(settings(runtime, fixture)))
        heartbeat = fixture / "work/child.json"
        child_code = (
            "const fs=require('node:fs');const out=process.argv[1];"
            "function tick(){fs.writeFileSync(out,"
            "JSON.stringify({pid:process.pid,tick:Date.now()}))}"
            "tick();setInterval(tick,50)"
        )
        code = (
            "const c=require('node:child_process').spawn(process.execPath,"
            + json.dumps(["--eval", child_code, "--", str(heartbeat)])
            + ",{detached:true,stdio:'ignore'});c.unref();"
            + ("" if immediate_exit else "setInterval(()=>{},1000)")
        )
        target = ProbeTarget(strict_process_scope=strict)
        child = None
        born = None
        started = time.time()
        result: dict = {}
        try:
            process_id = await target.start_process(
                ProcessSpec(
                    argv=probe.argv(code),
                    cwd=fixture / "work",
                    env=probe.env,
                    session_id="p0-fixture",
                    run_id="p0-fixture",
                )
            )
            for _ in range(250):
                if heartbeat.exists():
                    try:
                        pid = json.loads(heartbeat.read_text())["pid"]
                        candidate = psutil.Process(pid)
                        argv = candidate.cmdline()
                        if argv[-1:] == [str(heartbeat)] and candidate.create_time() >= started - 1:
                            child, born = candidate, candidate.create_time()
                            break
                    except (ValueError, psutil.Error):
                        pass
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.6)
            snapshot = await target.poll_process(process_id, wait_seconds=0)
            if immediate_exit:
                snapshot = await target.poll_process(process_id, wait_seconds=6)
            if not immediate_exit:
                snapshot = await target.terminate_process(
                    process_id, reason="P0 cancellation probe"
                )
            live = False
            if child is not None:
                try:
                    live = child.create_time() == born and child.status() != psutil.STATUS_ZOMBIE
                except psutil.Error:
                    pass
            false_completion = live and snapshot.status.value in {
                "completed",
                "failed",
                "cancelled",
                "timed_out",
            }
            resume_blocked = None
            if strict and immediate_exit and live:
                try:
                    await target.start_process(
                        ProcessSpec(
                            argv=probe.argv("console.log('unexpected-resume')"),
                            cwd=fixture / "work",
                            env=probe.env,
                            session_id="p0-fixture",
                            run_id="p0-fixture",
                        )
                    )
                    resume_blocked = False
                except RuntimeError as exc:
                    resume_blocked = "process_cleanup_incomplete" in str(exc)
            result.update(
                {
                    "name": "immediate_exit_detached_child"
                    if immediate_exit
                    else "cancel_detached_child",
                    "passed": child is not None
                    and not false_completion
                    and (immediate_exit or not live)
                    and (not (strict and immediate_exit and live) or resume_blocked is True),
                    "child_observed": child is not None,
                    "child_live": live,
                    "status": snapshot.status.value,
                    "cleanup_status": snapshot.cleanup_status,
                    "containment": snapshot.containment,
                    "output_complete": snapshot.output_complete,
                    "false_completion": false_completion,
                    "quota_retained": target.has_live_processes,
                    "resume_blocked": resume_blocked,
                }
            )
            return result
        finally:
            # Only the probe's independently identified, unique fixture process
            # is forced down. Never feed this external knowledge to the target.
            if child is not None:
                try:
                    if child.create_time() == born:
                        child.kill()
                        await asyncio.to_thread(child.wait, timeout=3)
                except psutil.Error:
                    pass
            try:
                await target.aclose()
            except RuntimeError as exc:
                result["close_error"] = str(exc)
                result["unattributed_count"] = sum(
                    len(item.scope.unattributed) for item in target._managed_processes.values()
                )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    if platform.system() != "Darwin":
        raise SystemExit("This diagnostic uses macOS host PID fixture semantics")
    cases = [await check_case(args.runtime.resolve(), immediate_exit=False, strict=args.strict)]
    for _ in range(3):
        cases.append(
            await check_case(args.runtime.resolve(), immediate_exit=True, strict=args.strict)
        )
    evidence = {
        "phase": "P0",
        "platform": platform.platform(),
        "cases": cases,
        "passed": all(case["passed"] for case in cases),
        "release_ready": False,
        "strict": args.strict,
        "probe_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2) + "\n")
    for case in cases:
        print(f"{'PASS' if case['passed'] else 'FAIL'} {case['name']}: {case['status']}")
    raise SystemExit(0 if evidence["passed"] else 1)


if __name__ == "__main__":
    asyncio.run(main())
