from pathlib import Path

import pytest

from bot.config import (
    ConfigError,
    load_config,
    resolve_api_key,
    set_config_value,
)


def test_config_precedence_and_relative_skill_path(tmp_path: Path, monkeypatch) -> None:
    project_config = tmp_path / ".bot" / "config.toml"
    project_config.parent.mkdir()
    project_config.write_text(
        """
[model]
base_url = "https://project.example/v1"
name = "project-model"

[skills]
path = "domain-skills"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("BOT_MODEL_NAME", "environment-model")

    config = load_config(tmp_path)

    assert config.model.base_url == "https://project.example/v1"
    assert config.model.name == "environment-model"
    assert config.skill_path(tmp_path) == (tmp_path / "domain-skills").resolve()


def test_config_rejects_unknown_fields(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[model]\nunknown = true\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="配置校验失败"):
        load_config(tmp_path, config_path=config_path)


def test_api_key_must_be_an_environment_reference(monkeypatch) -> None:
    monkeypatch.setenv("TEST_BOT_KEY", "secret")
    assert resolve_api_key("env:TEST_BOT_KEY") == "secret"
    with pytest.raises(ConfigError, match="不允许在配置中保存明文"):
        resolve_api_key("secret")


def test_config_writer_is_atomic_and_validates_values(tmp_path: Path) -> None:
    path = tmp_path / ".bot" / "config.toml"
    set_config_value(path, "model.name", "model-a")
    set_config_value(path, "permissions.workspace_only", False)

    config = load_config(tmp_path, config_path=path)
    assert config.model.name == "model-a"
    assert config.permissions.workspace_only is False
    with pytest.raises(ConfigError, match="不允许把明文"):
        set_config_value(path, "model.api_key", "secret")
