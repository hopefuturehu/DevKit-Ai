from pathlib import Path

from bot.subagents import AgentCatalog, AgentSource
from bot.tools import ToolRegistry
from bot.tools.builtins import ApplyPatchTool, ReadFileTool, SearchTextTool


def _write_agent(root: Path, name: str, *, tools: str = "read_file") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.md"
    path.write_text(
        f"""---
schema_version: 1
name: {name}
description: Test {name}
model: cheap-model
tools: [{tools}]
isolation: read_only
execution:
  default: foreground
  allowed: [foreground, background]
limits:
  max_steps: 12
interaction:
  can_request_input: true
output:
  format: structured
---

Follow the delegated task and return evidence.
""",
        encoding="utf-8",
    )
    return path


def _catalog(tmp_path: Path, *, trusted: bool) -> AgentCatalog:
    tools = ToolRegistry()
    tools.register(ReadFileTool())
    tools.register(SearchTextTool())
    tools.register(ApplyPatchTool())
    return AgentCatalog(
        builtin_root=tmp_path / "builtin",
        user_root=tmp_path / "user",
        project_root=tmp_path / "project",
        tools=tools,
        project_trusted=trusted,
        allow_worktree_writes=True,
    )


def test_catalog_loads_user_agent_but_hides_untrusted_project_agent(tmp_path: Path) -> None:
    _write_agent(tmp_path / "user", "user-reviewer")
    _write_agent(tmp_path / "project", "project-reviewer")
    catalog = _catalog(tmp_path, trusted=False)

    catalog.scan()

    assert set(catalog.agents) == {"user-reviewer"}
    spec = catalog.agents["user-reviewer"]
    assert spec.source == AgentSource.USER
    assert spec.model == "cheap-model"
    assert spec.max_steps == 12
    assert any("尚未信任" in item.message for item in catalog.diagnostics)

    catalog.project_trusted = True
    catalog.scan()
    assert set(catalog.agents) == {"user-reviewer", "project-reviewer"}
    assert catalog.agents["project-reviewer"].source == AgentSource.PROJECT


def test_catalog_disables_duplicates_and_rejects_write_tool_for_readonly(
    tmp_path: Path,
) -> None:
    _write_agent(tmp_path / "builtin", "duplicate")
    _write_agent(tmp_path / "user", "duplicate")
    _write_agent(tmp_path / "user", "unsafe", tools="apply_patch")
    catalog = _catalog(tmp_path, trusted=True)

    catalog.scan()

    assert catalog.agents == {}
    messages = [item.message for item in catalog.diagnostics]
    assert sum("名称重复" in message for message in messages) == 2
    assert any("read_only Agent 不允许" in message for message in messages)


def test_project_digest_changes_when_agent_definition_changes(tmp_path: Path) -> None:
    path = _write_agent(tmp_path / "project", "worker")
    before = AgentCatalog.compute_project_digest(path.parent)
    path.write_text(path.read_text(encoding="utf-8") + "\nNew rule.\n", encoding="utf-8")
    after = AgentCatalog.compute_project_digest(path.parent)
    assert before != after
