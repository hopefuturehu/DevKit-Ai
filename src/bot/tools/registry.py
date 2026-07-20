from __future__ import annotations

from collections.abc import Iterable

from bot.core.models import ToolDefinition
from bot.tools.base import Tool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool 名称重复: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def definitions(self) -> list[ToolDefinition]:
        return [tool.definition for tool in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)

    def subset(self, names: Iterable[str]) -> ToolRegistry:
        selected = ToolRegistry()
        for name in dict.fromkeys(names):
            tool = self._tools.get(name)
            if tool is None:
                raise ValueError(f"未知 Tool: {name}")
            selected.register(tool)
        return selected

    def __iter__(self):
        return iter(self._tools.values())
