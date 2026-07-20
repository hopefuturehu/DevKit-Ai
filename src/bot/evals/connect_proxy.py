from __future__ import annotations

import selectors
import socket
import socketserver
import threading
from collections.abc import Iterator
from contextlib import contextmanager


def _connect_target_allowed(request_line: str, allowed_host: str, allowed_port: int) -> bool:
    parts = request_line.split()
    expected_target = f"{allowed_host.lower()}:{allowed_port}"
    return (
        len(parts) == 3
        and parts[0] == "CONNECT"
        and parts[1].lower() == expected_target
        and parts[2].startswith("HTTP/")
    )


class _RestrictedConnectServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, allowed_host: str, allowed_port: int) -> None:
        self.allowed_host = allowed_host.lower()
        self.allowed_port = allowed_port
        super().__init__(("0.0.0.0", 0), _RestrictedConnectHandler)


class _RestrictedConnectHandler(socketserver.BaseRequestHandler):
    server: _RestrictedConnectServer

    def handle(self) -> None:
        request = self._read_headers()
        if request is None:
            return
        request_line = request.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
        if not _connect_target_allowed(
            request_line, self.server.allowed_host, self.server.allowed_port
        ):
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            return

        try:
            upstream = socket.create_connection(
                (self.server.allowed_host, self.server.allowed_port), timeout=30
            )
        except OSError:
            self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            return

        with upstream:
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self._relay(upstream)

    def _read_headers(self) -> bytes | None:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(4096)
            if not chunk:
                return None
            data.extend(chunk)
            if len(data) > 16 * 1024:
                self.request.sendall(
                    b"HTTP/1.1 431 Request Header Fields Too Large\r\nConnection: close\r\n\r\n"
                )
                return None
        return bytes(data)

    def _relay(self, upstream: socket.socket) -> None:
        selector = selectors.DefaultSelector()
        selector.register(self.request, selectors.EVENT_READ, upstream)
        selector.register(upstream, selectors.EVENT_READ, self.request)
        try:
            while True:
                for key, _ in selector.select():
                    source = key.fileobj
                    destination = key.data
                    data = source.recv(64 * 1024)
                    if not data:
                        return
                    destination.sendall(data)
        finally:
            selector.close()


@contextmanager
def restricted_connect_proxy(allowed_host: str, allowed_port: int = 443) -> Iterator[int]:
    """Expose a temporary CONNECT proxy restricted to one upstream TLS endpoint."""
    server = _RestrictedConnectServer(allowed_host, allowed_port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
