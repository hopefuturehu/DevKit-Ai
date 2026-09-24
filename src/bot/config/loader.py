from __future__ import annotations

import os
import re
import tomllib
from copy import deepcopy
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from bot.config.models import AppConfig, ModelConfig, PermissionsConfig
from bot.sandbox.config import (
    PermissionCeiling,
    SandboxConfig,
    SandboxTrust,
    assert_at_most,
    assert_permissions_at_most,
)


class ConfigError(RuntimeError):
    pass


def redact_config_secrets(message: str, data: dict[str, Any]) -> str:
    model = data.get("model")
    if not isinstance(model, dict):
        return message
    api_key = model.get("api_key")
    if isinstance(api_key, str) and api_key:
        return message.replace(api_key, "<redacted>")
    return message


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _read_toml(path: Path, *, required: bool = False) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        if not required and not os.path.lexists(path):
            return {}
        raise ConfigError(f"无法读取配置 {path}: {exc}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"无法读取配置 {path}: {exc}") from exc


def _environment_overlay() -> dict[str, Any]:
    mapping = {
        "BOT_MODEL_BASE_URL": ("model", "base_url"),
        "BOT_MODEL_NAME": ("model", "name"),
        "BOT_MODEL_API_KEY_REF": ("model", "api_key_ref"),
        "BOT_SKILLS_PATH": ("skills", "path"),
        "BOT_STATE_PATH": ("storage", "state_path"),
        "BOT_MEMORY_PATH": ("memory", "path"),
        "BOT_PERMISSION_MODE": ("permissions", "mode"),
        "BOT_AUTO_APPROVE": ("permissions", "auto_approve"),
    }
    result: dict[str, Any] = {}
    for variable, (section, key) in mapping.items():
        if variable in os.environ:
            result.setdefault(section, {})[key] = os.environ[variable]
    return result


def load_config(
    workspace: Path,
    *,
    config_path: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> AppConfig:
    workspace = workspace.resolve()
    trusted_source = Path(os.path.abspath((config_path or Path("~/.bot/config.toml")).expanduser()))
    trusted_path = trusted_source.resolve()
    project_path = None if config_path else workspace / ".bot" / "config.toml"
    trusted_data = _read_toml(trusted_source, required=config_path is not None)
    project_data = _read_toml(project_path) if project_path else {}
    environment = _environment_overlay()
    # Environment and explicit host overrides are trusted configuration inputs.
    # Preserve their existing precedence for ordinary configuration, but never
    # derive the security ceiling from the already-merged project values.
    ceiling_data = _deep_merge(_deep_merge(trusted_data, environment), overrides or {})
    data = _deep_merge(trusted_data, project_data)
    data = _deep_merge(_deep_merge(data, environment), overrides or {})
    try:
        config = AppConfig.model_validate(data)
        ceiling = SandboxConfig.model_validate(ceiling_data.get("sandbox", {}))
        permission_ceiling = PermissionCeiling(
            **PermissionsConfig.model_validate(ceiling_data.get("permissions", {})).model_dump()
        )
        if ceiling.mode == "required" or config.sandbox.mode == "required":
            assert_at_most(config.sandbox, ceiling)
            assert_permissions_at_most(config.permissions, permission_ceiling)
        config._sandbox_trust = SandboxTrust(
            sandbox=ceiling,
            permissions=permission_ceiling,
            trusted_config=trusted_path,
            project_config=project_path,
            require_sandbox=config.sandbox.mode == "required",
            skill_path=ceiling_data.get("skills", {}).get("path"),
            source_paths=tuple(
                sorted(
                    {
                        trusted_source,
                        trusted_path,
                        *((project_path, project_path.resolve()) if project_path else ()),
                    }
                )
            ),
            trusted_overrides=tuple(
                sorted(
                    [
                        f"environment:{section}.{key}"
                        for section, values in environment.items()
                        for key in values
                    ]
                    + [
                        f"override:{section}.{key}"
                        for section, values in (overrides or {}).items()
                        if isinstance(values, dict)
                        for key in values
                    ]
                )
            ),
        )
        return config
    except Exception as exc:
        detail = redact_config_secrets(str(exc), data)
        raise ConfigError(f"配置校验失败: {detail}") from exc


def _reference_variable(reference: str, prefix: str) -> str:
    variable = reference.removeprefix(prefix)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
        raise ConfigError(f"API Key 引用中的变量名无效: {variable or '<empty>'}")
    return variable


def api_key_reference_variable(reference: str) -> str:
    for prefix in ("auto:", "dotenv:", "env:"):
        if reference.startswith(prefix):
            return _reference_variable(reference, prefix)
    raise ConfigError(
        "api_key_ref 只支持 auto:<VARIABLE>、dotenv:<VARIABLE> 或 env:<VARIABLE>，"
        "不允许在配置中保存明文密钥"
    )


def _resolve_dotenv_api_key(variable: str, workspace: Path | None) -> str:
    if workspace is None:
        raise ConfigError("解析 dotenv API Key 引用时必须提供 workspace")
    dotenv_path = workspace.resolve() / ".env"
    if not dotenv_path.is_file():
        raise ConfigError(f".env 文件不存在: {dotenv_path}")
    try:
        value = dotenv_values(dotenv_path).get(variable)
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"无法读取 .env 文件 {dotenv_path}: {exc}") from exc
    if not value:
        raise ConfigError(f".env 文件 {dotenv_path} 中未设置 {variable}")
    return value


def resolve_api_key(reference: str, *, workspace: Path | None = None) -> str:
    variable = api_key_reference_variable(reference)
    if reference.startswith("env:"):
        value = os.environ.get(variable)
        if not value:
            raise ConfigError(f"环境变量 {variable} 未设置")
        return value
    if reference.startswith("auto:"):
        value = os.environ.get(variable)
        if value:
            return value
    return _resolve_dotenv_api_key(variable, workspace)


def resolve_model_api_key(
    model: ModelConfig,
    *,
    workspace: Path | None = None,
) -> str:
    """Resolve a model credential without logging or persisting derived values."""
    if model.api_key:
        return model.api_key.get_secret_value()
    return resolve_api_key(model.api_key_ref, workspace=workspace)
