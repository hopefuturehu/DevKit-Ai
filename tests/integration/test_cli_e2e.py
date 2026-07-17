import json
from collections.abc import AsyncIterator
from pathlib import Path

from typer.testing import CliRunner

from bot.cli.app import app
from bot.core.models import ModelCapabilities, ModelEvent, ModelEventKind, ModelRequest
from bot.providers import ModelProvider


class CliProvider(ModelProvider):
    requests: list[ModelRequest] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities()

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        yield ModelEvent(kind=ModelEventKind.TEXT_DELTA, text="hello from provider")
        yield ModelEvent(kind=ModelEventKind.FINISH, finish_reason="stop")


def test_cli_json_run_connects_config_provider_agent_and_store(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("bot.cli.runtime.OpenAICompatibleProvider", CliProvider)
    CliProvider.requests.clear()
    config_dir = tmp_path / ".bot"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        """
[model]
base_url = "https://provider.example/v1"
api_key_ref = "env:BOT_MODEL_API_KEY"
name = "mock-model"

[storage]
state_path = "./.bot/state.db"
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["-C", str(tmp_path), "run", "say hello", "--json"],
        env={"BOT_MODEL_API_KEY": "test-key"},
    )

    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in result.output.splitlines() if line.startswith("{")]
    assert any(event["type"] == "assistant.delta" for event in events)
    assert events[-1]["type"] == "run.completed"
    assert CliProvider.requests[0].model == "mock-model"
    assert CliProvider.requests[0].messages[-1].content == "say hello"
    assert (tmp_path / ".bot" / "state.db").exists()
