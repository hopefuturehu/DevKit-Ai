from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[2] / "scripts" / "run_context_full_stack_soak.py"
_SPEC = importlib.util.spec_from_file_location("run_context_full_stack_soak", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
run_context_full_stack_soak = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(run_context_full_stack_soak)


def test_context_full_stack_soak_requires_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUN_CONTEXT_FULL_STACK_SOAK", raising=False)
    monkeypatch.setattr(sys, "argv", ["run_context_full_stack_soak.py", "--suite", "soak"])

    with pytest.raises(SystemExit, match="RUN_CONTEXT_FULL_STACK_SOAK=1"):
        run_context_full_stack_soak.main()


def test_context_full_stack_rejects_short_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["run_context_full_stack_soak.py", "--turns", "5"])

    with pytest.raises(SystemExit, match="--turns 至少为 6"):
        run_context_full_stack_soak.main()
