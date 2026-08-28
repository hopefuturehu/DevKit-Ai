from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[2] / "scripts" / "run_context_blob_scale.py"
_SPEC = importlib.util.spec_from_file_location("run_context_blob_scale", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
run_context_blob_scale = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(run_context_blob_scale)


def test_context_blob_scale_soak_requires_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUN_CONTEXT_BLOB_SCALE_SOAK", raising=False)
    monkeypatch.setattr(sys, "argv", ["run_context_blob_scale.py", "--suite", "soak"])

    with pytest.raises(SystemExit, match="RUN_CONTEXT_BLOB_SCALE_SOAK=1"):
        run_context_blob_scale.main()


def test_context_blob_scale_rejects_zero_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["run_context_blob_scale.py", "--blob-count", "0"])

    with pytest.raises(SystemExit, match="--blob-count 必须大于 0"):
        run_context_blob_scale.main()
