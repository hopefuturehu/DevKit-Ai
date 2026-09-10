from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    arguments: dict[str, Any]


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role
    content: str | None = None
    reasoning_content: str | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)

    def assistant_payload_error(self) -> str | None:
        if self.role != Role.ASSISTANT:
            return None
        if self.content and self.content.strip():
            return None
        if self.tool_calls:
            return None
        return "assistant message must contain non-blank content or tool_calls"

    def to_openai(self) -> dict[str, Any]:
        if error := self.assistant_payload_error():
            raise ValueError(error)
        message: dict[str, Any] = {"role": self.role.value, "content": self.content}
        if self.name and self.role != Role.TOOL:
            message["name"] = self.name
        if self.tool_call_id:
            message["tool_call_id"] = self.tool_call_id
        # DeepSeek thinking mode requires the reasoning generated for an
        # assistant tool-call turn to be returned on subsequent requests. For a
        # final answer without tool calls it is diagnostic-only and is omitted
        # from the provider payload to avoid needlessly expanding context.
        if self.reasoning_content and self.role == Role.ASSISTANT and self.tool_calls:
            message["reasoning_content"] = self.reasoning_content
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": __import__("json").dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in self.tool_calls
            ]
        return message


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    input_schema: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class ModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    messages: list[ChatMessage]
    tools: list[ToolDefinition] = Field(default_factory=list)
    tool_choice: Literal["auto", "required"] | dict[str, Any] | None = None
    temperature: float = 0.2
    max_output_tokens: int | None = None
    thinking: Literal["enabled", "disabled"] | None = None


class ModelEventKind(StrEnum):
    TEXT_DELTA = "text_delta"
    REASONING_DELTA = "reasoning_delta"
    TOOL_CALL_DELTA = "tool_call_delta"
    USAGE = "usage"
    FINISH = "finish"


class InputTokenEstimate(BaseModel):
    """Local prediction and the amount reserved for input, never API usage."""

    model_config = ConfigDict(frozen=True)

    tokens: int = Field(ge=0)
    budget_tokens: int = Field(ge=0)
    source: str
    effective_thinking: str | None = None

    @model_validator(mode="after")
    def validate_budget(self) -> InputTokenEstimate:
        if self.budget_tokens < self.tokens:
            raise ValueError("input budget must include the token estimate")
        return self


class ModelEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ModelEventKind
    text: str | None = None
    tool_index: int | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments_delta: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason: str | None = None
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


class ModelCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text_generation: bool = True
    streaming: bool = True
    structured_tool_calling: bool = True
    named_tool_choice: bool = True
    usage_reporting: bool = False


class RunLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_steps: int | None = Field(default=None, ge=1)
    max_wall_time_seconds: float | None = Field(default=None, gt=0)
    max_input_tokens: int = Field(default=120_000, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    max_tool_output_bytes: int = Field(default=1_000_000, gt=0)
    max_total_tool_output_bytes: int | None = Field(default=None, gt=0)
    max_consecutive_failures: int | None = Field(default=None, ge=1)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str
    session_id: str | None = None
    run_id: str | None = None
    explicit_skills: list[str] = Field(default_factory=list)
    json_output: bool = False


class RunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    status: Literal["completed", "failed", "cancelled", "limit_reached", "blocked"]
    final_text: str = ""
    steps: int = 0
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    termination_reason: str | None = None
