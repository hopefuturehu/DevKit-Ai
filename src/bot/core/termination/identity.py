"""Deterministic identities. No filesystem access or shell equivalence guessing."""

from __future__ import annotations

import hashlib
import json
import posixpath
from typing import Any


def fingerprint(*values: Any) -> str:
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def call_identity(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    workspace: str = "/",
    scope: str = "",
    hard_timeout: float | None = None,
) -> str:
    args = dict(arguments)
    if tool_name in {"run_command", "run_shell"}:
        if tool_name == "run_shell":
            args["argv"] = ["/bin/sh", "-c", args.pop("script", "")]
        tool_name = "run_command"
        args.pop("wait_seconds", None)
        args.setdefault("cwd", ".")
        args.setdefault("interactive", False)
        args.setdefault("timeout_seconds", hard_timeout)
        if args["timeout_seconds"] is not None:
            args["timeout_seconds"] = float(args["timeout_seconds"])
    elif tool_name == "read_file":
        args.setdefault("start_line", 1)
    elif tool_name == "search_text":
        for key, value in {"path": ".", "regex": False, "glob": "*", "max_results": 100}.items():
            args.setdefault(key, value)
    elif tool_name == "poll_process":
        args.pop("wait_seconds", None)
    elif tool_name == "load_context_reference":
        defaults = (
            {"max_matches": 8, "context_chars": 240, "case_sensitive": False}
            if "query" in args
            else {"offset": 0, "limit": 16_000}
        )
        for key, value in defaults.items():
            args.setdefault(key, value)
    if tool_name in {"run_command", "read_file", "search_text", "apply_patch"}:
        key = "cwd" if tool_name == "run_command" else "path"
        if isinstance(args.get(key), str):
            # Interpretation belongs to the execution target. Never resolve a
            # container path against the host filesystem here.
            args[key] = posixpath.normpath(posixpath.join(workspace, args[key]))
    return fingerprint(scope, workspace, tool_name, args)


def process_evidence(
    status: str,
    returncode: int | None,
    stdout_sha256: str,
    stderr_sha256: str,
) -> str:
    return fingerprint("process-result", status, returncode, stdout_sha256, stderr_sha256)
