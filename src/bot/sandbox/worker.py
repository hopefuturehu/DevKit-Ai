"""Standalone, stdlib-only file worker; run its trusted installed copy with -I -S.

The protocol carries data and a fixed operation enum, never Python code. These
path checks are defense in depth: the host must still launch this worker under
the immutable OS policy. No request or resource is processed at import time.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import http.client
import json
import os
import re
import secrets
import ssl
import stat
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

MAX_REQUEST_BYTES = 2_000_000
MAX_RESPONSE_BYTES = 8_000_000
MAX_OUTPUT_BYTES = 1_000_000
MAX_FILE_BYTES = 16_000_000
MAX_SEARCH_ENTRIES = 250_000
OPERATIONS = {
    "read": ({"path"}, {"start_line", "end_line"}),
    "search": ({"query"}, {"path", "regex", "glob", "max_results"}),
    "apply_patch": ({"path", "old_text", "new_text"}, {"create"}),
    "fetch": ({"url"}, {"timeout_seconds"}),
    "file_version": ({"path"}, set()),
}


class ProtocolError(ValueError):
    pass


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ProtocolError("duplicate JSON key")
        value[key] = item
    return value


def _invalid_constant(value):
    raise ProtocolError("non-finite JSON number")


def decode_json_line(raw: bytes, *, limit: int) -> dict:
    if not raw or len(raw) > limit or not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise ProtocolError("expected one complete bounded JSON line")
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_invalid_constant
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("invalid JSON object") from exc
    if not isinstance(value, dict):
        raise ProtocolError("expected JSON object")
    return value


def encode_json_line(value: dict, *, limit: int) -> bytes:
    try:
        raw = (
            json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n"
        ).encode()
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProtocolError("unencodable JSON object") from exc
    if len(raw) > limit:
        raise ProtocolError("JSON line exceeds limit")
    return raw


def _keys(value, required: set[str], optional: set[str] | None = None):
    if (
        not isinstance(value, dict)
        or not required <= value.keys()
        or value.keys() - required - (optional or set())
    ):
        raise ProtocolError("unexpected or missing fields")


def _string(value):
    if not isinstance(value, str) or "\x00" in value:
        raise ProtocolError("expected string without NUL")
    # Unpaired surrogate escapes must not reach paths, progress keys or streams.
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ProtocolError("invalid Unicode string") from exc


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ProtocolError("integer outside permitted range")


def validate_request(value: dict) -> None:
    """Shape validation only; safe to call before launching the worker."""
    _keys(value, {"v", "id", "op", "args", "context"})
    if type(value["v"]) is not int or value["v"] != 1:
        raise ProtocolError("unsupported protocol version")
    if not isinstance(value["id"], str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", value["id"]):
        raise ProtocolError("invalid request id")
    op = value["op"]
    if not isinstance(op, str) or op not in OPERATIONS:
        raise ProtocolError("unknown operation")
    args = value["args"]
    _keys(args, *OPERATIONS[op])
    for key, item in args.items():
        if key in {"regex", "create"}:
            if type(item) is not bool:
                raise ProtocolError("expected boolean")
        elif key in {"start_line", "end_line", "max_results"}:
            _integer(item, 1, 500 if key == "max_results" else 2**31 - 1)
        elif key == "timeout_seconds":
            if type(item) not in {int, float} or not 1 <= item <= 120:
                raise ProtocolError("invalid timeout")
        else:
            _string(item)
    context = value["context"]
    _keys(
        context,
        {
            "workspace",
            "workspace_only",
            "denied_paths",
            "max_output_bytes",
            "read_only",
            "network_mode",
            "allowed_domains",
        },
    )
    _string(context["workspace"])
    if not Path(context["workspace"]).is_absolute():
        raise ProtocolError("workspace must be absolute")
    for key in ("workspace_only", "read_only"):
        if type(context[key]) is not bool:
            raise ProtocolError("expected context boolean")
    _integer(context["max_output_bytes"], 1, MAX_OUTPUT_BYTES)
    if context["network_mode"] not in ("deny", "allowlist"):
        raise ProtocolError("invalid network mode")
    for key in ("denied_paths", "allowed_domains"):
        if not isinstance(context[key], list) or len(context[key]) > 4096:
            raise ProtocolError("invalid context list")
        for item in context[key]:
            _string(item)
            if key == "denied_paths" and not Path(item).is_absolute():
                raise ProtocolError("denied path must be absolute")
    if context["network_mode"] == "deny" and context["allowed_domains"]:
        raise ProtocolError("deny network cannot carry domains")


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _bounded(text: str, limit: int) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    return raw[:limit].decode("utf-8", errors="ignore"), len(raw) > limit


def _resolve(context, raw: str, *, must_exist=False) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(context["workspace"]) / path
    path = path.resolve(strict=must_exist)
    if context["workspace_only"] and not path.is_relative_to(Path(context["workspace"]).resolve()):
        raise ValueError("路径超出工作区")
    if any(path.is_relative_to(Path(p).resolve()) for p in context["denied_paths"]):
        raise ValueError("拒绝访问受保护的内部路径")
    return path


def _read_regular(path: Path, *, limit=MAX_FILE_BYTES) -> tuple[bytes, os.stat_result]:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("不是普通文件")
        if before.st_nlink != 1:
            raise ValueError("拒绝读取硬链接别名")
        if before.st_size > limit:
            raise ValueError("文件超过 worker 大小限制")
        data = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
    if len(data) > limit or _stamp(before) != _stamp(after):
        raise ValueError("读取期间文件变化或超过大小限制")
    return data, after


def _stamp(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_size,
    )


def _read(context, args):
    path = _resolve(context, args["path"], must_exist=True)
    raw, _ = _read_regular(path)
    lines = raw.decode("utf-8", errors="replace").splitlines(keepends=True)
    start, end = args.get("start_line", 1), args.get("end_line", len(lines))
    # Match the existing read_file behavior, including its empty-file range.
    if start < 1 or end < start:
        raise ValueError("行范围不合法")
    output, truncated = _bounded("".join(lines[start - 1 : end]), context["max_output_bytes"])
    return {
        "success": True,
        "output": output,
        "truncated": truncated,
        "metadata": {"path": str(path), "start_line": start, "end_line": end},
        "progress": {
            "kind": "weak",
            "summary": f"读取了 {path.name}",
            "subject_key": f"file:{path}",
            "resource_version": _hash(raw),
            "evidence_complete": not truncated,
            "evidence_key": f"read:{path}:{start}:{end}:{_hash(output.encode())}",
        },
    }


def _file_version(context, args):
    path = _resolve(context, args["path"], must_exist=True)
    raw, _ = _read_regular(path)
    return {"success": True, "metadata": {"path": str(path), "sha256": _hash(raw)}}


def _walk(root):
    ignored = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules"}
    if root.is_file():
        yield root
    else:
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(name for name in dirs if name not in ignored)
            for name in sorted(files):
                yield Path(directory) / name


def _search(context, args):
    root = _resolve(context, args.get("path", "."), must_exist=True)
    query = args["query"]
    pattern = re.compile(query) if args.get("regex", False) else None
    results, length, truncated = [], 0, False
    for index, path in enumerate(_walk(root)):
        if index >= MAX_SEARCH_ENTRIES:
            truncated = True
            break
        if not fnmatch.fnmatch(path.name, args.get("glob", "*")):
            continue
        try:
            resolved = _resolve(context, str(path), must_exist=True)
            # Existing search_text always searches workspace-relative paths.
            resolved.relative_to(Path(context["workspace"]))
            data, _ = _read_regular(resolved, limit=5_000_000)
        except (OSError, ValueError):
            continue
        for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if not (pattern.search(line) if pattern else query in line):
                continue
            text = f"{path.relative_to(context['workspace'])}:{number}:{line}"
            available = context["max_output_bytes"] - length - bool(results)
            if available <= 0:
                truncated = True
                break
            text, cut = _bounded(text, available)
            results.append(text)
            length += len(text.encode()) + (len(results) > 1)
            if cut or len(results) >= args.get("max_results", 100):
                truncated = True
                break
        if truncated:
            break
    output = "\n".join(results)
    displayed, cut = _bounded(output or "未找到匹配内容。", context["max_output_bytes"])
    truncated |= cut
    return {
        "success": True,
        "output": displayed,
        "truncated": truncated,
        "metadata": {"result_count": len(results)},
        "progress": {
            "kind": "weak",
            "summary": f"搜索得到 {len(results)} 条证据",
            "evidence_key": f"search:{root}:{query}:{_hash(output.encode())}",
            "subject_key": f"tree:{root}",
            "evidence_complete": not truncated,
        },
    }


def _apply_patch(context, args):
    if context["read_only"]:
        raise ValueError("只读任务不能修改工作区")
    path = _resolve(context, args["path"])
    try:
        raw, original_stat = _read_regular(path)
        original = raw.decode("utf-8")
    except FileNotFoundError:
        if not args.get("create", False) or args["old_text"]:
            raise ValueError("文件不存在") from None
        original, original_stat = "", None
    old, new = args["old_text"], args["new_text"]
    count = original.count(old) if old else 1
    if count != 1:
        raise ValueError(f"old_text 需要恰好匹配一次，实际匹配 {count} 次")
    updated = (original.replace(old, new, 1) if old else new).encode("utf-8")
    if len(updated) > MAX_FILE_BYTES:
        raise ValueError("更新后的文件超过 worker 大小限制")
    path.parent.mkdir(parents=True, exist_ok=True)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = f".bot-patch-{secrets.token_hex(16)}"
    created = False
    try:
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        created = True
        with os.fdopen(fd, "wb") as handle:
            handle.write(updated)
            handle.flush()
            if original_stat is not None:
                os.fchmod(handle.fileno(), stat.S_IMODE(original_stat.st_mode))
            os.fsync(handle.fileno())
        try:
            current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if (current is None) != (original_stat is None) or (
            current is not None and _stamp(current) != _stamp(original_stat)
        ):
            raise ValueError("patch 期间目标文件已变化")
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        created = False
    finally:
        if created:
            os.unlink(temporary, dir_fd=directory)
        os.close(directory)
    before, after = _hash(original.encode()), _hash(updated)
    try:
        display = path.relative_to(context["workspace"])
    except ValueError:
        display = path
    output, truncated = _bounded(f"已更新 {display}", context["max_output_bytes"])
    return {
        "success": True,
        "output": output,
        "truncated": truncated,
        "metadata": {
            "path": str(path),
            "bytes": len(updated),
            "before_sha256": before,
            "after_sha256": after,
        },
        "progress": {
            "kind": "strong" if before != after else "none",
            "summary": f"文件 {path.name} 内容{'已变化' if before != after else '未变化'}",
            "evidence_key": f"file:{path}:{after}",
            "subject_key": f"file:{path}",
            "resource_version": after,
            "evidence_complete": True,
        },
    }


def _destination(context, url):
    if context["network_mode"] != "allowlist":
        raise ValueError("当前任务禁止联网")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("只支持不含用户凭据的 HTTP(S) URL")
    if any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise ValueError("URL 包含控制字符或空白")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise ValueError("URL 端口无效")
    authority = f"[{host}]" if ":" in host else host
    if (
        authority not in context["allowed_domains"]
        and f"{authority}:{port}" not in context["allowed_domains"]
    ):
        raise ValueError("目标不在任务域名白名单中")
    return parsed, host, port


def _fetch(context, args):
    url = args["url"]
    deadline = time.monotonic() + args.get("timeout_seconds", 30)
    for redirects in range(6):
        parsed, host, port = _destination(context, url)
        proxy = urlsplit(
            os.environ.get("HTTPS_PROXY" if parsed.scheme == "https" else "HTTP_PROXY", "")
        )
        if proxy.scheme != "http" or proxy.hostname != "127.0.0.1" or not proxy.port:
            raise ValueError("缺少受控 HTTP 代理；禁止直接联网")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("获取 URL 超时")
        authorization = {}
        if proxy.username is not None:
            token = f"{unquote(proxy.username)}:{unquote(proxy.password or '')}"
            authorization["Proxy-Authorization"] = (
                "Basic " + base64.b64encode(token.encode()).decode()
            )
        if parsed.scheme == "https":
            connection = http.client.HTTPSConnection(
                proxy.hostname, proxy.port, timeout=remaining, context=ssl.create_default_context()
            )
            connection.set_tunnel(host, port, headers=authorization)
            destination = parsed.path or "/"
            if parsed.query:
                destination += "?" + parsed.query
            headers = {"User-Agent": "bot-cli-agent/0.1", "Accept-Encoding": "identity"}
        else:
            connection = http.client.HTTPConnection(proxy.hostname, proxy.port, timeout=remaining)
            destination = parsed._replace(fragment="").geturl()
            headers = {
                "User-Agent": "bot-cli-agent/0.1",
                "Accept-Encoding": "identity",
                **authorization,
            }
        try:
            connection.request("GET", destination, headers=headers)
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location or redirects == 5:
                    raise ValueError("无效重定向或重定向次数超过限制")
                url = urljoin(url, location)
                continue
            if response.status >= 400:
                raise ValueError(f"HTTP 请求失败: {response.status}")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise ValueError("服务器返回了尚未支持的压缩响应")
            limit = context["max_output_bytes"]
            data = bytearray()
            while len(data) <= limit:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("获取 URL 超时")
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(8192, limit + 1 - len(data)))
                if not chunk:
                    if response.length not in (None, 0):
                        raise ValueError("HTTP 响应在声明的长度之前结束")
                    break
                data.extend(chunk)
            output, cut = _bounded(bytes(data[:limit]).decode("utf-8", errors="replace"), limit)
            return {
                "success": True,
                "output": output,
                "truncated": cut or len(data) > limit,
                "metadata": {
                    "url": url,
                    "status_code": response.status,
                    "content_type": response.getheader("Content-Type"),
                },
                "progress": {
                    "kind": "weak",
                    "summary": f"获取了 {url} 的网络证据",
                    "evidence_key": f"fetch:{url}:{response.status}:{_hash(output.encode())}",
                },
            }
        finally:
            connection.close()
    raise ValueError("重定向次数超过限制")


def handle_request(request: dict) -> dict:
    validate_request(request)
    try:
        result = {
            "read": _read,
            "search": _search,
            "apply_patch": _apply_patch,
            "fetch": _fetch,
            "file_version": _file_version,
        }[request["op"]](request["context"], request["args"])
    except (OSError, ValueError, UnicodeError, http.client.HTTPException) as exc:
        error, _ = _bounded(str(exc), 2000)
        result = {"success": False, "error": error}
    return {"v": 1, "id": request["id"], "result": result}


def main() -> int:
    try:
        raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
        request = decode_json_line(raw, limit=MAX_REQUEST_BYTES)
        validate_request(request)
        # The host closes stdin after one request. Check framing before any
        # operation so a second request cannot cause partial mutation.
        if sys.stdin.buffer.read(1):
            raise ProtocolError("worker accepts exactly one request")
        response = encode_json_line(handle_request(request), limit=MAX_RESPONSE_BYTES)
        sys.stdout.buffer.write(response)
        sys.stdout.buffer.flush()
        return 0
    except ProtocolError as exc:
        print(f"worker protocol error: {exc}", file=sys.stderr)
        return 65


if __name__ == "__main__":
    raise SystemExit(main())
