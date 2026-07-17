from pathlib import Path

from bot.skills import SkillCatalog, SkillManager


def write_skill(root: Path, directory: str, name: str, description: str = "Useful skill") -> None:
    skill_dir = root / directory
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nFollow the evidence.\n",
        encoding="utf-8",
    )


def test_catalog_scans_only_direct_children_and_detects_duplicates(tmp_path: Path) -> None:
    write_skill(tmp_path, "one", "duplicate")
    write_skill(tmp_path, "two", "duplicate")
    write_skill(tmp_path, "valid", "valid-skill")
    nested = tmp_path / "container" / "nested"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text(
        "---\nname: nested\ndescription: nested\n---\nbody", encoding="utf-8"
    )

    catalog = SkillCatalog(tmp_path)
    catalog.scan()

    assert list(catalog.skills) == ["valid-skill"]
    assert any("名称重复" in item.message for item in catalog.diagnostics)
    assert catalog.get("nested") is None


def test_skill_manager_explicit_activation_precedes_auto_limit(tmp_path: Path) -> None:
    write_skill(tmp_path, "one", "one")
    write_skill(tmp_path, "two", "two")
    write_skill(tmp_path, "three", "three")
    catalog = SkillCatalog(tmp_path)
    catalog.scan()
    manager = SkillManager(catalog, max_auto_activated=1)

    first, _ = manager.activate("one", "automatic", explicit=False)
    blocked, message = manager.activate("two", "automatic", explicit=False)
    explicit, _ = manager.activate("three", "user selected", explicit=True)

    assert first is not None
    assert blocked is None
    assert "达到上限" in message
    assert explicit is not None


def test_skill_resources_require_activation_and_stay_inside_allowed_directories(
    tmp_path: Path,
) -> None:
    write_skill(tmp_path, "one", "one")
    references = tmp_path / "one" / "references"
    references.mkdir()
    (references / "guide.md").write_text("domain evidence", encoding="utf-8")
    catalog = SkillCatalog(tmp_path)
    catalog.scan()
    manager = SkillManager(catalog)

    blocked, _ = manager.load_resource("one", "references/guide.md")
    manager.activate("one", "test", explicit=True)
    loaded, _ = manager.load_resource("one", "references/guide.md")
    escaped, message = manager.load_resource("one", "../outside.md")

    assert blocked is None
    assert loaded is not None and "domain evidence" in loaded
    assert escaped is None
    assert "只允许读取" in message


def test_repository_kunpeng_skill_is_valid_and_has_loadable_reference() -> None:
    root = Path(__file__).resolve().parents[2] / "skills"
    catalog = SkillCatalog(root)
    catalog.scan()
    manager = SkillManager(catalog)

    skill, _ = manager.activate(
        "kunpeng-performance-analysis", "repository validation", explicit=True
    )
    reference, _ = manager.load_resource(
        "kunpeng-performance-analysis", "references/tool-selection.md"
    )

    assert skill is not None
    assert reference is not None
    assert "KSYS `diff`" in reference
    assert not [item for item in catalog.diagnostics if item.level == "error"]
