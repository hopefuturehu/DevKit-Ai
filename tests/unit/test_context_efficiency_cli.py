from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[2] / "scripts" / "run_context_efficiency_benchmark.py"
_SPEC = importlib.util.spec_from_file_location("run_context_efficiency_benchmark", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
run_context_efficiency_benchmark = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(run_context_efficiency_benchmark)


def test_live_context_efficiency_requires_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUN_CONTEXT_EFFICIENCY_LIVE", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_context_efficiency_benchmark.py", "--provider", "live", "--repeat", "3"],
    )

    with pytest.raises(SystemExit, match="RUN_CONTEXT_EFFICIENCY_LIVE=1"):
        run_context_efficiency_benchmark.main()


@pytest.mark.parametrize(
    ("extra_args", "expected"),
    [
        ([], "至少需要 --repeat 3"),
        (["--repeat", "3", "--variants", "current"], "必须同时包含 raw 和 current"),
    ],
)
def test_live_context_efficiency_validates_comparison_shape_before_network(
    monkeypatch: pytest.MonkeyPatch,
    extra_args: list[str],
    expected: str,
) -> None:
    monkeypatch.setenv("RUN_CONTEXT_EFFICIENCY_LIVE", "1")
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_context_efficiency_benchmark.py", "--provider", "live", *extra_args],
    )

    with pytest.raises(SystemExit, match=expected):
        run_context_efficiency_benchmark.main()
