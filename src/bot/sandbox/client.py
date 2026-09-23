"""Managed one-shot worker IPC and strict replies that never grant host authority.

The native adapter supplies the installed worker and verified interpreter.
Arbitrary command stdout must never enter this decoder.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from pydantic import ValidationError

from bot.execution.base import ExecutionTarget, ProcessSpec, ProcessStatus
from bot.sandbox.models import SandboxError, SandboxErrorCode
from bot.sandbox.worker import (
    MAX_OUTPUT_BYTES,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    OPERATIONS,
    ProtocolError,
    decode_json_line,
    encode_json_line,
    validate_request,
)
from bot.tools.base import ToolResult

_METADATA = {
    "read": {"path": str, "start_line": int, "end_line": int},
    "search": {"result_count": int},
    "apply_patch": {"path": str, "bytes": int, "before_sha256": str, "after_sha256": str},
    "fetch": {"url": str, "status_code": int, "content_type": (str, type(None))},
    "file_version": {"path": str, "sha256": str},
}


async def execute_worker(target: ExecutionTarget, spec: ProcessSpec, request: dict) -> ToolResult:
    """One trusted worker process, with bounded stdin/stdout and managed cleanup."""
    data = encode_request(request)
    process_id = await target.start_process(spec)
    try:
        async with asyncio.timeout(spec.timeout_seconds):
            await target.send_process_input(process_id, data.decode("ascii"), eof=True)
            while True:
                snapshot = await target.poll_process(
                    process_id, wait_seconds=1, consume_output=False
                )
                if snapshot.truncated or not snapshot.stdout.isascii():
                    raise SandboxError(
                        SandboxErrorCode.PROTOCOL_ERROR,
                        "worker 输出过大或含非协议字节",
                        stage="worker_response",
                        executed=None,
                    )
                if snapshot.status not in {ProcessStatus.RUNNING, ProcessStatus.TERMINATING}:
                    break
        if snapshot.status == ProcessStatus.CLEANUP_FAILED:
            raise SandboxError(
                SandboxErrorCode.CLEANUP_FAILED,
                "worker 清理未完成",
                stage="worker_cleanup",
                executed=None,
            )
        if snapshot.status == ProcessStatus.TIMED_OUT:
            raise TimeoutError("worker 进程超时")
        if snapshot.status != ProcessStatus.COMPLETED or snapshot.returncode != 0:
            raise SandboxError(
                SandboxErrorCode.COMMAND_FAILED,
                f"worker 进程失败: {snapshot.status.value}; {snapshot.stderr[:2000]}",
                stage="worker_exit",
                executed=None,
            )
        if snapshot.truncated or not snapshot.output_complete:
            raise SandboxError(
                SandboxErrorCode.PROTOCOL_ERROR,
                "worker 输出被截断或未收齐",
                stage="worker_response",
                executed=None,
            )
        result = decode_response(
            snapshot.stdout.encode("utf-8"),
            request_id=request["id"],
            operation=request["op"],
            output_limit=request["context"]["max_output_bytes"],
        )
        result.metadata.update(
            {
                "execution_scope": snapshot.scope.model_dump(mode="json"),
                "backend": snapshot.execution_metadata.get("backend"),
                "backend_version": snapshot.execution_metadata.get("backend_version"),
                "cleanup_status": snapshot.cleanup_status,
            }
        )
        return result
    except BaseException as exc:
        cleanup = asyncio.create_task(
            target.terminate_process(process_id, reason="worker 调用结束或协议失败")
        )
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        cleaned = cleanup.result()
        # Cancellation remains cancellation, but a failed cleanup must not be
        # hidden behind an ordinary timeout/protocol/pipe error. The manager
        # retains ownership and capacity for unresolved processes.
        if cleaned.status == ProcessStatus.CLEANUP_FAILED and not isinstance(
            exc, asyncio.CancelledError
        ):
            raise SandboxError(
                SandboxErrorCode.CLEANUP_FAILED,
                "worker 清理未完成",
                stage="worker_cleanup",
                executed=None,
            ) from exc
        if isinstance(exc, (OSError, ValueError)) and not isinstance(exc, TimeoutError):
            raise SandboxError(
                SandboxErrorCode.PROTOCOL_ERROR,
                f"worker IPC 失败: {type(exc).__name__}",
                stage="worker_transport",
                executed=None,
            ) from exc
        raise


def encode_request(request: dict) -> bytes:
    try:
        validate_request(request)
        return encode_json_line(request, limit=MAX_REQUEST_BYTES)
    except ProtocolError as exc:
        raise SandboxError(
            SandboxErrorCode.CONFIG_INVALID, str(exc), stage="worker_request"
        ) from exc


def decode_response(
    raw: bytes, *, request_id: str, operation: str, output_limit: int = MAX_OUTPUT_BYTES
) -> ToolResult:
    try:
        reply = decode_json_line(raw, limit=MAX_RESPONSE_BYTES)
        if (
            operation not in OPERATIONS
            or reply.keys() != {"v", "id", "result"}
            or type(reply["v"]) is not int
            or reply["v"] != 1
            or reply["id"] != request_id
        ):
            raise ProtocolError("unexpected worker response identity")
        result = reply["result"]
        if (
            not isinstance(result, dict)
            or "success" not in result
            or result.keys() - {"success", "output", "error", "truncated", "metadata", "progress"}
        ):
            raise ProtocolError("unexpected worker result fields")
        if type(result["success"]) is not bool:
            raise ProtocolError("worker success must be boolean")
        if result["success"]:
            metadata = result.get("metadata")
            schema = _METADATA[operation]
            if not isinstance(metadata, dict) or metadata.keys() != schema.keys():
                raise ProtocolError("unexpected worker metadata fields")
            for key, expected in schema.items():
                if type(metadata[key]) not in (
                    expected if isinstance(expected, tuple) else (expected,)
                ):
                    raise ProtocolError("invalid worker metadata type")
                if "sha256" in key and not re.fullmatch(r"[0-9a-f]{64}", metadata[key]):
                    raise ProtocolError("invalid file version")
                if key == "path" and not Path(metadata[key]).is_absolute():
                    raise ProtocolError("worker returned a relative resource identity")
                if expected is int and metadata[key] < 0:
                    raise ProtocolError("negative worker metadata")
            if result.get("error") is not None:
                raise ProtocolError("successful worker result has an error")
            progress = result.get("progress")
            if operation == "file_version":
                if progress is not None:
                    raise ProtocolError("version probe cannot report progress")
            elif not isinstance(progress, dict) or progress.get("kind") not in (
                {"strong", "none"} if operation == "apply_patch" else {"weak"}
            ):
                raise ProtocolError("unexpected worker progress kind")
        elif result.keys() != {"success", "error"} or not isinstance(result["error"], str):
            raise ProtocolError("failed worker result contains unexpected data")
        validated = ToolResult.model_validate_json(
            encode_json_line(result, limit=MAX_RESPONSE_BYTES), strict=True
        )
        if len(validated.output.encode("utf-8")) > output_limit:
            raise ProtocolError("worker exceeded output budget")
        return validated
    except (ProtocolError, ValidationError, TypeError, ValueError, RecursionError) as exc:
        raise SandboxError(
            SandboxErrorCode.PROTOCOL_ERROR,
            f"worker 响应无效: {str(exc)[:2000]}",
            stage="worker_response",
            executed=None,
        ) from exc
