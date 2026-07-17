from __future__ import annotations

import json
import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from bot.config.loader import ConfigError
from bot.config.models import AppConfig


def config_target(workspace: Path, explicit_path: Path | None = None) -> Path:
    return (
        explicit_path.resolve() if explicit_path else workspace.resolve() / ".bot" / "config.toml"
    )


def parse_config_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def set_config_value(path: Path, dotted_key: str, value: Any) -> None:
    if not dotted_key or any(not part for part in dotted_key.split(".")):
        raise ConfigError("配置键必须是 section.key 形式")
    if dotted_key in {"model.api_key", "model.api_key_value"}:
        raise ConfigError("不允许把明文 API Key 写入配置，请使用 model.api_key_ref")
    data: dict[str, Any] = {}
    if path.exists():
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"无法读取配置 {path}: {exc}") from exc
    target: dict[str, Any] = data
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        current = target.setdefault(part, {})
        if not isinstance(current, dict):
            raise ConfigError(f"配置路径 {part} 不是对象")
        target = current
    target[parts[-1]] = value
    try:
        AppConfig.model_validate(data)
    except Exception as exc:
        raise ConfigError(f"配置值不合法: {exc}") from exc
    _atomic_write(path, tomli_w.dumps(data))


def get_config_value(data: dict[str, Any], dotted_key: str) -> Any:
    value: Any = data
    for part in dotted_key.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ConfigError(f"配置键不存在: {dotted_key}")
        value = value[part]
    return value


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        temp_path = Path(temp_name)
        if temp_path.exists():
            temp_path.unlink()
