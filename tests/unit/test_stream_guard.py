from pathlib import Path

import pytest

from bot.config.models import StreamGuardConfig
from bot.core.termination.stream_guard import StreamGuard

FIXTURE = Path(__file__).parents[1] / "fixtures/reliability/512k-step111-prefix.txt"


@pytest.mark.parametrize("chunk", [1, 137, 4096, 16000])
def test_real_512k_response_detected_before_16k_chars(chunk):
    text = FIXTURE.read_text()
    guard = StreamGuard(StreamGuardConfig())
    for start in range(0, len(text), chunk):
        hit = guard.feed(text[start : start + chunk])
        if hit:
            break
    assert hit is not None
    assert hit.response_chars == 14156
    assert hit.period_chars == 859
    assert hit.repetitions >= 6


@pytest.mark.parametrize(
    "text",
    [
        "```python\n"
        + 'print("Sentence one. Sentence two. Enough words here.")\n' * 2000
        + "\n```",
        '{"text": "' + "Sentence one. Sentence two. Enough words here. " * 1000 + '"}',
        "\n".join(
            f"第 {i} 项测量。当前坐标 {i * 13}。这些数值不同，需要保留。" for i in range(1500)
        ),
        "\n".join(
            f"| {i} | Changing measurement. Another sentence. Value {i * 7}. |" for i in range(1500)
        ),
        " ".join(f"Observation {i}. The next result is {i * 10007}." for i in range(2000)),
    ],
)
def test_long_legitimate_output_is_not_interrupted(text):
    guard = StreamGuard(StreamGuardConfig())
    for start in range(0, len(text), 31):
        assert guard.feed(text[start : start + 31]) is None
    assert len(guard.window) <= guard.config.window_chars


def test_chinese_prose_and_whitespace_normalization():
    block = "现在继续分析这个问题。已有证据仍不足以判断原因，需要重新检查输入文件。" * 5
    guard = StreamGuard(StreamGuardConfig())
    assert guard.feed((block + "\n\n") * 150)
