import json
import subprocess
from pathlib import Path

import pytest

from bot.evals.connect_proxy import _connect_target_allowed
from bot.evals.swebench import (
    SWEbenchInstance,
    build_agent_prompt,
    collect_model_patch,
    load_instance,
)
from bot.evals.swebench_worker import run_worker


def test_load_instance_selects_one_jsonl_row(tmp_path: Path) -> None:
    path = tmp_path / "instances.jsonl"
    rows = [
        {
            "instance_id": name,
            "repo": "owner/repo",
            "base_commit": "abc123",
            "problem_statement": f"fix {name}",
        }
        for name in ("first", "second")
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    instance = load_instance(path, "second")

    assert instance.instance_id == "second"
    assert instance.problem_statement == "fix second"


def test_load_instance_rejects_ambiguous_input(tmp_path: Path) -> None:
    path = tmp_path / "instances.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="恰好匹配一个"):
        load_instance(path)


def test_prompt_requires_an_implementation_not_just_an_explanation() -> None:
    instance = SWEbenchInstance("case", "owner/repo", "abc123", "broken behavior")

    prompt = build_agent_prompt(instance)

    assert "modify the implementation" in prompt
    assert "Do not merely describe" in prompt
    assert prompt.endswith("broken behavior")


def test_collect_model_patch_includes_new_files_but_excludes_runtime_state(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.py").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "baseline"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.py").write_text("after\n", encoding="utf-8")
    (tmp_path / "new.py").write_text("new\n", encoding="utf-8")
    (tmp_path / ".bot").mkdir()
    (tmp_path / ".bot" / "state.db").write_text("runtime\n", encoding="utf-8")

    patch = collect_model_patch(tmp_path)

    assert "tracked.py" in patch
    assert "new.py" in patch
    assert ".bot/state.db" not in patch


@pytest.mark.asyncio
async def test_container_worker_refuses_to_run_without_explicit_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SWEBENCH_CONTAINER", raising=False)

    with pytest.raises(RuntimeError, match="只允许在显式标记的一次性容器中运行"):
        await run_worker(
            tmp_path / "instance.json",
            Path("/testbed"),
            tmp_path / "config.toml",
        )


def test_restricted_connect_proxy_only_accepts_configured_upstream() -> None:
    assert _connect_target_allowed("CONNECT api.example:443 HTTP/1.1", "api.example", 443)
    assert not _connect_target_allowed(
        "CONNECT forbidden.example:443 HTTP/1.1", "api.example", 443
    )
    assert not _connect_target_allowed("GET api.example:443 HTTP/1.1", "api.example", 443)
