"""Resume a preselected summary pair in disposable clones of a reconstructed Docker checkpoint."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import tomli_w


def save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def worker() -> None:
    from bot.cli.runtime import build_runtime
    from bot.core.approval import AllowApprovalHandler
    from bot.core.events import JsonlEventSink
    from bot.core.models import RunRequest
    from bot.evals.terminalbench_worker import _worker_config_overrides

    if not Path("/.dockerenv").exists():
        raise RuntimeError("Continuation worker requires a disposable Docker container")
    folder = Path("/logs/fidelity")
    metadata = json.loads((folder / "trial.json").read_text())
    with (folder / "events.jsonl").open("w") as events:
        runtime = build_runtime(
            workspace=Path("/app"),
            config_path=folder / "config.toml",
            event_sinks=[JsonlEventSink(events)],
            approval_handler=AllowApprovalHandler(),
            config_overrides=_worker_config_overrides(
                state_path=folder / "state.db",
                max_steps=12,
                max_wall_time_seconds=600,
                max_cost_usd=2,
                subagents_enabled=False,
            ),
        )
        original_stream = runtime.runner.provider.stream
        requests = 0

        async def recorded(request):
            nonlocal requests
            requests += 1
            save(folder / f"request-{requests}.json", request.model_dump(mode="json"))
            async for event in original_stream(request):
                yield event

        runtime.runner.provider.stream = recorded
        try:
            result = await runtime.runner.run(
                RunRequest(session_id=metadata["session_id"], prompt="继续完成原任务。")
            )
            save(folder / "result.json", result.model_dump(mode="json"))
            print(result.model_dump_json(), flush=True)
        finally:
            await runtime.aclose()


def run_host(replay: Path, output: Path, image: str) -> None:
    from bot.config import load_config, resolve_model_api_key
    from bot.config.models import AppConfig
    from bot.core.events import EventBus, EventType
    from bot.sessions import SQLiteSessionStore

    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((replay / "manifest.json").read_text())
    results = json.loads((replay / "results.json").read_text())
    selected = None
    for repeat in range(1, manifest["repeats"] + 1):
        rows = [r for r in results if r["case"] == "initial" and r["repeat"] == repeat]
        if len(rows) == 2 and all(r["result"]["compacted"] for r in rows):
            selected = repeat
            break
    if selected is None:
        raise ValueError("No complete published pair yet")
    if output.exists():
        raise ValueError("Use a fresh continuation directory")
    output.mkdir(parents=True)
    assets = replay.parent / "continuation-assets"
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True
    ).strip()
    order = ["baseline", "improved"]
    random.Random(20260912).shuffle(order)
    protocol = {
        "created_at": datetime.now(UTC).isoformat(),
        "selected_repeat": selected,
        "selection": manifest["continuation_selection"],
        "image_id": image_id,
        "order": order,
        "max_steps": 12,
        "max_wall_seconds": 600,
        "max_cost_usd_conservative_per_run": 2,
        "script_sha256": digest(Path(__file__)),
        "source_restore": json.loads((assets / "restore.json").read_text()),
        "checkpoint_files_sha256": (assets / "files.sha256").read_text(),
        "limitations": [
            "One selected pair is a pilot, not an estimate of general task success.",
            "Filesystem reconstructed from original operations 55,57,59; "
            "no original process or kernel cache snapshot.",
            "Both clones use the same image/file hashes; "
            "extra verification libraries are installed in the agent virtualenv.",
            "Both arms receive the same neutral resume instruction; original history is immutable.",
            "Only visible history blobs and the last historical plan are restored; no future data.",
            "12 steps or 600 seconds is a short continuation, not the full 29-minute attempt.",
        ],
    }
    save(output / "manifest.json", protocol)
    local = load_config(root)
    env = dict(os.environ)
    env["BOT_MODEL_API_KEY"] = resolve_model_api_key(local.model, workspace=root)
    original_db = (root / manifest["source"]["source_db"]).resolve()
    import tomllib

    config_path = root / "artifacts/context-strategy-path-20260910/a-output-32k/a.toml"
    for arm in order:
        folder = output / arm
        folder.mkdir()
        source_trial = replay / "replays" / f"initial-{selected}-{arm}"
        with sqlite3.connect((source_trial / "state.db").as_uri() + "?mode=ro", uri=True) as src:
            with sqlite3.connect(folder / "state.db") as dest:
                src.backup(dest)
        session = manifest["seeds"]["initial"]["session_id"]
        store = SQLiteSessionStore(folder / "state.db")
        restored = []
        try:
            entries = store.load_positioned_messages(session)
            references = set(
                re.findall(
                    r"blob:[0-9a-f]{64}", "\n".join(e.message.model_dump_json() for e in entries)
                )
            )
            with sqlite3.connect(original_db.as_uri() + "?mode=ro", uri=True) as source:
                for reference in sorted(references):
                    row = source.execute(
                        "SELECT content,media_type FROM context_blobs WHERE id=?", (reference,)
                    ).fetchone()
                    if row is None:
                        raise ValueError(f"missing historical blob {reference}")
                    actual = store.put_context_blob(
                        session_id=session, run_id="seed", content=row[0], media_type=row[1]
                    )
                    assert actual == reference
                    restored.append(reference)
            store.finish_run("replay", "completed")
            plan_calls = [
                call
                for entry in entries
                for call in entry.message.tool_calls
                if call.name == "update_plan"
            ]
            if plan_calls:
                asyncio.run(
                    EventBus([store]).emit(
                        EventType.PLAN_UPDATED,
                        session_id=session,
                        run_id="seed",
                        payload=plan_calls[-1].arguments,
                    )
                )
        finally:
            store.close()
        cfg = AppConfig.model_validate(tomllib.loads(config_path.read_text()))
        cfg.context.compaction_strategy = "a_fallback"
        cfg.model.api_key = None
        cfg.model.api_key_ref = "env:BOT_MODEL_API_KEY"
        cfg.model.max_output_tokens = 32768
        cfg.memory.enabled = cfg.memory.auto_extract = False
        cfg.subagents.enabled = False
        (folder / "config.toml").write_text(
            tomli_w.dumps(cfg.model_dump(mode="json", exclude_none=True))
        )
        container = f"bot-fidelity-{arm}-20260912"
        trial = {
            "arm": arm,
            "session_id": session,
            "container": container,
            "summary_sha256": digest(source_trial / "summary.md"),
            "restored_blobs": restored,
            "started_at": datetime.now(UTC).isoformat(),
        }
        save(folder / "trial.json", trial)
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "--platform",
                "linux/amd64",
                "--entrypoint",
                "/bin/sh",
                image_id,
                "-c",
                "sleep infinity",
            ],
            check=True,
            capture_output=True,
        )
        try:
            subprocess.run(
                ["docker", "exec", container, "mkdir", "-p", "/logs/fidelity"], check=True
            )
            for path in folder.iterdir():
                subprocess.run(
                    ["docker", "cp", str(path), f"{container}:/logs/fidelity/{path.name}"],
                    check=True,
                )
            subprocess.run(
                [
                    "docker",
                    "cp",
                    str(Path(__file__)),
                    f"{container}:/installed-agent/continuation.py",
                ],
                check=True,
            )
            for name in ("service", "strategies"):
                subprocess.run(
                    [
                        "docker",
                        "cp",
                        str(replay / "frozen" / f"{arm}_{name}.py"),
                        f"{container}:/installed-agent/venv/lib/python3.12/site-packages/bot/compaction/{name}.py",
                    ],
                    check=True,
                )
            check = subprocess.run(
                ["docker", "exec", "-i", container, "sha256sum", "-c", "-"],
                input=protocol["checkpoint_files_sha256"],
                text=True,
                capture_output=True,
            )
            (folder / "checkpoint-check.log").write_text(check.stdout + check.stderr)
            assert check.returncode == 0
            print(f"starting continuation {arm}", flush=True)
            with (folder / "worker.log").open("w") as log:
                completed = subprocess.run(
                    [
                        "docker",
                        "exec",
                        "-e",
                        "BOT_MODEL_API_KEY",
                        "-e",
                        "BOT_DEEPSEEK_TOKENIZER_PATH=/installed-agent/tokenizer.json",
                        container,
                        "/installed-agent/venv/bin/python",
                        "/installed-agent/continuation.py",
                        "--worker",
                    ],
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=720,
                )
            trial["worker_returncode"] = completed.returncode
        finally:
            subprocess.run(
                ["docker", "cp", f"{container}:/logs/fidelity/.", str(folder)], check=True
            )
            trial["ended_at"] = datetime.now(UTC).isoformat()
            save(folder / "trial.json", trial)
            subprocess.run(["docker", "stop", container], capture_output=True)
        print(f"ended continuation {arm}: {trial.get('worker_returncode')}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--replay-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--image", default="bot-fidelity-checkpoint:20260912")
    args = parser.parse_args()
    if args.worker:
        asyncio.run(worker())
    elif args.replay_root and args.output:
        run_host(args.replay_root.resolve(), args.output.resolve(), args.image)
    else:
        parser.error("--replay-root and --output are required")


if __name__ == "__main__":
    main()
