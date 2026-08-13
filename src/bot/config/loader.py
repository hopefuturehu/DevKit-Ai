from __future__ import annotations

import os
import re
import tomllib
from copy import deepcopy
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from bot.config.models import AppConfig


class ConfigError(RuntimeError):
    pass


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
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
    data: dict[str, Any] = {}
    if config_path:
        data = _deep_merge(data, _read_toml(config_path.resolve()))
    else:
        data = _deep_merge(data, _read_toml(Path("~/.bot/config.toml").expanduser()))
        data = _deep_merge(data, _read_toml(workspace / ".bot" / "config.toml"))
    data = _deep_merge(data, _environment_overlay())
    data = _deep_merge(data, overrides or {})
    try:
        return AppConfig.model_validate(data)
    except Exception as exc:
        raise ConfigError(f"配置校验失败: {exc}") from exc


def _reference_variable(reference: str, prefix: str) -> str:
    variable = reference.removeprefix(prefix)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
        raise ConfigError(f"API Key 引用中的变量名无效: {variable or '<empty>'}")
    return variable


def resolve_api_key(reference: str, *, workspace: Path | None = None) -> str:
    if reference.startswith("dotenv:"):
        variable = _reference_variable(reference, "dotenv:")
        if workspace is None:
            raise ConfigError("解析 dotenv: API Key 引用时必须提供 workspace")
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
    if reference.startswith("env:"):
        variable = _reference_variable(reference, "env:")
        value = os.environ.get(variable)
        if not value:
            raise ConfigError(f"环境变量 {variable} 未设置")
        return value
    raise ConfigError(
        "api_key_ref 只支持 dotenv:<VARIABLE> 或 env:<VARIABLE>，不允许在配置中保存明文密钥"
    )
