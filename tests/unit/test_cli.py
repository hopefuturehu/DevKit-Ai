import os
from pathlib import Path

from typer.testing import CliRunner

from bot.cli.app import app
from bot.config.loader import load_config

runner = CliRunner()


def test_cli_recognizes_management_commands_before_natural_language(tmp_path: Path) -> None:
    initialized = runner.invoke(app, ["-C", str(tmp_path), "init"])
    assert initialized.exit_code == 0, initialized.output
    assert (tmp_path / ".bot" / "config.toml").exists()
    assert load_config(tmp_path).context.compaction_strategy == "a_fallback"
    assert (
        (tmp_path / ".env.example")
        .read_text(encoding="utf-8")
        .endswith("BOT_MODEL_API_KEY=your-api-key\n")
    )
    assert 'api_key_ref = "auto:BOT_MODEL_API_KEY"' in (
        tmp_path / ".bot" / "config.toml"
    ).read_text(encoding="utf-8")
    assert "model_request_retries = 2" in (tmp_path / ".bot" / "config.toml").read_text(
        encoding="utf-8"
    )
    assert "auto_approve = false" in (tmp_path / ".bot" / "config.toml").read_text(encoding="utf-8")
    assert "[agents]" in (tmp_path / ".bot" / "config.toml").read_text(encoding="utf-8")
    if os.name == "posix":
        assert (tmp_path / ".bot" / "config.toml").stat().st_mode & 0o077 == 0
    assert (tmp_path / "skills" / "kunpeng-performance-analysis" / "SKILL.md").is_file()

    updated = runner.invoke(
        app,
        ["-C", str(tmp_path), "config", "set", "model.name", "test-model"],
    )
    assert updated.exit_code == 0, updated.output
    fetched = runner.invoke(
        app,
        ["-C", str(tmp_path), "config", "get", "model.name"],
    )
    assert fetched.exit_code == 0, fetched.output
    assert "test-model" in fetched.output

    local_state = runner.invoke(
        app,
        [
            "-C",
            str(tmp_path),
            "config",
            "set",
            "storage.state_path",
            "./.bot/state.db",
        ],
    )
    assert local_state.exit_code == 0, local_state.output
    agents = runner.invoke(app, ["-C", str(tmp_path), "agent", "list"])
    assert agents.exit_code == 0, agents.output
    assert "explorer" in agents.output
    assert "reviewer" in agents.output
    assert "coder" in agents.output


def test_cli_help_and_version_do_not_require_model_configuration() -> None:
    help_result = runner.invoke(app, ["--help"])
    version_result = runner.invoke(app, ["--version"])

    assert help_result.exit_code == 0
    assert "doctor" in help_result.output
    assert version_result.exit_code == 0
    assert "0.1.0" in version_result.output


def test_config_get_redacts_direct_api_key(tmp_path: Path) -> None:
    config_path = tmp_path / ".bot" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text(
        '[model]\napi_key = "must-not-leak"\nname = "test-model"\n',
        encoding="utf-8",
    )

    result = runner.invoke(app, ["-C", str(tmp_path), "config", "get", "model"])

    assert result.exit_code == 0, result.output
    assert "must-not-leak" not in result.output
    assert "<redacted>" in result.output


def test_doctor_warns_when_environment_and_dotenv_credentials_differ(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / ".bot" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text(
        """[model]
base_url = "https://api.example.com/v1"
name = "example-model"
api_key_ref = "auto:DOCTOR_TEST_KEY"

[storage]
state_path = "./.bot/state.db"
""",
        encoding="utf-8",
    )
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("DOCTOR_TEST_KEY=dotenv-secret\n", encoding="utf-8")
    dotenv_path.chmod(0o644)
    monkeypatch.setenv("DOCTOR_TEST_KEY", "environment-secret")

    result = runner.invoke(app, ["-C", str(tmp_path), "doctor"])

    assert result.exit_code == 0, result.output
    assert "值不同" in result.output
    assert "当前引用会使用环境变量" in result.output
    if os.name == "posix":
        assert "建议执行 chmod 600 .env" in result.output
