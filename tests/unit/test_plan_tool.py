from pathlib import Path

import pytest

from bot.execution import LocalExecutionTarget
from bot.tools import ToolContext
from bot.tools.plan import UpdatePlanTool


def _context(tmp_path: Path) -> ToolContext:
    return ToolContext(workspace=tmp_path, execution_target=LocalExecutionTarget())


@pytest.mark.asyncio
async def test_update_plan_normalizes_and_returns_complete_snapshot(tmp_path: Path) -> None:
    result = await UpdatePlanTool().execute(
        _context(tmp_path),
        {
            "explanation": "  开始实现  ",
            "items": [
                {"content": "  分析入口  ", "status": "completed"},
                {"content": "实现功能", "status": "in_progress"},
                {"content": "运行测试", "status": "pending"},
            ],
        },
    )

    assert result.success is True
    assert result.metadata["plan_update"] == {
        "explanation": "开始实现",
        "items": [
            {"content": "分析入口", "status": "completed"},
            {"content": "实现功能", "status": "in_progress"},
            {"content": "运行测试", "status": "pending"},
        ],
    }
    assert result.metadata["counts"] == {
        "pending": 1,
        "in_progress": 1,
        "completed": 1,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("items", "message"),
    [
        (
            [
                {"content": "重复", "status": "pending"},
                {"content": " 重复 ", "status": "completed"},
            ],
            "TODO 内容不能重复",
        ),
        (
            [
                {"content": "第一项", "status": "in_progress"},
                {"content": "第二项", "status": "in_progress"},
            ],
            "最多只能有一个 in_progress TODO",
        ),
        ([{"content": "   ", "status": "pending"}], "TODO 内容不能为空"),
    ],
)
async def test_update_plan_rejects_invalid_snapshots(
    tmp_path: Path,
    items: list[dict[str, str]],
    message: str,
) -> None:
    result = await UpdatePlanTool().execute(_context(tmp_path), {"items": items})

    assert result.success is False
    assert message in (result.error or "")


@pytest.mark.asyncio
async def test_update_plan_accepts_empty_list_to_clear_plan(tmp_path: Path) -> None:
    result = await UpdatePlanTool().execute(_context(tmp_path), {"items": []})

    assert result.success is True
    assert result.metadata["plan_update"]["items"] == []
