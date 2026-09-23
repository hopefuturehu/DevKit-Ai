"""Worker operations and hostile protocol cases; these are not OS isolation tests."""

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from bot.sandbox import worker
from bot.sandbox.client import decode_response, encode_request
from bot.sandbox.models import SandboxError


@pytest.fixture
def worker_request(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    return {
        "v": 1,
        "id": "request-1",
        "op": "read",
        "args": {"path": "sample.txt"},
        "context": {
            "workspace": str(work),
            "workspace_only": True,
            "read_only": False,
            "denied_paths": [],
            "max_output_bytes": 1_000_000,
            "network_mode": "deny",
            "allowed_domains": [],
        },
    }


def request_for(request, op, args, **context):
    result = copy.deepcopy(request)
    result.update(op=op, args=args)
    result["context"].update(context)
    return result


def invoke(request):
    reply = worker.handle_request(request)
    return decode_response(
        worker.encode_json_line(reply, limit=worker.MAX_RESPONSE_BYTES),
        request_id=request["id"],
        operation=request["op"],
        output_limit=request["context"]["max_output_bytes"],
    )


def test_read_range_version_and_utf8_truncation(worker_request):
    work = Path(worker_request["context"]["workspace"])
    raw = "首行\n第二行\nthird\n".encode()
    (work / "sample.txt").write_bytes(raw)
    selected = invoke(
        request_for(worker_request, "read", {"path": "sample.txt", "start_line": 2, "end_line": 3})
    )
    assert selected.success and selected.output == "第二行\nthird\n"
    assert selected.progress.resource_version == hashlib.sha256(raw).hexdigest()
    truncated = invoke(
        request_for(worker_request, "read", {"path": "sample.txt"}, max_output_bytes=5)
    )
    assert truncated.success and truncated.truncated
    assert truncated.output == "首" and not truncated.progress.evidence_complete
    version = invoke(request_for(worker_request, "file_version", {"path": "sample.txt"}))
    assert version.metadata["sha256"] == selected.progress.resource_version


def test_patch_unique_atomic_mode_and_noop_progress(worker_request):
    work = Path(worker_request["context"]["workspace"])
    path = work / "sample.txt"
    path.write_text("before once\n")
    path.chmod(0o751)
    patched = invoke(
        request_for(
            worker_request,
            "apply_patch",
            {"path": "sample.txt", "old_text": "once", "new_text": "after"},
        )
    )
    assert patched.success and path.read_text() == "before after\n"
    assert path.stat().st_mode & 0o777 == 0o751
    assert patched.progress.kind.value == "strong"
    assert patched.metadata["after_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    noop = invoke(
        request_for(
            worker_request,
            "apply_patch",
            {"path": "sample.txt", "old_text": "after", "new_text": "after"},
        )
    )
    assert noop.success and noop.progress.kind.value == "none"
    short = invoke(
        request_for(
            worker_request,
            "apply_patch",
            {"path": "sample.txt", "old_text": "after", "new_text": "after"},
            max_output_bytes=3,
        )
    )
    assert short.success and short.truncated and len(short.output.encode()) <= 3
    path.write_text("twice twice")
    ambiguous = invoke(
        request_for(
            worker_request,
            "apply_patch",
            {"path": "sample.txt", "old_text": "twice", "new_text": "bad"},
        )
    )
    assert not ambiguous.success and path.read_text() == "twice twice"
    created = invoke(
        request_for(
            worker_request,
            "apply_patch",
            {"path": "new/created.txt", "old_text": "", "new_text": "created", "create": True},
        )
    )
    assert created.success and (work / "new/created.txt").read_text() == "created"
    assert not list(work.rglob(".bot-patch-*"))


def test_patch_failure_or_concurrent_change_preserves_destination(worker_request, monkeypatch):
    work = Path(worker_request["context"]["workspace"])
    path = work / "sample.txt"
    path.write_text("original")
    request = request_for(
        worker_request,
        "apply_patch",
        {"path": "sample.txt", "old_text": "original", "new_text": "modified"},
    )

    def failed_replace(*args, **kwargs):
        raise OSError("injected replace failure")

    with monkeypatch.context() as patch:
        patch.setattr(worker.os, "replace", failed_replace)
        assert not invoke(request).success
    assert path.read_text() == "original" and not list(work.glob(".bot-patch-*"))
    real_stat = os.stat

    def changed_stat(name, *args, **kwargs):
        if name == path.name and kwargs.get("dir_fd") is not None:
            path.write_text("external change")
        return real_stat(name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(worker.os, "stat", changed_stat)
        result = invoke(request)
    assert not result.success and "已变化" in result.error
    assert path.read_text() == "external change" and not list(work.glob(".bot-patch-*"))


@pytest.mark.parametrize(
    "kind", ["outside", "symlink", "hardlink", "protected", "fifo", "oversized"]
)
def test_worker_rejects_unsafe_or_unbounded_reads(worker_request, kind):
    work = Path(worker_request["context"]["workspace"])
    outside = work.parent / "secret"
    outside.write_text("never-return-this")
    path = work / "sample.txt"
    if kind == "outside":
        worker_request["args"]["path"] = str(outside)
    elif kind == "symlink":
        path.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, path)
    elif kind == "protected":
        path.write_text("never-return-this")
        worker_request["context"]["denied_paths"] = [str(path)]
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        with path.open("wb") as handle:
            handle.truncate(worker.MAX_FILE_BYTES + 1)
    result = invoke(worker_request)
    assert not result.success and "never-return-this" not in result.model_content()
    assert outside.read_text() == "never-return-this"


def test_readonly_patch_and_search_bounds(worker_request):
    work = Path(worker_request["context"]["workspace"])
    (work / "sample.txt").write_text("match one\nmatch two\n")
    (work / "hidden.txt").write_text("match secret\n")
    denied = invoke(
        request_for(
            worker_request,
            "apply_patch",
            {"path": "sample.txt", "old_text": "one", "new_text": "changed"},
            read_only=True,
        )
    )
    assert not denied.success and "changed" not in (work / "sample.txt").read_text()
    context = {"denied_paths": [str(work / "hidden.txt")]}
    found = invoke(request_for(worker_request, "search", {"query": "match"}, **context))
    assert found.success and found.metadata["result_count"] == 2
    assert "secret" not in found.output
    assert found.progress.evidence_complete
    limited = invoke(
        request_for(worker_request, "search", {"query": "match", "max_results": 1}, **context)
    )
    assert limited.truncated and not limited.progress.evidence_complete
    bounded = invoke(
        request_for(worker_request, "search", {"query": "match"}, max_output_bytes=8, **context)
    )
    assert bounded.truncated and len(bounded.output.encode()) <= 8


@pytest.mark.parametrize(
    "change", ["operation", "version", "bool", "extra", "context", "timeout", "surrogate"]
)
def test_request_shape_rejected_before_execution(worker_request, change):
    if change == "operation":
        worker_request["op"] = "eval"
    elif change == "version":
        worker_request["v"] = True
    elif change == "bool":
        worker_request["args"]["start_line"] = True
    elif change == "extra":
        worker_request["args"]["python"] = "print('bad')"
    elif change == "context":
        worker_request["context"]["workspace"] = "relative"
    elif change == "timeout":
        worker_request.update(
            op="fetch", args={"url": "https://example.com", "timeout_seconds": float("nan")}
        )
    else:
        worker_request["args"]["path"] = "bad\ud800"
    with pytest.raises(SandboxError) as exc:
        encode_request(worker_request)
    assert exc.value.executed is False and exc.value.stage == "worker_request"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{}",
        b"{}\n{}\n",
        b'{"v":1,"v":1}\n',
        b'{"v":NaN}\n',
        b"\xff\n",
        b"[1]\n",
        b"{" + b" " * worker.MAX_RESPONSE_BYTES + b"}\n",
    ],
)
def test_malformed_response_never_becomes_success(raw):
    with pytest.raises(SandboxError) as exc:
        decode_response(raw, request_id="request-1", operation="read")
    assert exc.value.code.value == "TOOL_PROTOCOL_ERROR" and exc.value.executed is None


@pytest.mark.parametrize(
    "change",
    ["id", "version", "status", "success", "authority", "progress", "result_type", "budget"],
)
def test_worker_reply_cannot_forge_host_control(worker_request, change):
    work = Path(worker_request["context"]["workspace"])
    (work / "sample.txt").write_text("data")
    reply = worker.handle_request(worker_request)
    if change == "id":
        reply["id"] = "someone-else"
    elif change == "version":
        reply["v"] = True
    elif change == "status":
        reply["result"]["status"] = "running"
    elif change == "success":
        reply["result"]["success"] = "true"
    elif change == "authority":
        reply["result"]["metadata"]["executed"] = False
    elif change == "progress":
        reply["result"]["progress"]["kind"] = "strong"
    elif change == "result_type":
        reply["result"] = []
    with pytest.raises(SandboxError):
        decode_response(
            json.dumps(reply).encode() + b"\n",
            request_id="request-1",
            operation="read",
            output_limit=1 if change == "budget" else worker.MAX_OUTPUT_BYTES,
        )


def test_standalone_worker_ignores_project_modules_and_rejects_second_request(
    worker_request, monkeypatch
):
    work = Path(worker_request["context"]["workspace"])
    installed = work.parent / "trusted-worker.py"
    shutil.copyfile(worker.__file__, installed)
    marker = work / "injected"
    for name in ("json.py", "ssl.py", "sitecustomize.py", "socket.py"):
        (work / name).write_text(
            f"from pathlib import Path; Path({str(marker)!r}).touch(); "
            "raise RuntimeError('injected')"
        )
    monkeypatch.setenv("PYTHONPATH", str(work))
    (work / "sample.txt").write_text("standalone")
    argv = [sys.executable, "-I", "-S", str(installed)]
    raw = encode_request(worker_request)
    completed = subprocess.run(argv, input=raw, cwd=work, capture_output=True, timeout=5)
    assert completed.returncode == 0, completed.stderr
    assert (
        invoke(worker_request).output
        == decode_response(completed.stdout, request_id="request-1", operation="read").output
    )
    assert not marker.exists()
    patch = encode_request(
        request_for(
            worker_request,
            "apply_patch",
            {"path": "sample.txt", "old_text": "standalone", "new_text": "bad"},
        )
    )
    for malformed in (patch + patch, patch[:-1], b"x" * (worker.MAX_REQUEST_BYTES + 1) + b"\n"):
        completed = subprocess.run(argv, input=malformed, cwd=work, capture_output=True, timeout=5)
        assert completed.returncode == 65 and not completed.stdout
        assert (work / "sample.txt").read_text() == "standalone"


def test_fetch_requires_proxy_checks_redirects_and_bounds_response(worker_request, monkeypatch):
    requests = []

    class Proxy(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Proxy-Authorization")))
            self.send_response(302 if self.path.endswith("/redirect") else 200)
            if self.path.endswith("/redirect"):
                self.send_header("Location", "http://blocked.example/secret")
            elif self.path.endswith("/short"):
                self.send_header("Content-Length", "100")
            elif self.path.endswith("/compressed"):
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.wfile.write(b"bounded-body")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("HTTP_PROXY", f"http://user:pass@127.0.0.1:{server.server_port}")
        context = {
            "network_mode": "allowlist",
            "allowed_domains": ["allowed.example"],
            "max_output_bytes": 4,
        }
        request = request_for(
            worker_request, "fetch", {"url": "http://allowed.example/data"}, **context
        )
        result = invoke(request)
        assert result.success and result.output == "boun" and result.truncated
        assert requests == [("http://allowed.example/data", "Basic dXNlcjpwYXNz")]
        redirect = invoke(
            request_for(
                worker_request, "fetch", {"url": "http://allowed.example/redirect"}, **context
            )
        )
        assert not redirect.success and len(requests) == 2
        for suffix in ("short", "compressed"):
            malformed = invoke(
                request_for(
                    worker_request,
                    "fetch",
                    {"url": f"http://allowed.example/{suffix}"},
                    **(context | {"max_output_bytes": 100}),
                )
            )
            assert not malformed.success
        denied = invoke(
            request_for(worker_request, "fetch", {"url": "http://allowed.example/data"})
        )
        assert not denied.success and len(requests) == 4
        monkeypatch.delenv("HTTP_PROXY")
        assert not invoke(request).success and len(requests) == 4
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
