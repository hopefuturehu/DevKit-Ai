"""Real AgentRunner + real tools behind FastAPI, with deterministic model responses."""

import asyncio
import json
import sys
import time

import pytest
from fastapi.testclient import TestClient

from bot.core.models import ModelCapabilities, ModelEvent, ModelEventKind
from bot.execution import ProcessSpec
from bot.web.server import create_app


class ControlledProvider:
    def __init__(self, **kwargs):
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.commands = []
        self.requests = []
        self.first = True

    def capabilities(self, model):
        return ModelCapabilities()

    async def stream(self, request):
        self.requests.append(request)
        if self.first:
            self.first = False
            self.entered.set()
            yield ModelEvent(kind=ModelEventKind.USAGE, input_tokens=20, output_tokens=5)
            if self.commands:
                yield ModelEvent(
                    kind=ModelEventKind.TOOL_CALL_DELTA,
                    tool_index=0,
                    tool_call_id="command",
                    tool_name="run_command",
                    arguments_delta=json.dumps({"argv": self.commands, "wait_seconds": 60}),
                )
                yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="tool_calls")
                return
            yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="正在检查。")
            await self.gate.wait()
            yield ModelEvent(
                kind=ModelEventKind.TOOL_CALL_DELTA,
                tool_index=0,
                tool_call_id="write",
                tool_name="apply_patch",
                arguments_delta=json.dumps(
                    {
                        "path": "result.txt",
                        "old_text": "",
                        "new_text": "actual artifact\n",
                        "create": True,
                    }
                ),
            )
            yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="tool_calls")
        else:
            yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="已完成，文件已生成。")
            yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_MODEL_API_KEY", "test-key-not-real")
    monkeypatch.setattr("bot.cli.runtime.OpenAICompatibleProvider", ControlledProvider)
    config = tmp_path / ".bot/config.toml"
    config.parent.mkdir()
    config.write_text(
        '[model]\nname="test-model"\napi_key_ref="env:BOT_MODEL_API_KEY"\n'
        "[memory]\nenabled=false\n[permissions]\nauto_approve=true\n"
        "[subagents]\nenabled=false\n"
        '[storage]\nstate_path="./.bot/state.db"\n'
    )
    app = create_app(tmp_path, config)
    with TestClient(app) as client:
        yield client


def until(fn, predicate=lambda v: bool(v), seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = fn()
        if predicate(value):
            return value
        time.sleep(0.01)
    raise AssertionError(f"Condition did not become true: {value!r}")


def start(client):
    session = client.post("/api/sessions").json()["session_id"]
    response = client.post(
        f"/api/sessions/{session}/runs",
        json={"prompt": "write artifact", "request_id": "request-1"},
    )
    assert response.status_code == 202, response.text
    run = response.json()["run_id"]
    until(lambda: client.get(f"/api/runs/{run}").json(), lambda r: r.get("status") == "running")
    return session, run


def read_snapshot(ws):
    events = []
    while True:
        message = ws.receive_json()
        if message["type"] == "snapshot":
            events.extend(message["events"])
        if message["type"] == "snapshot_complete":
            return events, message["cursor"]


def test_disconnect_resume_steer_and_artifact_delivery(client):
    session, run = start(client)
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "subscribe", "session_id": session, "subscription_id": "one"})
        events, cursor = read_snapshot(ws)
        assert any(e["type"] == "run.started" for e in events)
    # The model is still awaiting its next event after the socket is closed.
    assert client.get(f"/api/runs/{run}").json()["status"] == "running"
    duplicate = client.post(
        f"/api/sessions/{session}/runs",
        json={"prompt": "write artifact", "request_id": "request-1"},
    )
    assert duplicate.json()["duplicate"] is True
    collision = client.post(
        f"/api/sessions/{session}/runs", json={"prompt": "another", "request_id": "request-2"}
    )
    assert collision.status_code == 400
    steer = {"text": "include a result file", "message_id": "message-1"}
    assert client.post(f"/api/runs/{run}/steer", json=steer).status_code == 202
    assert client.post(f"/api/runs/{run}/steer", json=steer).status_code == 202
    service = client.app.state.workbench
    client.portal.call(service.runtime.runner.provider.gate.set)
    until(lambda: client.get(f"/api/runs/{run}").json(), lambda r: r.get("status") == "completed")
    with client.websocket_connect("/ws") as ws:
        ws.send_json(
            {"type": "subscribe", "session_id": session, "cursor": cursor, "subscription_id": "two"}
        )
        resumed, _ = read_snapshot(ws)
        assert any(e["type"] == "run.finished" for e in resumed)
        assert sum(e["type"] == "run.steer.queued" for e in resumed) == 1
        assert any(
            e["type"] == "run.steered" and e["payload"]["message_id"] == "message-1"
            for e in resumed
        )
    artifacts = until(
        lambda: client.get(f"/api/runs/{run}/artifacts").json(), lambda r: bool(r["artifacts"])
    )
    record = next(a for a in artifacts["artifacts"] if a["path"] == "result.txt")
    detail = client.get(f"/api/runs/{run}/artifacts/{record['id']}").json()
    assert "+actual artifact" in detail["diff"]
    output = client.get(f"/api/runs/{run}/artifacts/{record['id']}/content?download=true")
    assert output.text == "actual artifact\n"
    assert output.headers["content-disposition"].startswith("attachment")
    assert client.get(f"/api/runs/{run}/tools/write").json()["full_output_available"]


def test_stop_terminates_only_owned_process_and_preserves_usage(client, tmp_path):
    service = client.app.state.workbench
    provider = service.runtime.runner.provider
    provider.commands = [
        sys.executable,
        "-c",
        "import time;print('ready',flush=True);time.sleep(60)",
    ]

    async def unrelated():
        return await service.runtime.target.start_process(
            ProcessSpec(
                argv=[sys.executable, "-c", "import time;time.sleep(60)"],
                cwd=tmp_path,
                session_id="other-session",
                run_id="other-run",
            )
        )

    other = client.portal.call(unrelated)
    session, run = start(client)
    until(lambda: client.get(f"/api/runs/{run}/processes").json(), lambda rows: bool(rows))
    assert client.post(f"/api/runs/{run}/cancel").status_code == 202
    final = until(
        lambda: client.get(f"/api/runs/{run}").json(), lambda r: r.get("status") == "cancelled"
    )
    assert final["input_tokens"] == 20 and final["output_tokens"] == 5
    processes = client.get(f"/api/runs/{run}/processes").json()
    assert all(p["status"] == "cancelled" for p in processes)

    async def other_status():
        return await service.runtime.target.poll_process(other, consume_output=False)

    assert client.portal.call(other_status).status.value == "running"
    assert (
        client.post(
            f"/api/runs/{run}/steer", json={"text": "too late", "message_id": "late"}
        ).status_code
        == 409
    )


def test_http_and_websocket_subscription_boundaries(client, tmp_path):
    service = client.app.state.workbench
    session = client.post("/api/sessions").json()["session_id"]
    foreign = service.runtime.store.create_session(tmp_path / "different")
    assert client.get(f"/api/sessions/{foreign}").status_code == 404
    assert (
        client.post("/api/sessions", headers={"Origin": "https://elsewhere.example"}).status_code
        == 403
    )
    with client.websocket_connect("/ws") as ws:
        ws.send_text("not json")
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "subscribe", "session_id": session, "subscription_id": "good"})
        read_snapshot(ws)
        ws.send_json({"type": "subscribe", "session_id": foreign})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "subscribe", "session_id": session, "cursor": "old:0"})
        assert ws.receive_json()["code"] == "cursor_expired"
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
