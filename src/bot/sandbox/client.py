"""Trusted-side worker protocol validation; replies never grant host authority.

Process launch/transport wiring is deliberately separate from this codec. The
native adapter must provide a trusted installed worker and verified interpreter,
close stdin after one request, and verify process exit and complete output before
accepting a reply. Arbitrary command stdout must never enter this decoder.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import ValidationError

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
