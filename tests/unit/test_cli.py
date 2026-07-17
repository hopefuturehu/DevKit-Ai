from pathlib import Path

from typer.testing import CliRunner

from bot.cli.app import app

runner = CliRunner()


def test_cli_recognizes_management_commands_before_natural_language(tmp_path: Path) -> None:
    initialized = runner.invoke(app, ["-C", str(tmp_path), "init"])
    assert initialized.exit_code == 0, initialized.output
    assert (tmp_path / ".bot" / "config.toml").exists()
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


def test_cli_help_and_version_do_not_require_model_configuration() -> None:
    help_result = runner.invoke(app, ["--help"])
    version_result = runner.invoke(app, ["--version"])

    assert help_result.exit_code == 0
    assert "doctor" in help_result.output
    assert version_result.exit_code == 0
    assert "0.1.0" in version_result.output
