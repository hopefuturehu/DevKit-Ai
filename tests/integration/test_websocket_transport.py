import base64
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


def _write_config(workspace: Path) -> None:
    config_path = workspace / ".bot" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text(
        """[model]
base_url = "https://api.example.com/v1"
api_key_ref = "env:BOT_MODEL_API_KEY"
name = "test-model"

[skills]
path = "./skills"

[storage]
state_path = "./.bot/state.db"

[memory]
enabled = false

[subagents]
enabled = false
""",
        encoding="utf-8",
    )


def _available_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _take_bytes(connection: socket.socket, buffer: bytearray, count: int) -> bytes:
    while len(buffer) < count:
        chunk = connection.recv(4096)
        if not chunk:
            raise ConnectionError("WebSocket connection closed before a complete frame arrived")
        buffer.extend(chunk)
    value = bytes(buffer[:count])
    del buffer[:count]
    return value


def _websocket_ping(port: int) -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=1) as connection:
        websocket_key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET /ws HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {websocket_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        connection.sendall(request.encode("ascii"))

        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = connection.recv(4096)
            if not chunk:
                raise ConnectionError("server closed the WebSocket upgrade response")
            response.extend(chunk)
        headers, remainder = bytes(response).split(b"\r\n\r\n", 1)
        assert headers.startswith(b"HTTP/1.1 101 "), headers.decode(
            "latin-1", errors="replace"
        )
        assert b"upgrade: websocket" in headers.lower()

        payload = json.dumps({"type": "ping"}, separators=(",", ":")).encode()
        mask = os.urandom(4)
        masked_payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        assert len(payload) < 126
        connection.sendall(bytes((0x81, 0x80 | len(payload))) + mask + masked_payload)

        buffer = bytearray(remainder)
        first, second = _take_bytes(connection, buffer, 2)
        assert first & 0x0F == 0x01
        length = second & 0x7F
        if length == 126:
            length = int.from_bytes(_take_bytes(connection, buffer, 2))
        elif length == 127:
            length = int.from_bytes(_take_bytes(connection, buffer, 8))
        response_payload = _take_bytes(connection, buffer, length)
        assert json.loads(response_payload) == {"type": "pong"}


def test_real_uvicorn_websocket_upgrade_and_ping(tmp_path: Path) -> None:
    """Exercise the network/Uvicorn layer that Starlette TestClient bypasses."""
    _write_config(tmp_path)
    port = _available_tcp_port()
    environment = os.environ.copy()
    environment.update(
        {
            "BOT_MODEL_API_KEY": "test-key-not-real",
            "BOT_MODEL_API_KEY_REF": "env:BOT_MODEL_API_KEY",
            "BOT_MODEL_BASE_URL": "https://api.example.com/v1",
            "BOT_MODEL_NAME": "test-model",
            "BOT_SKILLS_PATH": str(tmp_path / "skills"),
            "BOT_STATE_PATH": str(tmp_path / ".bot" / "state.db"),
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "bot.cli.app",
            "-C",
            str(tmp_path),
            "web",
            "start",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    problem: Exception | None = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"Uvicorn exited before accepting connections: {process.returncode}"
                )
            try:
                _websocket_ping(port)
            except (ConnectionRefusedError, ConnectionResetError, TimeoutError, OSError) as exc:
                problem = exc
                time.sleep(0.05)
                continue
            problem = None
            break
        else:
            raise TimeoutError(f"Uvicorn did not accept a WebSocket connection: {problem}")
    except Exception as exc:
        problem = exc
    finally:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()

    if problem is not None:
        raise AssertionError(
            f"real WebSocket transport failed: {problem}\nstdout:\n{stdout}\nstderr:\n{stderr}"
        ) from problem
