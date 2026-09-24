from __future__ import annotations

from collections.abc import Iterable

from bot.core.models import ToolDefinition
from bot.tools.base import Tool


class ToolRegistry:
    def __init__(self, *, requires_confinement: bool = False) -> None:
        self._tools: dict[str, Tool] = {}
        self._requires_confinement = requires_confinement

    @property
    def requires_confinement(self) -> bool:
        return self._requires_confinement

    def require_confinement(self) -> None:
        """Validate the complete existing inventory before enabling strict registration."""
        from bot.tools.native_boundary import assert_native_tool_supported

        for tool in self._tools.values():
            assert_native_tool_supported(tool, stage="tool_registration")
        self._requires_confinement = True

    def _validate(self) -> None:
        if self._requires_confinement:
            self.require_confinement()

    def register(self, tool: Tool) -> None:
        if self._requires_confinement:
            from bot.tools.native_boundary import assert_native_tool_supported

            assert_native_tool_supported(tool, stage="tool_registration")
        if tool.name in self._tools:
            raise ValueError(f"Tool 名称重复: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        self._validate()
        return self._tools.get(name)

    def definitions(self) -> list[ToolDefinition]:
        self._validate()
        return [tool.definition for tool in self._tools.values()]

    def names(self) -> list[str]:
        self._validate()
        return list(self._tools)

    def subset(self, names: Iterable[str]) -> ToolRegistry:
        self._validate()
        selected = ToolRegistry(requires_confinement=self._requires_confinement)
        for name in dict.fromkeys(names):
            tool = self._tools.get(name)
            if tool is None:
                raise ValueError(f"未知 Tool: {name}")
            selected.register(tool)
        return selected

    def __iter__(self):
        self._validate()
        return iter(self._tools.values())
