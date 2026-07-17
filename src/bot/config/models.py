from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelConfig(StrictModel):
    provider: Literal["openai_compatible"] = "openai_compatible"
    base_url: str = ""
    api_key_ref: str = "env:BOT_MODEL_API_KEY"
    name: str = ""
    temperature: float = Field(default=0.2, ge=0, le=2)
    timeout_seconds: float = Field(default=120, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    input_cost_per_million: float | None = Field(default=None, ge=0)
    output_cost_per_million: float | None = Field(default=None, ge=0)


class AgentConfig(StrictModel):
    max_steps: int = Field(default=30, ge=1)
    max_wall_time_seconds: float = Field(default=1800, gt=0)
    max_tool_output_bytes: int = Field(default=1_000_000, gt=0)
    max_consecutive_failures: int = Field(default=3, ge=1)
    max_cost_usd: float | None = Field(default=None, gt=0)


class PermissionsConfig(StrictModel):
    mode: Literal["safe", "read-only", "full-access"] = "safe"
    workspace_only: bool = True
    network: Literal["allow", "ask", "deny"] = "ask"


class ContextConfig(StrictModel):
    max_input_tokens: int = Field(default=120_000, gt=0)
    auto_compact_threshold: float = Field(default=0.8, gt=0, lt=1)


class SkillsConfig(StrictModel):
    path: str = "./skills"
    auto_activate: bool = True
    max_auto_activated: int = Field(default=3, ge=0)
    max_catalog_chars: int = Field(default=8_000, gt=0)


class DisplayConfig(StrictModel):
    tool_output: Literal["summary", "full"] = "summary"
    progress: bool = True


class StorageConfig(StrictModel):
    state_path: str = "~/.bot/state.db"


class AppConfig(StrictModel):
    model: ModelConfig = Field(default_factory=ModelConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)

    def skill_path(self, workspace: Path) -> Path:
        path = Path(self.skills.path).expanduser()
        return path if path.is_absolute() else (workspace / path).resolve()

    def state_path(self, workspace: Path) -> Path:
        path = Path(self.storage.state_path).expanduser()
        return path.resolve() if path.is_absolute() else (workspace / path).resolve()
