from io import StringIO

import pytest

from bot.cli.render import RichEventSink, create_cli_console
from bot.core.events import AgentEvent, EventType


def _event(event_type: EventType, payload: dict) -> AgentEvent:
    return AgentEvent(
        type=event_type,
        session_id="session",
        run_id="run",
        payload=payload,
    )


@pytest.mark.asyncio
async def test_tool_arguments_are_plain_text_without_ansi_or_rich_markup_parsing() -> None:
    stream = StringIO()
    sink = RichEventSink(create_cli_console(file=stream, width=500))

    await sink.publish(
        _event(
            EventType.TOOL_REQUESTED,
            {
                "name": "apply_patch",
                "arguments": {
                    "path": "docs/solution.md",
                    "label": "[literal]",
                    "new_text": "# 标题\n> 状态：[草案](design.md)",
                },
            },
        )
    )

    output = stream.getvalue()
    assert "\x1b" not in output
    assert "[cyan]" not in output
    assert "→ Tool apply_patch" in output
    assert "[literal]" in output
    assert "chars, 2 lines>" in output
    assert "# 标题" not in output
    assert "[草案](design.md)" not in output


@pytest.mark.asyncio
async def test_assistant_markdown_is_preserved_for_the_output_consumer() -> None:
    stream = StringIO()
    sink = RichEventSink(create_cli_console(file=stream, width=500))
    markdown = "# 标题\n\n- 第一项\n- 第二项"

    await sink.publish(_event(EventType.ASSISTANT_DELTA, {"text": markdown}))
    await sink.publish(_event(EventType.ASSISTANT_MESSAGE, {"text": markdown}))

    output = stream.getvalue()
    assert "\x1b" not in output
    assert output == markdown + "\n"


@pytest.mark.asyncio
async def test_progress_recovery_and_blocked_events_are_visible() -> None:
    stream = StringIO()
    sink = RichEventSink(create_cli_console(file=stream, width=500))

    await sink.publish(_event(EventType.RUN_STALL_WARNING, {"message": "重复读取"}))
    await sink.publish(
        _event(
            EventType.RUN_RECOVERY_STARTED,
            {"message": "切换路径", "recovery_attempt": 1},
        )
    )
    await sink.publish(_event(EventType.RUN_FINALIZING, {}))
    await sink.publish(_event(EventType.RUN_BLOCKED, {"message": "恢复后仍无进展"}))
    await sink.publish(_event(EventType.RUN_LIMIT_REACHED, {"error": "达到费用边界"}))
    await sink.publish(_event(EventType.RUN_CANCELLED, {}))

    output = stream.getvalue()
    assert "进展警告：重复读取" in output
    assert "正在纠偏：切换路径" in output
    assert "正在生成收尾说明" in output
    assert "运行已阻塞：恢复后仍无进展" in output
    assert "运行达到策略边界：达到费用边界" in output
    assert "运行已取消" in output


@pytest.mark.asyncio
async def test_compaction_request_progress_is_visible() -> None:
    stream = StringIO()
    sink = RichEventSink(create_cli_console(file=stream, width=500))

    await sink.publish(
        _event(
            EventType.CONTEXT_COMPACTION_REQUEST_STARTED,
            {
                "source_range": [105, 231],
                "phase": "generate",
                "planned_input_tokens": 47_619,
            },
        )
    )
    await sink.publish(
        _event(
            EventType.CONTEXT_COMPACTION_REQUEST_COMPLETED,
            {
                "phase": "condense",
                "duration_ms": 12_500,
                "visible_summary_tokens": 3_200,
            },
        )
    )
    await sink.publish(
        _event(
            EventType.CONTEXT_COMPACTION_REQUEST_FAILED,
            {"error_class": "timeout", "error": "90 秒超时"},
        )
    )

    output = stream.getvalue()
    assert "105–231 generate" in output
    assert "input≈47619 tokens" in output
    assert "condense，duration=12.5s" in output
    assert "timeout，90 秒超时" in output
