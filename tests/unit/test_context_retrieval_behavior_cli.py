from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[2] / "scripts" / "run_context_retrieval_behavior.py"
_SPEC = importlib.util.spec_from_file_location("run_context_retrieval_behavior", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
run_context_retrieval_behavior = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(run_context_retrieval_behavior)


def test_live_retrieval_behavior_requires_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUN_CONTEXT_RETRIEVAL_LIVE", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_context_retrieval_behavior.py", "--provider", "live", "--repeat", "3"],
    )

    with pytest.raises(SystemExit, match="RUN_CONTEXT_RETRIEVAL_LIVE=1"):
        run_context_retrieval_behavior.main()


def test_live_retrieval_behavior_requires_three_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUN_CONTEXT_RETRIEVAL_LIVE", "1")
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_context_retrieval_behavior.py", "--provider", "live"],
    )

    with pytest.raises(SystemExit, match="至少需要 --repeat 3"):
        run_context_retrieval_behavior.main()
