"""Bounded, chunk-independent detection of exact periodic prose suffixes."""

from collections import deque
from dataclasses import dataclass

from bot.config.models import StreamGuardConfig


@dataclass(frozen=True)
class Repetition:
    response_chars: int
    period_chars: int
    repeated_span_chars: int
    repetitions: int


def _z_array(text: str) -> list[int]:
    z = [0] * len(text)
    left = right = 0
    for i in range(1, len(text)):
        if i <= right:
            z[i] = min(right - i + 1, z[i - left])
        while i + z[i] < len(text) and text[z[i]] == text[i + z[i]]:
            z[i] += 1
        if i + z[i] - 1 > right:
            left, right = i, i + z[i] - 1
    return z


class StreamGuard:
    def __init__(self, config: StreamGuardConfig) -> None:
        self.config = config
        self.window: deque[str] = deque(maxlen=config.window_chars)
        self.response_chars = 0
        self.normalized_chars = 0
        self.last_space = False
        self.fence: str | None = None
        self.marker = ""
        self.marker_count = 0
        self.line_start = True
        self.structured = False
        self.disabled = config.mode == "off"
        self.previous: tuple[int, int] | None = None
        self.confirmations = 0
        self.hit: Repetition | None = None

    def reset_window(self) -> None:
        self.window.clear()
        self.previous = None
        self.confirmations = 0
        self.last_space = False

    def feed(self, text: str) -> Repetition | None:
        # A large delta is processed at the same character boundaries as tiny deltas.
        for char in text:
            self.response_chars += 1
            if self.disabled or self.hit:
                continue
            if self.line_start and not char.isspace():
                if char in "{[":
                    self.structured = True
                self.line_start = False
            if char == "\n":
                self.line_start = True
            if char in "`~":
                self.marker_count = self.marker_count + 1 if char == self.marker else 1
                self.marker = char
                if self.marker_count == 3:
                    self.fence = None if self.fence == char else char
                    self.reset_window()
            else:
                self.marker_count = 0
                self.marker = ""
            if self.fence or self.structured or char in "`~":
                continue
            if char.isspace():
                if self.last_space:
                    continue
                char = " "
            self.last_space = char == " "
            self.window.append(char)
            self.normalized_chars += 1
            if (
                self.response_chars >= self.config.min_response_chars
                and self.normalized_chars % self.config.check_every_chars == 0
            ):
                self._check()
        return self.hit

    def _check(self) -> None:
        text = "".join(self.window)[::-1]
        z = _z_array(text)
        candidate = None
        for period in range(
            self.config.min_period_chars, min(self.config.max_period_chars, len(text) // 2) + 1
        ):
            span = period + z[period]
            if (
                span < self.config.min_repeated_span_chars
                or span // period < self.config.min_repetitions
            ):
                continue
            block = text[:period]
            if sum(c in ".!?。！？；;" for c in block) < 2:
                continue
            if sum(c.isalpha() for c in block) < len(block) * 0.4:
                continue
            candidate = (period, span)
            break
        if candidate is None:
            self.previous = None
            self.confirmations = 0
            return
        if self.previous and candidate[0] == self.previous[0] and candidate[1] > self.previous[1]:
            self.confirmations += 1
        else:
            self.confirmations = 1
        self.previous = candidate
        if self.confirmations >= self.config.confirmations:
            self.hit = Repetition(self.response_chars, *candidate, candidate[1] // candidate[0])
