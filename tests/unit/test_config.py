import os
from pathlib import Path

import pytest

from bot.config import (
    ConfigError,
    api_key_reference_variable,
    load_config,
    resolve_api_key,
    resolve_model_api_key,
    set_config_value,
)
from bot.config.models import AppConfig


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


def test_auto_approve_can_be_enabled_from_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BOT_AUTO_APPROVE", "true")

    config = load_config(tmp_path)

    assert config.permissions.auto_approve is True


def test_config_rejects_unknown_fields(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[model]\nunknown = true\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="配置校验失败"):
        load_config(tmp_path, config_path=config_path)


def test_config_rejects_reserves_larger_than_model_context_window(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[model]
context_window_tokens = 8000
max_output_tokens = 7000

[context]
protocol_reserve_tokens = 1000
safety_margin_tokens = 1000
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="reserve"):
        load_config(tmp_path, config_path=config_path)


def test_api_key_supports_dotenv_and_environment_references(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".env").write_text(
        'TEST_DOTENV_KEY="dotenv secret"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("TEST_DOTENV_KEY", raising=False)
    monkeypatch.setenv("TEST_BOT_KEY", "secret")

    assert resolve_api_key("dotenv:TEST_DOTENV_KEY", workspace=tmp_path) == "dotenv secret"
    assert "TEST_DOTENV_KEY" not in os.environ
    assert resolve_api_key("env:TEST_BOT_KEY") == "secret"
    monkeypatch.setenv("TEST_DOTENV_KEY", "environment secret")
    assert resolve_api_key("auto:TEST_DOTENV_KEY", workspace=tmp_path) == "environment secret"
    monkeypatch.delenv("TEST_DOTENV_KEY")
    assert resolve_api_key("auto:TEST_DOTENV_KEY", workspace=tmp_path) == "dotenv secret"
    assert "TEST_DOTENV_KEY" not in os.environ
    with pytest.raises(ConfigError, match="不允许在配置中保存明文"):
        resolve_api_key("secret")


def test_model_api_key_supports_direct_value_and_reference_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "environment-secret")
    direct = AppConfig.model_validate(
        {"model": {"api_key": "toml-secret", "api_key_ref": "env:TEST_MODEL_KEY"}}
    )
    referenced = AppConfig.model_validate({"model": {"api_key_ref": "env:TEST_MODEL_KEY"}})

    assert resolve_model_api_key(direct.model, workspace=tmp_path) == "toml-secret"
    assert resolve_model_api_key(referenced.model, workspace=tmp_path) == "environment-secret"
    assert "toml-secret" not in repr(direct.model)


def test_dotenv_api_key_reports_missing_file_and_variable(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\.env 文件不存在"):
        resolve_api_key("dotenv:BOT_MODEL_API_KEY", workspace=tmp_path)

    (tmp_path / ".env").write_text("OTHER_KEY=value\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="未设置 BOT_MODEL_API_KEY"):
        resolve_api_key("dotenv:BOT_MODEL_API_KEY", workspace=tmp_path)


def test_api_key_reference_rejects_invalid_variable_name(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="变量名无效"):
        resolve_api_key("dotenv:not-valid!", workspace=tmp_path)
    assert api_key_reference_variable("auto:BOT_MODEL_API_KEY") == "BOT_MODEL_API_KEY"
    with pytest.raises(ConfigError, match="只支持 auto"):
        api_key_reference_variable("file:BOT_MODEL_API_KEY")


def test_subagent_limits_are_strictly_validated(tmp_path: Path) -> None:
    config = load_config(
        tmp_path,
        overrides={
            "subagents": {
                "max_concurrent": 4,
                "max_queued": 20,
                "allow_worktree_writes": False,
            }
        },
    )
    assert config.subagents.max_concurrent == 4
    assert config.subagents.max_queued == 20
    assert config.subagents.allow_worktree_writes is False

    with pytest.raises(ConfigError, match="配置校验失败"):
        load_config(tmp_path, overrides={"subagents": {"max_concurrent": 0}})
    with pytest.raises(ConfigError, match="worktree_dir"):
        load_config(tmp_path, overrides={"subagents": {"worktree_dir": "../escape"}})
    with pytest.raises(ConfigError, match="memory.path"):
        load_config(tmp_path, overrides={"memory": {"path": "."}})


def test_project_agent_path_cannot_escape_through_symlink(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-agents"
    outside.mkdir()
    project_bot = tmp_path / ".bot"
    project_bot.mkdir()
    try:
        (project_bot / "agents").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前环境不允许创建符号链接")

    with pytest.raises(ValueError, match="符号链接逃逸"):
        AppConfig().project_agent_path(tmp_path)


def test_execution_has_no_fixed_global_limit_by_default() -> None:
    config = AppConfig()

    assert config.model.api_key_ref == "auto:BOT_MODEL_API_KEY"
    assert config.permissions.auto_approve is False
    assert config.agent.max_steps is None
    assert config.agent.max_wall_time_seconds is None
    assert config.agent.max_total_tool_output_bytes is None
    assert config.agent.max_consecutive_failures is None
    assert config.agent.model_request_retries == 2
    assert config.agent.model_request_retry_backoff_seconds == 1
    assert config.agent.process_hard_timeout_seconds is None
    assert config.subagents.max_steps is None
    assert config.subagents.max_wall_time_seconds is None
    assert config.context.compaction_request_timeout_seconds == 90
    assert config.context.compaction_command_max_requests == 8
    assert config.context.compaction_command_max_seconds == 600
    assert config.context.recent_conversation_tokens == 20_000
    assert config.context.compaction_summary_target_tokens is None
    assert config.context.compaction_summary_tokens == 4_000
    assert config.context.compaction_source_refs == "range"
    assert config.context.compaction_strategy == "a_fallback"
    assert config.context.compaction_thinking == "auto"


def test_compaction_summary_target_cannot_exceed_visible_hard_limit() -> None:
    with pytest.raises(ValueError, match="summary_target_tokens"):
        AppConfig.model_validate(
            {
                "context": {
                    "compaction_summary_target_tokens": 4_001,
                    "compaction_summary_tokens": 4_000,
                }
            }
        )


def test_progress_thresholds_must_be_ordered() -> None:
    with pytest.raises(ValueError, match="严格递增"):
        AppConfig.model_validate(
            {
                "agent": {
                    "progress": {
                        "warning_after_no_progress_steps": 5,
                        "recovery_after_no_progress_steps": 4,
                        "finalize_after_no_progress_steps": 6,
                    }
                }
            }
        )

    assert AppConfig().agent.progress.process_inactivity_finalize_seconds is None
    with pytest.raises(ValueError, match="process inactivity"):
        AppConfig.model_validate(
            {
                "agent": {
                    "progress": {
                        "process_inactivity_warning_seconds": 10,
                        "process_inactivity_recovery_seconds": 5,
                    }
                }
            }
        )
    with pytest.raises(ValueError, match="process inactivity"):
        AppConfig.model_validate(
            {
                "agent": {
                    "progress": {
                        "process_inactivity_warning_seconds": 1,
                        "process_inactivity_recovery_seconds": 5,
                        "process_inactivity_finalize_seconds": 4,
                    }
                }
            }
        )


def test_config_writer_is_atomic_and_validates_values(tmp_path: Path) -> None:
    path = tmp_path / ".bot" / "config.toml"
    set_config_value(path, "model.name", "model-a")
    set_config_value(path, "permissions.workspace_only", False)

    config = load_config(tmp_path, config_path=path)
    assert config.model.name == "model-a"
    assert config.permissions.workspace_only is False
    set_config_value(path, "model.api_key", "secret")
    config = load_config(tmp_path, config_path=path)
    assert resolve_model_api_key(config.model, workspace=tmp_path) == "secret"
    assert path.stat().st_mode & 0o077 == 0


def test_config_validation_error_redacts_direct_api_key(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[model]\napi_key = "must-not-leak"\ncontext_window_tokens = 1000\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as captured:
        load_config(tmp_path, config_path=path)

    assert "must-not-leak" not in str(captured.value)
