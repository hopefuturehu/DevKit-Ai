from pathlib import Path

from bot.core.context import ContextAssembler, compact_messages, estimate_tokens
from bot.core.events import AgentEvent, EventType
from bot.core.models import ChatMessage, Role
from bot.observability import Redactor
from bot.skills import SkillCatalog
from bot.tools import ToolResult


def test_context_manifest_reports_instruction_sources(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("project rule", encoding="utf-8")
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill.\n---\nInstructions.",
        encoding="utf-8",
    )
    catalog = SkillCatalog(tmp_path / "skills")
    catalog.scan()
    assembler = ContextAssembler(workspace=tmp_path, skill_catalog=catalog)

    manifest = assembler.manifest()

    assert [entry["layer"] for entry in manifest] == [
        "core_policy",
        "project_context",
        "skill_catalog",
    ]
    assert manifest[-1]["items"] == 1


def test_context_discovers_agents_hierarchy_from_repo_root(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "services" / "api"
    nested.mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("root rule", encoding="utf-8")
    (tmp_path / "services" / "AGENTS.md").write_text("service rule", encoding="utf-8")
    (nested / "AGENTS.md").write_text("api rule", encoding="utf-8")
    catalog = SkillCatalog(nested / "skills")
    catalog.scan()
    assembler = ContextAssembler(workspace=nested, skill_catalog=catalog)

    files = assembler.project_instruction_files()

    assert files == [
        tmp_path / "AGENTS.md",
        tmp_path / "services" / "AGENTS.md",
        nested / "AGENTS.md",
    ]


def test_context_compaction_preserves_system_and_recent_messages() -> None:
    messages = [ChatMessage(role=Role.SYSTEM, content="policy")]
    messages.extend(
        ChatMessage(role=Role.USER if index % 2 == 0 else Role.ASSISTANT, content="x" * 200)
        for index in range(20)
    )
    before = estimate_tokens(messages)

    compacted, details = compact_messages(
        messages,
        max_tokens=500,
        threshold=0.5,
        recent_count=6,
    )

    assert details is not None
    assert details["messages_summarized"] == 14
    assert compacted[0].content == "policy"
    assert "被压缩的旧会话摘要" in (compacted[1].content or "")
    assert compacted[-6:] == messages[-6:]
    assert estimate_tokens(compacted) < before


def test_redactor_removes_known_secrets_ansi_and_generic_credentials() -> None:
    redactor = Redactor(["known-secret"])
    text = redactor.redact_text(
        "\x1b[31mknown-secret\x1b[0m Bearer abcdefghijkl token=super-secret"
    )
    event = redactor.redact_event(
        AgentEvent(
            type=EventType.TOOL_OUTPUT,
            session_id="s",
            run_id="r",
            payload={"output": "known-secret"},
        )
    )
    result = redactor.redact_tool_result(ToolResult(success=True, output="password=hunter22"))

    assert "known-secret" not in text
    assert "\x1b" not in text
    assert text.count("[REDACTED]") == 3
    assert event.payload["output"] == "[REDACTED]"
    assert result.output == "password=[REDACTED]"
