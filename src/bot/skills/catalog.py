from __future__ import annotations

import platform
import shutil
from pathlib import Path
from typing import Any

import yaml

from bot.core.models import ToolDefinition
from bot.skills.models import ActiveSkill, Skill, SkillDiagnostic


class SkillCatalog:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.skills: dict[str, Skill] = {}
        self.diagnostics: list[SkillDiagnostic] = []

    def scan(self) -> None:
        self.skills.clear()
        self.diagnostics.clear()
        if not self.root.exists():
            self.diagnostics.append(
                SkillDiagnostic(path=self.root, level="warning", message="Skill 目录不存在")
            )
            return
        if not self.root.is_dir():
            self.diagnostics.append(
                SkillDiagnostic(path=self.root, level="error", message="Skill 路径不是目录")
            )
            return

        discovered: dict[str, list[Skill]] = {}
        for directory in sorted(path for path in self.root.iterdir() if path.is_dir()):
            skill_path = directory / "SKILL.md"
            if not skill_path.is_file():
                continue
            try:
                skill = self._load_skill(skill_path)
                reason = self._ineligible_reason(skill)
                if reason:
                    self.diagnostics.append(
                        SkillDiagnostic(path=skill_path, level="info", message=reason)
                    )
                    continue
                discovered.setdefault(skill.name, []).append(skill)
            except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
                self.diagnostics.append(
                    SkillDiagnostic(path=skill_path, level="error", message=str(exc))
                )

        for name, matches in discovered.items():
            if len(matches) > 1:
                for match in matches:
                    self.diagnostics.append(
                        SkillDiagnostic(
                            path=match.path,
                            level="error",
                            message=f"Skill 名称重复，已禁用: {name}",
                        )
                    )
                continue
            self.skills[name] = matches[0]

    def get(self, name: str) -> Skill | None:
        return self.skills.get(name)

    def summary(self, max_chars: int = 8_000) -> str:
        if not self.skills:
            return "当前没有可用 Skill。"
        lines = ["可用 Skill（需要时调用 activate_skill 加载完整内容）："]
        for skill in self.skills.values():
            line = f"- {skill.name}: {skill.description} [path={skill.path}]"
            if sum(len(existing) + 1 for existing in lines) + len(line) > max_chars:
                lines.append("- ... 其余 Skill 因目录上下文预算省略")
                break
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def activation_tool_definition() -> ToolDefinition:
        return ToolDefinition(
            name="activate_skill",
            description=(
                "加载已发现 Skill 的完整专家说明。任务明显匹配时可调用多个；不要激活无关 Skill。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill 名称"},
                    "reason": {"type": "string", "description": "与当前任务相关的原因"},
                },
                "required": ["name", "reason"],
                "additionalProperties": False,
            },
        )

    @staticmethod
    def resource_tool_definition() -> ToolDefinition:
        return ToolDefinition(
            name="load_skill_resource",
            description=(
                "按需读取已激活 Skill 的 references、scripts 或 assets 中的文本资源。"
                "该动作只读取文件，不执行脚本。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "skill": {"type": "string", "description": "已激活 Skill 名称"},
                    "path": {
                        "type": "string",
                        "description": "相对 Skill 目录的资源路径",
                    },
                },
                "required": ["skill", "path"],
                "additionalProperties": False,
            },
        )

    @staticmethod
    def _load_skill(path: Path) -> Skill:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            raise ValueError("SKILL.md 缺少 YAML frontmatter")
        try:
            _, frontmatter, body = text.split("---", 2)
        except ValueError as exc:
            raise ValueError("SKILL.md frontmatter 未闭合") from exc
        raw = yaml.safe_load(frontmatter) or {}
        if not isinstance(raw, dict):
            raise ValueError("SKILL.md frontmatter 必须是对象")
        metadata = raw.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError("Skill metadata 必须是对象")
        return Skill(
            name=raw.get("name", ""),
            description=raw.get("description", ""),
            path=path.resolve(),
            instructions=body.strip(),
            metadata=metadata,
        )

    @staticmethod
    def _ineligible_reason(skill: Skill) -> str | None:
        bot_metadata: dict[str, Any] = skill.metadata.get("bot") or {}
        if not isinstance(bot_metadata, dict):
            return "metadata.bot 不是对象"
        platforms = bot_metadata.get("platforms") or []
        current_platform = platform.system().lower()
        if platforms and current_platform not in {str(item).lower() for item in platforms}:
            return f"当前平台 {current_platform} 不满足 Skill platforms 条件"
        architectures = bot_metadata.get("architectures") or []
        current_arch = platform.machine().lower()
        if architectures and current_arch not in {str(item).lower() for item in architectures}:
            return f"当前架构 {current_arch} 不满足 Skill architectures 条件"
        required_tools = bot_metadata.get("requires_tools") or []
        missing = [str(name) for name in required_tools if shutil.which(str(name)) is None]
        if missing:
            return f"缺少所需命令: {', '.join(missing)}"
        return None


class SkillManager:
    def __init__(self, catalog: SkillCatalog, max_auto_activated: int = 3) -> None:
        self.catalog = catalog
        self.max_auto_activated = max_auto_activated
        self.active: dict[str, ActiveSkill] = {}

    def activate(self, name: str, reason: str, *, explicit: bool) -> tuple[Skill | None, str]:
        skill = self.catalog.get(name)
        if skill is None:
            return None, f"Skill 不存在或不可用: {name}"
        existing = self.active.get(name)
        if existing:
            return skill, f"Skill 已激活: {name}"
        auto_count = sum(not item.explicit for item in self.active.values())
        if not explicit and auto_count >= self.max_auto_activated:
            return None, f"自动激活 Skill 已达到上限 {self.max_auto_activated}"
        self.active[name] = ActiveSkill(name=name, reason=reason, explicit=explicit)
        return skill, self.render(skill)

    @staticmethod
    def render(skill: Skill) -> str:
        return (
            f"已激活 Skill: {skill.name}\n"
            f"来源: {skill.path}\n"
            f"说明: {skill.description}\n\n"
            f"{skill.instructions}"
        )

    def reset(self) -> None:
        self.active.clear()

    def load_resource(
        self, skill_name: str, relative_path: str, *, max_chars: int = 100_000
    ) -> tuple[str | None, str]:
        if skill_name not in self.active:
            return None, f"Skill 尚未激活: {skill_name}"
        skill = self.catalog.get(skill_name)
        if skill is None:
            return None, f"Skill 不存在或已不可用: {skill_name}"
        relative = Path(relative_path)
        if relative.is_absolute() or not relative.parts:
            return None, "Skill 资源路径必须是相对路径"
        if relative.parts[0] not in {"references", "scripts", "assets"}:
            return None, "只允许读取 references/、scripts/ 或 assets/ 下的资源"
        root = skill.path.parent.resolve()
        try:
            resource = (root / relative).resolve(strict=True)
            resource.relative_to(root)
        except (OSError, ValueError):
            return None, f"Skill 资源不存在或路径逃逸: {relative_path}"
        if not resource.is_file():
            return None, f"Skill 资源不是文件: {relative_path}"
        try:
            content = resource.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return None, f"Skill 资源不是可读 UTF-8 文本: {exc}"
        if len(content) > max_chars:
            content = content[:max_chars] + "\n…资源因上下文预算截断"
        rendered = f"Skill {skill_name} 资源 {relative_path}:\n\n{content}"
        return rendered, rendered
