import asyncio

from fastapi.testclient import TestClient

from bot.web.server import WebApprovalHandler, create_app


def _write_config(tmp_path):
    """Write a minimal config.toml so build_runtime can start offline."""
    cfg_dir = tmp_path / ".bot"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.toml").write_text(
        "[model]\n"
        'base_url = "https://api.example.com/v1"\n'
        'api_key_ref = "env:BOT_MODEL_API_KEY"\n'
        'name = "test-model"\n'
        "\n"
        "[skills]\n"
        'path = "./skills"\n'
        "\n"
        "[storage]\n"
        'state_path = "./.bot/state.db"\n',
        encoding="utf-8",
    )
    return cfg_dir / "config.toml"


def test_web_approval_handler_resolves_pending_decision() -> None:
    async def scenario() -> None:
        handler = WebApprovalHandler()
        task = asyncio.create_task(handler.approve(None, None))
        await asyncio.sleep(0.01)
        assert handler.resolve_next(True)
        response = await task
        assert response.approved is True
        assert response.scope.value == "once"
        # No pending decision left: resolve returns False.
        assert handler.resolve_next(False) is False

    asyncio.run(scenario())


def test_create_app_starts_and_serves_rest_endpoints(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOT_MODEL_API_KEY", "test-key-not-real")
    config_path = _write_config(tmp_path)

    app = create_app(workspace=tmp_path, config_path=config_path)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200

        status = client.get("/api/status").json()
        assert status["model"] == "test-model"
        assert status["permission_mode"] == "safe"
        assert "active_skills" in status

        assert client.get("/api/sessions").status_code == 200
        tools = client.get("/api/tools").json()
        assert isinstance(tools, list) and tools

        created = client.post("/api/sessions").json()
        sid = created["session_id"]
        detail = client.get(f"/api/sessions/{sid}").json()
        assert detail["session_id"] == sid
        assert detail["messages"] == []
        assert detail["plan"] is None
        assert client.get("/api/sessions/missing").status_code == 404


def test_websocket_ping_session_and_approval(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOT_MODEL_API_KEY", "test-key-not-real")
    config_path = _write_config(tmp_path)

    app = create_app(workspace=tmp_path, config_path=config_path)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_text('{"type":"ping"}')
            assert ws.receive_json()["type"] == "pong"

            ws.send_text('{"type":"create_session"}')
            created = ws.receive_json()
            assert created["type"] == "session_created"
            assert created["session_id"]
            assert created["plan"] is None

            ws.send_text('{"type":"approval","decision":"approve"}')
            assert ws.receive_json()["type"] == "approval_resolved"
